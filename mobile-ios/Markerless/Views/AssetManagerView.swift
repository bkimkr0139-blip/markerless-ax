// AssetManagerView.swift — 자산 목록 관리: 선택(검수용) / 재등록 / 단건·선택·전체 삭제
//
// - ② 스캔·등록 우상단 [자산 관리]: onPick=nil (관리 전용)
// - ③ 검수 [자산 선택]: onPick 지정 — 탭하면 선택하고 닫힘. [편집]으로 삭제 모드 전환
// - 행을 오른쪽으로 밀면 [재등록](② 스캔·등록으로 이동, 같은 ID로 저장 시 덮어씀), 왼쪽으로 밀면 [삭제]

import SwiftUI

struct AssetManagerView: View {
    @EnvironmentObject var app: AppState
    @Environment(\.dismiss) private var dismiss

    /// 검수 화면에서 자산을 고를 때 사용 (nil이면 관리 전용)
    var onPick: ((String) -> Void)?
    /// 삭제된 자산 ID들을 상위 화면에 알린다 (선택 중이던 자산 정리용)
    var onDeleted: (([String]) -> Void)?

    @State private var assets: [AssetOut] = []
    @State private var selection = Set<String>()
    @State private var editing = false
    @State private var loading = false
    @State private var busy = false
    @State private var error: String?
    @State private var pendingDelete: [String] = []
    @State private var confirmDelete = false

    var body: some View {
        List(selection: $selection) {
            if let error { Text(error).foregroundStyle(.red).font(.footnote) }

            if !app.isSpaceSelected {
                Text("공간이 선택되지 않았습니다 — 홈 탭에서 공간을 먼저 선택하세요").foregroundStyle(.secondary)
            } else if assets.isEmpty && !loading && error == nil {
                Text("등록된 자산이 없습니다 — ② 스캔·등록에서 사물을 스캔해 등록하세요").foregroundStyle(.secondary)
            }

            ForEach(assets) { a in
                row(a)
                    .tag(a.assetId)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) { ask([a.assetId]) } label: { Label("삭제", systemImage: "trash") }
                    }
                    .swipeActions(edge: .leading) {
                        Button { reregister(a.assetId) } label: { Label("재등록", systemImage: "arrow.triangle.2.circlepath") }
                            .tint(.blue)
                    }
            }
        }
        .environment(\.editMode, .constant(editing ? .active : .inactive))
        .navigationTitle(onPick != nil ? "자산 선택" : "자산 관리")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if !assets.isEmpty {
                    Button(editing ? "완료" : "편집") {
                        editing.toggle()
                        selection.removeAll()
                    }
                }
            }
            ToolbarItemGroup(placement: .bottomBar) {
                if editing {
                    Button(selection.count == assets.count ? "선택 해제" : "전체 선택") {
                        selection = selection.count == assets.count ? [] : Set(assets.map(\.assetId))
                    }
                    Spacer()
                    Button("선택 삭제 (\(selection.count))", role: .destructive) { ask(Array(selection)) }
                        .disabled(selection.isEmpty || busy)
                    Spacer()
                    Button("전체 삭제 (\(assets.count))", role: .destructive) { ask(assets.map(\.assetId)) }
                        .disabled(busy)
                }
            }
        }
        .confirmationDialog(
            "자산 \(pendingDelete.count)개를 삭제할까요?",
            isPresented: $confirmDelete, titleVisibility: .visible
        ) {
            Button("삭제", role: .destructive) { Task { await delete(pendingDelete) } }
            Button("취소", role: .cancel) { pendingDelete = [] }
        } message: {
            Text("모델 파일과 측정 기록도 함께 삭제되며 되돌릴 수 없습니다.\n\(pendingDelete.prefix(5).joined(separator: ", "))\(pendingDelete.count > 5 ? " 외 \(pendingDelete.count - 5)개" : "")")
        }
        .overlay { if loading || busy { ProgressView() } }
        .task { await load() }
        .refreshable { await load() }
        .onChange(of: app.dataRevision) { _, _ in Task { await load() } }  // 관리자 수정(이름·보관·삭제·재스캔)이 바로 보인다
    }

    // MARK: - 행

    @ViewBuilder
    private func row(_ a: AssetOut) -> some View {
        let content = VStack(alignment: .leading, spacing: 2) {
            Text(a.name ?? a.assetId).bold()
            if let n = a.name, n != a.assetId { Text(a.assetId).font(.caption2.monospaced()).foregroundStyle(.secondary) }
            Text(subtitle(a)).font(.caption).foregroundStyle(.secondary)
            Label(scanInfo(a), systemImage: a.geo == nil ? "location.slash" : "location.fill")
                .font(.caption2).foregroundStyle(a.geo == nil ? Color.orange : Color.blue)
        }
        if let onPick, !editing {
            Button {
                onPick(a.assetId)
                dismiss()
            } label: { content.frame(maxWidth: .infinity, alignment: .leading) }
            .buttonStyle(.plain)
        } else {
            content
        }
    }

    /// "2026-10-07 21:09 · 37.123456, 127.123456 (±8m)" — 스캔 일시와 GPS
    private func scanInfo(_ a: AssetOut) -> String {
        var when = "스캔 시각 정보 없음"
        if let s = a.scannedAt, let d = Self.parseISO(s) {
            let f = DateFormatter(); f.locale = Locale(identifier: "ko_KR"); f.dateFormat = "yyyy-MM-dd HH:mm:ss"
            when = f.string(from: d)
        }
        guard let g = a.geo else { return when + " · GPS 없음" }
        let acc = g.accuracyM.map { String(format: " (±%.0fm)", $0) } ?? ""
        return when + String(format: " · %.6f, %.6f", g.lat, g.lon) + acc
    }

    private static func parseISO(_ s: String) -> Date? {
        let a = ISO8601DateFormatter(); a.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return a.date(from: s) ?? ISO8601DateFormatter().date(from: s)
    }

    private func subtitle(_ a: AssetOut) -> String {
        var parts: [String] = []
        if let box = a.aabb, box.minM.count == 3, box.maxM.count == 3 {
            let w = (box.maxM[0] - box.minM[0]) * 100, h = (box.maxM[1] - box.minM[1]) * 100, d = (box.maxM[2] - box.minM[2]) * 100
            parts.append(String(format: "%.0f×%.0f×%.0f cm", w, h, d))
        }
        parts.append("rev \(a.bimRevision)")
        return parts.joined(separator: " · ")
    }

    // MARK: - 동작

    private func load() async {
        guard app.isSpaceSelected else { assets = []; return }
        loading = true
        defer { loading = false }
        do {
            assets = try await app.api.assetsInSpace(spaceNumericId: app.spaceNumericId)
            error = nil
            selection = selection.filter { id in assets.contains { $0.assetId == id } }
        } catch {
            DebugLog.error("assets", "자산 목록 실패: \(error.localizedDescription)")
            self.error = error.localizedDescription
        }
    }

    private func ask(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        pendingDelete = ids
        confirmDelete = true
    }

    private func delete(_ ids: [String]) async {
        busy = true
        defer { busy = false; pendingDelete = [] }
        do {
            let out = ids.count == 1
                ? { try await app.api.deleteAsset(assetId: ids[0]); return BulkDeleteOutJSON(deleted: ids, notFound: []) }
                : { try await app.api.bulkDeleteAssets(ids: ids) }
            let res = try await out()
            DebugLog.log("assets", "삭제 \(res.deleted.count)개", extra: ["ids": res.deleted.joined(separator: ","),
                                                                         "notFound": res.notFound.joined(separator: ",")])
            onDeleted?(res.deleted)
            selection.removeAll()
            if !res.notFound.isEmpty { error = "이미 없는 자산: \(res.notFound.joined(separator: ", "))" }
            await load()
            if assets.isEmpty { editing = false }
        } catch {
            DebugLog.error("assets", "삭제 실패: \(error.localizedDescription)")
            self.error = "삭제 실패: \(error.localizedDescription)"
        }
    }

    private func reregister(_ id: String) {
        DebugLog.log("assets", "재등록 시작 \(id)")
        app.reregisterAssetId = id
        app.selectedTab = 1
        dismiss()
    }
}
