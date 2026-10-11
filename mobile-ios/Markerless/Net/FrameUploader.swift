// FrameUploader.swift — 공간 스캔 프레임 영속 업로드 큐 (docs/INTERFACES.md §5)
//
// 흐름(세션 하나): ① POST /spaces/{id}/scan-sessions → ② 프레임마다 init-upload(idempotencyKey="<로컬세션>-<idx>", sha256)
//   → ③ 파트(rgb/depth/conf)를 1MiB 청크로 PUT ?offset= (init-upload가 돌려준 received부터 재개)
//   → ④ complete-upload → ⑤ (RoomPlan structure.json 있으면 POST /structure) → ⑥ finalize → ⑦ reconstruct → ⑧ status 폴링.
// 상태는 전부 디스크(SpaceScanStore: session.json + queue.json)에 있으므로 앱을 껐다 켜도 이어서 올린다 (resumePending).
// 네트워크 오류는 지수 백오프(2→60초)로 계속 재시도하고, 4xx(영구 오류)는 해당 프레임만 failed로 표시하고 넘어간다.
// 동시 3프레임까지 병렬. 업로드는 촬영 [완료] 후에 시작한다 (촬영 중 '구간 제외'와의 경합 방지).
// 앱이 백그라운드로 가면 OS가 Task를 멈춘다 — 별도 background URLSession은 쓰지 않는다(실기기 검증 필요).

import CryptoKit
import Foundation

@MainActor
final class FrameUploader: ObservableObject {
    static let shared = FrameUploader()

    struct Progress: Equatable {
        var total = 0
        var uploaded = 0
        var failed = 0
        var stage = "대기"
        var message: String?
        var serverStatus: String?  // PROCESSING / READY / NEED_MORE_CAPTURE / FAILED …
        var serverStage: String?
        var serverProgress: Double?
        var quality: String?
        var coverage: String?
        var done = false
    }

    @Published private(set) var progress: [String: Progress] = [:]
    /// 현재 서버 주소·키 (AppState.api). RootView가 연결해 준다.
    var apiProvider: (@MainActor () -> APIClient)?
    private var runner: Task<Void, Never>?
    private let chunk = 1 << 20

    private enum FrameResult: Sendable { case ok, transient(String), fatal(String) }

    // MARK: 공개

    func enqueue(_ id: String) {
        var q = SpaceScanStore.queue()
        if !q.contains(id) { q.append(id); SpaceScanStore.setQueue(q) }
        if let r = SpaceScanStore.load(id) { setProgress(id, from: r, stage: "업로드 대기") }
        kick()
    }

    /// 앱 시작 시: 촬영 도중 종료된 세션을 마감하고 대기열을 이어서 처리
    func resumePending() {
        for id in SpaceScanStore.recoverUnfinished() { enqueue(id) }
        for id in SpaceScanStore.queue() { if let r = SpaceScanStore.load(id) { setProgress(id, from: r, stage: "업로드 대기") } }
        kick()
    }

    func retry(_ id: String) {
        guard var r = SpaceScanStore.load(id) else { return }
        r.failed = []; r.serverStatus = nil
        SpaceScanStore.save(r)
        enqueue(id)
    }

    func kick() {
        guard runner == nil else { return }
        runner = Task { [weak self] in
            await self?.runLoop()
            self?.runner = nil
        }
    }

    // MARK: 실행 루프

    private func runLoop() async {
        while !Task.isCancelled {
            guard let id = SpaceScanStore.queue().first else { return }
            await process(id)
            var q = SpaceScanStore.queue(); q.removeAll { $0 == id }; SpaceScanStore.setQueue(q)
        }
    }

    private func setProgress(_ id: String, from r: SpaceSessionRecord, stage: String? = nil, message: String? = nil) {
        var p = progress[id] ?? Progress()
        p.total = r.frameIndices.count
        p.uploaded = r.uploaded.count
        p.failed = r.failed.count
        if let stage { p.stage = stage }
        if let message { p.message = message }
        if let s = r.serverStatus { p.serverStatus = s }
        progress[id] = p
    }

    private func waitBackoff(_ attempt: Int) async {
        let secs = min(60, 2 << min(attempt, 5))
        try? await Task.sleep(nanoseconds: UInt64(secs) * 1_000_000_000)
    }

    private func api() -> APIClient? {
        guard let a = apiProvider?(), a.baseURL != nil else { return nil }
        return a
    }

    private func process(_ id: String) async {
        guard var r = SpaceScanStore.load(id) else { return }
        var attempt = 0

        func note(_ stage: String, _ msg: String? = nil) {
            SpaceScanStore.save(r)
            setProgress(id, from: r, stage: stage, message: msg)
        }

        // ① 세션 생성
        while r.serverSessionId == nil, !Task.isCancelled {
            guard let api = api() else { note("서버 주소 대기"); await waitBackoff(attempt); attempt += 1; continue }
            do {
                note("서버 세션 생성")
                let out = try await api.createScanSession(spaceNumericId: r.spaceNumericId, r.create)
                r.serverSessionId = out.scanSessionId
                attempt = 0
                note("세션 생성됨")
            } catch let e as APIError where (400..<500).contains(e.status) && e.status != 408 && e.status != 429 {
                r.serverStatus = "FAILED"; r.message = "세션 생성 거부: \(e.message)"
                note("실패", r.message); return
            } catch {
                note("재시도 대기", "세션 생성 실패: \(error.localizedDescription)"); await waitBackoff(attempt); attempt += 1
            }
        }
        guard let sid = r.serverSessionId else { return }

        // ②~④ 프레임 업로드 (동시 3)
        attempt = 0
        while !Task.isCancelled {
            let todo = r.frameIndices.filter { !r.uploaded.contains($0) && !r.failed.contains($0) }
            if todo.isEmpty { break }
            guard let api = api() else { note("서버 주소 대기"); await waitBackoff(attempt); attempt += 1; continue }
            var it = todo.makeIterator()
            var transientSeen = false
            await withTaskGroup(of: (Int, FrameResult).self) { group in
                func launch() {
                    guard let i = it.next() else { return }
                    let epoch = r.epochs["\(i)"] ?? 0
                    let rid = r.id
                    group.addTask { [chunk] in (i, await FrameUploader.uploadFrame(api: api, localId: rid, serverId: sid, index: i, epoch: epoch, chunk: chunk)) }
                }
                for _ in 0..<3 { launch() }
                while let (i, res) = await group.next() {
                    switch res {
                    case .ok: r.uploaded.append(i)
                    case .fatal(let m):
                        // 해시 불일치 등 → 한 번은 새 세대 키로 다시 시도, 그래도면 실패 처리
                        let e = (r.epochs["\(i)"] ?? 0) + 1
                        if e <= 2 { r.epochs["\(i)"] = e } else { r.failed.append(i); r.message = "프레임 \(i) 업로드 실패: \(m)" }
                    case .transient(let m): transientSeen = true; r.message = m
                    }
                    note("프레임 업로드 \(r.uploaded.count)/\(r.frameIndices.count)", r.message)
                    launch()
                }
            }
            if transientSeen { await waitBackoff(attempt); attempt += 1 } else { attempt = 0 }
        }

        // ⑤ 구조(RoomPlan)
        attempt = 0
        let structURL = SpaceScanStore.dir(id).appendingPathComponent("structure.json")
        while r.hasStructure, !r.structureUploaded, !Task.isCancelled {
            guard let api = api(), let data = try? Data(contentsOf: structURL),
                  let s = try? JSONDecoder().decode(SpaceStructureJSON.self, from: data) else { break }
            do {
                note("구조 업로드")
                try await api.uploadStructure(sessionId: sid, s)
                r.structureUploaded = true; note("구조 업로드됨")
            } catch let e as APIError where (400..<500).contains(e.status) && e.status != 429 {
                r.message = "구조 업로드 거부(\(e.status)) — 건너뜀"; r.structureUploaded = true; note("구조 건너뜀", r.message)
            } catch { note("재시도 대기", "구조 업로드 실패"); await waitBackoff(attempt); attempt += 1 }
        }

        // ⑥ finalize
        attempt = 0
        while !r.finalized, !Task.isCancelled {
            guard let api = api() else { await waitBackoff(attempt); attempt += 1; continue }
            do {
                note("촬영 마감(finalize)")
                try await api.finalizeScanSession(sessionId: sid, endedReason: r.endedReason ?? "USER_FINISHED")
                r.finalized = true; note("마감됨")
            } catch let e as APIError where (400..<500).contains(e.status) && e.status != 429 && e.status != 408 {
                r.serverStatus = "FAILED"; r.message = "finalize 거부: \(e.message)"; note("실패", r.message); return
            } catch { note("재시도 대기", "finalize 실패"); await waitBackoff(attempt); attempt += 1 }
        }

        // ⑦⑧ 상태 확인 → (필요 시) 재구성 → 폴링
        attempt = 0
        var polls = 0
        while !Task.isCancelled, polls < 900 {  // 최대 약 45분
            guard let api = api() else { await waitBackoff(attempt); attempt += 1; continue }
            do {
                let st = try await api.scanSessionStatus(sessionId: sid)
                attempt = 0
                r.serverStatus = st.status
                var p = progress[id] ?? Progress()
                p.serverStatus = st.status; p.serverStage = st.stage; p.serverProgress = st.progress
                p.quality = st.quality?.display; p.coverage = st.coverage?.display
                p.message = st.message ?? p.message
                progress[id] = p
                let s = st.status.uppercased()
                if s == "READY" || s == "NEED_MORE_CAPTURE" || s == "FAILED" {
                    r.message = st.message; note("서버 상태: \(s)", st.message)
                    progress[id]?.done = true
                    return
                }
                if s == "PROCESSING", !r.reconstructQueued {  // 마감 후 사전검사 통과 → 비동기 재구성 잡 큐잉
                    try await api.reconstructScanSession(sessionId: sid)
                    r.reconstructQueued = true; note("재구성 요청됨")
                }
                note("서버 처리 중 \(st.stage ?? "")")
            } catch let e as APIError where (400..<500).contains(e.status) && e.status != 429 && e.status != 408 {
                // reconstruct 409(이미 큐잉) 등은 무시하고 계속 폴링, 그 외 4xx는 실패
                if e.status == 409 { r.reconstructQueued = true }
                else { r.serverStatus = "FAILED"; r.message = "서버 거부: \(e.message)"; note("실패", r.message); return }
            } catch { attempt += 1; note("재시도 대기", "상태 확인 실패") }
            polls += 1
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }

    // MARK: 프레임 하나 (nonisolated — 파일 읽기·해시·네트워크는 메인 밖)

    private nonisolated static func uploadFrame(api: APIClient, localId: String, serverId: String, index: Int,
                                                epoch: Int, chunk: Int) async -> FrameResult {
        do {
            let rec = try JSONDecoder().decode(FrameDiskRecord.self, from: Data(contentsOf: SpaceScanStore.frameURL(localId, index, "json")))
            let parts: [(String, Data)] = [
                ("rgb", try Data(contentsOf: SpaceScanStore.frameURL(localId, index, "jpg"))),
                ("depth", try Data(contentsOf: SpaceScanStore.frameURL(localId, index, "depth"))),
                ("conf", try Data(contentsOf: SpaceScanStore.frameURL(localId, index, "conf"))),
            ]
            var infos: [String: FramePartInfoJSON] = [:]
            var hashes: [String: String] = [:]
            for (n, d) in parts {
                let h = SHA256.hash(data: d).map { String(format: "%02x", $0) }.joined()
                infos[n] = FramePartInfoJSON(bytes: d.count, sha256: h); hashes[n] = h
            }
            let key = epoch == 0 ? "\(localId)-\(index)" : "\(localId)-\(index)-r\(epoch)"
            let ini = try await api.initFrameUpload(sessionId: serverId, FrameInitUploadJSON(meta: rec.meta, parts: infos, idempotencyKey: key))
            if ini.state.uppercased() == "DONE" { return .ok }
            for (n, d) in parts {
                let got = ini.received?[n] ?? 0
                for (off, len) in ChunkPlanner.plan(total: d.count, received: got, chunk: chunk) {
                    try await api.putFramePart(sessionId: serverId, frameIndex: index, part: n, offset: off,
                                               chunk: d.subdata(in: off..<(off + len)), partSha256: hashes[n]!)
                }
            }
            try await api.completeFrameUpload(sessionId: serverId, frameIndex: index)
            return .ok
        } catch let e as APIError {
            if e.status == 0 || e.status == 408 || e.status == 429 || e.status >= 500 { return .transient("[\(e.status)] \(e.message)") }
            return .fatal("[\(e.status)] \(e.message)")
        } catch let e as URLError {
            return .transient(e.localizedDescription)
        } catch {
            return .fatal(error.localizedDescription)  // 파일 없음·디코드 실패 등
        }
    }
}
