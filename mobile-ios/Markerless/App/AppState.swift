// AppState.swift — 전역 상태: 서버 주소/프로젝트/공간 선택 (UserDefaults 영속)

import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @AppStorage("baseURL") var baseURLString: String = "http://192.168.0.10:8111"
    @AppStorage("projectId") var projectId: String = "PJT001"
    /// 외부 접속(터널) 시 서버가 요구하는 API 키 — 로컬 개발 서버는 비워 둔다
    @AppStorage("apiKey") var apiKey: String = ""
    @AppStorage("deviceId") var deviceId: String = UIDevice.current.name

    /// 0=① 공간, 1=② 스캔·등록, 2=③ 검수 — 단계 안내 버튼이 다음 탭으로 보낸다
    @Published var selectedTab = 0

    /// 자산 관리에서 [재등록]을 누르면 설정 — ② 스캔·등록이 이 ID로 열린다 (저장하면 덮어씀)
    @Published var reregisterAssetId: String?

    init() {
        // 개발 빌드: 처음 실행(키 없음)이거나 예전 기본 주소면 배포 때 주입된 값으로 채운다 → 401 잡음 방지
        let d = UserDefaults.standard
        if (d.string(forKey: "apiKey") ?? "").isEmpty, !DevDefaults.apiKey.isEmpty {
            d.set(DevDefaults.apiKey, forKey: "apiKey")
        }
        let url = d.string(forKey: "baseURL") ?? ""
        if (url.isEmpty || url == "http://192.168.0.10:8111"), !DevDefaults.tunnelURL.isEmpty {
            d.set(DevDefaults.tunnelURL, forKey: "baseURL")
        }
    }

    @Published var spaceId: String = UserDefaults.standard.string(forKey: "spaceId") ?? "" {
        didSet { UserDefaults.standard.set(spaceId, forKey: "spaceId") }
    }
    @Published var spaceNumericId: Int = UserDefaults.standard.integer(forKey: "spaceNumericId") {
        didSet {
            UserDefaults.standard.set(spaceNumericId, forKey: "spaceNumericId")
            if oldValue != spaceNumericId { Task { await syncOnce(force: true) } }  // 공간을 바꾸면 그 공간의 이름·자산 이름을 바로 읽는다
        }
    }

    // MARK: - 통합관리자 수정 사항 자동 반영
    //
    // 주기적으로 묻지 않는다. 서버가 "저장이 일어난 시점"에 이벤트(SSE, GET /api/v1/sync/events)를 보내므로
    // 앱은 연결을 열어 두고 있다가 이벤트가 올 때만 공간·자산 정보를 다시 읽고 dataRevision을 올린다.
    // 각 화면(공간·자산 관리·검수)은 dataRevision이 바뀔 때 스스로 새로 읽는다.
    // 연결이 끊기면(네트워크·백그라운드) 다시 연결하고, 연결될 때마다 한 번 따라잡는다.

    /// 관리자 변경을 감지할 때마다 1씩 오른다 — 화면은 .onChange(of:)로 새로 읽는다
    @Published var dataRevision = 0
    /// 선택한 공간의 현재 이름 (관리자가 고치면 바뀐다)
    @Published var spaceName = ""
    /// assetId → 표시 이름 (관리자가 고친 이름)
    @Published var assetNames: [String: String] = [:]
    /// 삭제·보관 등으로 선택이 풀렸을 때의 안내 (화면에서 확인하면 지운다)
    @Published var syncNotice: String?
    private var syncTask: Task<Void, Never>?
    private var lastRevision: String?

    func assetName(_ id: String) -> String { assetNames[id].flatMap { $0.isEmpty ? nil : $0 } ?? id }

    func startSync() {
        guard syncTask == nil else { return }
        syncTask = Task { [weak self] in await self?.eventLoop() }
    }

    func stopSync() { syncTask?.cancel(); syncTask = nil }

    /// 이벤트 연결 유지: 끊기면 2·4·8…30초 간격으로 다시 연결한다 (데이터를 묻는 게 아니라 연결을 되살리는 것)
    private func eventLoop() async {
        var wait: UInt64 = 2
        while !Task.isCancelled {
            guard let base = baseURL else { try? await Task.sleep(nanoseconds: 3_000_000_000); continue }
            var req = URLRequest(url: base.appendingPathComponent("api/v1/sync/events"))
            req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            req.timeoutInterval = 70  // 서버가 20초마다 연결 유지용 신호를 보낸다 — 그것도 없으면 끊긴 연결로 보고 다시 연결
            let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
            if !key.isEmpty { req.setValue(key, forHTTPHeaderField: "X-API-Key") }
            do {
                let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                wait = 2
                DebugLog.log("sync", "변경 이벤트 연결됨")
                await syncOnce(force: true)  // 연결(재연결) 직후 한 번 따라잡기
                for try await line in bytes.lines {
                    // "data: {...}" 한 줄 = 저장이 일어났다. 첫 hello는 위에서 이미 따라잡았으므로 건너뛴다
                    if line.hasPrefix("data:"), !line.contains("\"hello\"") { await syncOnce(force: true) }
                }
            } catch {
                if Task.isCancelled { break }
            }
            if Task.isCancelled { break }
            try? await Task.sleep(nanoseconds: wait * 1_000_000_000)
            wait = min(wait * 2, 30)
        }
    }

    func syncOnce(force: Bool = false) async {
        guard baseURL != nil else { return }
        do {
            let st = try await api.syncState()
            if !force, st.revision == lastRevision { return }
            let spaces = try await api.spaces(projectId: projectId)
            if spaceNumericId > 0 {
                if let s = spaces.first(where: { $0.id == spaceNumericId }) {
                    if s.code != spaceId { spaceId = s.code }  // 관리자가 공간 ID를 바꿈
                    spaceName = s.name ?? ""
                    if let list = try? await api.assetsInSpace(spaceNumericId: spaceNumericId) {
                        assetNames = list.reduce(into: [:]) { $0[$1.assetId] = $1.name ?? $1.assetId }
                    }
                } else {
                    syncNotice = "선택했던 공간 '\(spaceId)'이(가) 관리자에 의해 삭제되었습니다 — 홈에서 다시 선택하세요"
                    DebugLog.log("sync", "선택한 공간이 서버에서 삭제됨 → 선택 해제", extra: ["space": spaceId])
                    spaceId = ""; spaceNumericId = 0; spaceName = ""; assetNames = [:]
                }
            }
            let first = lastRevision == nil
            lastRevision = st.revision
            if !first || force {
                dataRevision += 1
                DebugLog.log("sync", "저장 이벤트 → 새로 읽기", extra: ["revision": st.revision, "spaces": "\(st.spaces)", "assets": "\(st.assets)"])
            }
        } catch { /* 일시적인 네트워크 오류는 다음 주기(4초 뒤)에 다시 확인한다 */ }
    }

    var baseURL: URL? { AppState.normalizedURL(baseURLString) }

    var api: APIClient {
        APIClient(baseURL: baseURL, apiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// 공백 제거, 스킴 없으면 https:// (외부 URL) 보정, 끝의 / 제거
    nonisolated static func normalizedURL(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") {
            let local = s.hasPrefix("192.168.") || s.hasPrefix("10.") || s.hasPrefix("localhost") || s.hasPrefix("127.")
            s = (local ? "http://" : "https://") + s
        }
        while s.hasSuffix("/") { s.removeLast() }
        return URL(string: s)
    }

    var isSpaceSelected: Bool { !spaceId.isEmpty && spaceNumericId > 0 }
}
