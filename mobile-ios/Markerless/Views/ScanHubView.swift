// ScanHubView.swift — ② 스캔·등록 탭의 모드 선택 껍데기
//   ① 공간 전체 스캔 / ② 시설물 스캔 → SpaceScanView (같은 파이프라인, mode만 SPACE/FACILITY)
//   ③ 사물 외관 스캔(기존)           → ScanAssetView (변경 없음)
// 한 번에 한 뷰만 살아 있다(switch) → ARView/ARSession 동시 소유 없음. 촬영 중에는 모드 전환을 막는다.

import SwiftUI

struct ScanHubView: View {
    enum Mode: Int { case space = 0, facility, object }

    @EnvironmentObject var app: AppState
    @AppStorage("scanHubMode") private var modeRaw = Mode.object.rawValue
    @StateObject private var space = SpaceScanController()  // 모드 ①② 공통, 모드 ③으로 갔다 와도 상태 유지

    private var mode: Mode { Mode(rawValue: modeRaw) ?? .object }
    private var locked: Bool { space.isActive || space.state == .paused }

    var body: some View {
        VStack(spacing: 0) {
            Picker("스캔 모드", selection: Binding(get: { modeRaw }, set: { modeRaw = $0 })) {
                Text("① 공간 전체").tag(Mode.space.rawValue)
                Text("② 시설물").tag(Mode.facility.rawValue)
                Text("③ 사물 외관").tag(Mode.object.rawValue)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12).padding(.vertical, 6)
            .disabled(locked)
            .overlay(alignment: .bottom) { if locked { Text("촬영 중에는 모드를 바꿀 수 없습니다").font(.caption2).foregroundStyle(.secondary).offset(y: 12) } }

            switch mode {
            case .space, .facility:
                SpaceScanView(ctl: space)
                    .onAppear { space.mode = (mode == .facility) ? "FACILITY" : "SPACE" }
                    .onChange(of: modeRaw) { _, _ in space.mode = (mode == .facility) ? "FACILITY" : "SPACE" }
            case .object:
                ScanAssetView()
            }
        }
    }
}
