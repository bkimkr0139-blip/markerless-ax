// SpaceScanView.swift — ② 스캔·등록 탭의 모드 ① 공간 전체 스캔 / ② 시설물 스캔 화면
//
// RGB-D 키프레임을 방 전체에서 모아 서버에서 3D 공간으로 재구성한다 (POI 반경 제한 없음).
// 상태: 대기 → 촬영 중 ↔ 일시정지 / 복구 중 → 처리 중 → 완료 | 추가 촬영 필요 | 실패
// 사람처럼 움직이는 물체는 기기에서 감지할 수 없다 — 찍혔다면 [구간 제외]로 직전 10초를 지운다.

import SwiftUI

struct SpaceScanView: View {
    @EnvironmentObject var app: AppState
    @ObservedObject var ctl: SpaceScanController
    @ObservedObject private var ar: ScanSessionController
    @ObservedObject private var uploader = FrameUploader.shared
    @State private var showRoomPlan = false
    @State private var showNote = false

    init(ctl: SpaceScanController) {
        self.ctl = ctl
        self._ar = ObservedObject(wrappedValue: ctl.ar)
    }

    private var isFacility: Bool { ctl.mode == "FACILITY" }
    private var running: Bool { ctl.state == .scanning || ctl.state == .recovery }
    private var finalStates: Bool { [.ready, .needMoreCapture, .failed].contains(ctl.state) }

    var body: some View {
        ZStack {
            ARScanView(controller: ar).ignoresSafeArea()

            VStack(spacing: 8) {
                topBar
                if !app.isSpaceSelected { banner("홈에서 공간을 먼저 선택하세요", "building.2", .orange) }
                else if !ctl.canCapture { banner("이 기기는 LiDAR 깊이를 지원하지 않아 공간 스캔을 할 수 없습니다", "exclamationmark.triangle.fill", .red) }
                if isFacility && ctl.state == .idle {
                    banner("시설물 모드: 배관·전기반·장비 주변을 0.5~1.5m에서 여러 각도로 비추고, 주변 벽·바닥도 함께 담으세요", "wrench.and.screwdriver", .teal)
                }
                if let n = ctl.notice { banner(n, "thermometer.medium", .yellow) }
                if ctl.state == .recovery { recoveryCard }
                if running { liveBar }
                Spacer()
                qualityPill
                SpaceQualityHUD(ctl: ctl)
                hintLine
                uploadCard
                dock
            }
            .padding(.horizontal, 10).padding(.top, 6).padding(.bottom, 8)

            SpaceEdgeGuide(ctl: ctl).padding(.vertical, 120)
            if let t = ctl.toast {
                Label(t, systemImage: "checkmark.circle.fill").font(.subheadline.weight(.bold)).foregroundStyle(.white)
                    .padding(.horizontal, 16).padding(.vertical, 9)
                    .background(CellState.ok.color.opacity(0.92), in: Capsule())
                    .transition(.scale.combined(with: .opacity))
                    .frame(maxHeight: .infinity, alignment: .center)
            }
        }
        .animation(.easeOut(duration: 0.2), value: ctl.toast)
        .fullScreenCover(isPresented: $ctl.reviewOpen) {
            SpaceReviewScreen(ctl: ctl, title: isFacility ? "시설물 스캔 점검" : "공간 스캔 점검")
        }
        .onAppear { ctl.screenAppeared() }
        .onDisappear { ctl.screenDisappeared() }
        .fullScreenCover(isPresented: $showRoomPlan) {
            RoomPlanPassView { result in
                showRoomPlan = false
                ctl.endStructurePass(result)
            }
        }
    }

    // MARK: - 상단

    private var topBar: some View {
        HStack(spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: ctl.state.symbol)
                Text(ctl.state.label).font(.footnote.weight(.bold))
                if ctl.state == .scanning { Circle().fill(.red).frame(width: 7, height: 7) }
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(ctl.state.color.opacity(0.85), in: Capsule()).foregroundStyle(.white)
            Text("구간 \(ctl.segmentIndex + 1) · \(ctl.keyframeCount)장 · \(Int(ctl.storedMB))MB")
                .font(.caption2.monospacedDigit()).foregroundStyle(.white)
                .padding(.horizontal, 8).padding(.vertical, 5).background(.black.opacity(0.55), in: Capsule())
            Spacer()
            Button { showNote.toggle() } label: {
                Image(systemName: "info.circle").foregroundStyle(.white).shadow(radius: 3)
            }
        }
        .padding(.top, 14)
        .overlay(alignment: .topTrailing) {
            if showNote {
                Text("움직이는 사람은 기기에서 걸러지지 않습니다. 사람이 찍혔으면 [구간 제외]로 직전 10초를 지우세요. 깊이 센서 정확도는 가정하지 않고 서버가 신뢰도로 걸러냅니다.")
                    .font(.caption).padding(10).frame(maxWidth: 280)
                    .background(.black.opacity(0.8), in: RoundedRectangle(cornerRadius: 12)).foregroundStyle(.white)
                    .offset(y: 46)
            }
        }
    }

    private func banner(_ text: String, _ symbol: String, _ tint: Color) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol).foregroundStyle(tint)
            Text(text).font(.footnote).foregroundStyle(.white).frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(10).background(.black.opacity(0.62), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var recoveryCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("복구 중", systemImage: "exclamationmark.arrow.triangle.2.circlepath").font(.footnote.weight(.bold)).foregroundStyle(.orange)
            Text(ctl.recoveryText.isEmpty ? "추적을 되찾는 중입니다" : ctl.recoveryText).font(.footnote).foregroundStyle(.white)
            Text("복구되면 새 구간으로 이어서 기록됩니다").font(.caption2).foregroundStyle(.white.opacity(0.7))
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.35), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.orange, lineWidth: 1))
    }

    // MARK: - 실시간 품질 바 (촬영 중에만; 대기 중엔 숨김)

    private var liveBar: some View {
        let q = ctl.liveQuality
        return LiveQualityBar(level: q?.level ?? .good, score: q?.score ?? 1, reasons: q?.reasons ?? [], distanceM: q?.distanceM)
    }

    // MARK: - 촬영 품질 알약 / 고화질 사진 토글

    private var qualityPill: some View {
        VStack(spacing: 6) {
            if ctl.state == .idle && ctl.highResAvailable {
                Toggle(isOn: Binding(get: { ctl.highRes }, set: { ctl.setHighRes($0) })) {
                    Text("고화질 사진").font(.caption.weight(.semibold)).foregroundStyle(.white)
                }
                .toggleStyle(.switch).fixedSize()
                .padding(.horizontal, 12).padding(.vertical, 6).background(.black.opacity(0.55), in: Capsule())
            }
            if ctl.keyframeCount > 0 || ctl.qualitySharpRatio != nil {
                HStack(spacing: 10) {
                    if let r = ctl.qualitySharpRatio {
                        Label("선명 \(Int(r * 100))%", systemImage: "scope").foregroundStyle(r >= 0.6 ? .green : .orange)
                    }
                    if let c = ctl.qualityMeanConf {
                        Label("깊이 \(String(format: "%.1f", c))/2", systemImage: "cube.transparent").foregroundStyle(c >= 1.2 ? .green : .orange)
                    }
                    if let d = ctl.qualityDenoisedPct {
                        Label("평균 \(Int(d))%", systemImage: "square.stack.3d.down.right").foregroundStyle(.white)
                    }
                    if ctl.highResCount > 0 {
                        Label("\(ctl.highResCount)", systemImage: "camera.aperture").foregroundStyle(.white)
                    }
                }
                .font(.system(size: 10).monospacedDigit().weight(.semibold))
                .padding(.horizontal, 8).padding(.vertical, 3).background(.black.opacity(0.5), in: Capsule())
            }
        }
    }

    private var hintLine: some View {
        Group {
            if running || ctl.state == .paused || ctl.state == .idle {
                Text(ctl.hint).font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.black.opacity(0.65), in: Capsule())
            }
        }
    }

    // MARK: - 업로드 진행

    private var uploadCard: some View {
        Group {
            if let p = ctl.progress {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(p.stage).font(.footnote.weight(.semibold))
                        Spacer()
                        Text("\(p.uploaded)/\(p.total)\(p.failed > 0 ? " · 실패 \(p.failed)" : "")").font(.caption.monospacedDigit())
                    }
                    ProgressView(value: p.total > 0 ? Double(p.uploaded) / Double(p.total) : 0)
                    if let sp = p.serverProgress { ProgressView(value: sp) { Text("서버 \(p.serverStage ?? "")").font(.caption2) } }
                    if let q = p.quality { Text("품질 \(q)" + (p.coverage.map { " · 커버리지 \($0)" } ?? "")).font(.caption) }
                    if let m = p.message, !m.isEmpty { Text(m).font(.caption2).foregroundStyle(.white.opacity(0.8)) }
                    if ctl.state == .needMoreCapture { Text("서버가 더 촬영이 필요하다고 판단했습니다 — [추가 촬영]으로 부족한 곳을 새 세션으로 찍으세요").font(.caption).foregroundStyle(.orange) }
                }
                .foregroundStyle(.white).padding(10)
                .background(.black.opacity(0.65), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
    }

    // MARK: - 하단 도크

    private var canStart: Bool { app.isSpaceSelected && ctl.canCapture }

    private var dock: some View {
        HStack(alignment: .top, spacing: 0) {
            switch ctl.state {
            case .idle:
                Button { ctl.begin(app: app) } label: { DockIcon(symbol: "record.circle", caption: "촬영 시작", tint: .red, active: true) }
                    .disabled(!canStart).opacity(canStart ? 1 : 0.4)
                structureButton
            case .scanning, .recovery:
                Button { ctl.pause() } label: { DockIcon(symbol: "pause.circle", caption: "일시정지") }
                Button { ctl.openReview() } label: { DockIcon(symbol: "checklist", caption: "점검", tint: .cyan, active: true) }
                Button { ctl.excludeRecent() } label: { DockIcon(symbol: "person.fill.xmark", caption: "구간 제외", tint: .orange, active: true) }
                Button { ctl.requestFinish() } label: { DockIcon(symbol: "checkmark.circle.fill", caption: "촬영 완료", tint: .green, active: true) }
            case .paused:
                Button { ctl.resume() } label: { DockIcon(symbol: "play.circle.fill", caption: "이어서", tint: .red, active: true) }
                Button { ctl.excludeRecent() } label: { DockIcon(symbol: "person.fill.xmark", caption: "구간 제외", tint: .orange, active: true) }
                structureButton
                Button { ctl.openReview() } label: { DockIcon(symbol: "checklist", caption: "점검", tint: .cyan, active: true) }
                Button { ctl.requestFinish() } label: { DockIcon(symbol: "checkmark.circle.fill", caption: "촬영 완료", tint: .green, active: true) }
            case .processing:
                DockIcon(symbol: "gearshape.2", caption: "처리 중").frame(maxWidth: .infinity)
            case .ready, .needMoreCapture, .failed:
                if ctl.state == .failed {
                    Button { ctl.retryUpload() } label: { DockIcon(symbol: "arrow.clockwise", caption: "다시 시도") }
                }
                Button { ctl.startOver() } label: {
                    DockIcon(symbol: "plus.viewfinder", caption: ctl.state == .needMoreCapture ? "추가 촬영" : "새 촬영", tint: .teal, active: true)
                }
            }
        }
        .buttonStyle(DockSlotStyle())
        .frame(maxWidth: .infinity)
    }

    /// 구조 스캔(RoomPlan): 지원 기기에서만 보인다. RGB-D 패스와 순차 실행(카메라를 비운 뒤 시작).
    @ViewBuilder private var structureButton: some View {
        if SpaceScanDevice.roomPlanSupported && !isFacility {
            Button {
                ctl.beginStructurePass(app: app)
                showRoomPlan = true
            } label: { DockIcon(symbol: "square.split.bottomrightquarter", caption: "구조 스캔") }
                .disabled(!canStart).opacity(canStart ? 1 : 0.4)
        }
    }
}

/// 도크 버튼마다 같은 폭의 칸을 주어, 버튼 수가 달라도 화면 폭 안에서 고르게 나뉜다 (간격 고정 → 가장자리 잘림 방지)
private struct DockSlotStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(maxWidth: .infinity)
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}
