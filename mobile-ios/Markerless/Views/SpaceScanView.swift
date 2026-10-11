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
                Spacer()
                qualityPill
                hud
                hintLine
                uploadCard
                dock
            }
            .padding(.horizontal, 10).padding(.top, 6).padding(.bottom, 8)
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

    // MARK: - 커버리지 HUD (방위 링: 바깥=벽, 가운데=천장, 안쪽=바닥. 위쪽이 지금 보는 방향)

    private var hud: some View {
        HStack(alignment: .center, spacing: 12) {
            CoverageRing(grid: ctl.coverage, heading: ctl.heading)
                .frame(width: 120, height: 120)
            VStack(alignment: .leading, spacing: 4) {
                bar("벽", ctl.coverage.wallFraction, .green)
                bar("천장", ctl.coverage.ceilingFraction, .cyan)
                bar("바닥", ctl.coverage.floorFraction, .yellow)
                if ctl.lastExcluded > 0 { Text("제외한 프레임 \(ctl.lastExcluded)장").font(.caption2).foregroundStyle(.white.opacity(0.8)) }
            }
            .padding(10).background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            Spacer(minLength: 0)
        }
        .opacity(ctl.state == .idle && ctl.keyframeCount == 0 ? 0.7 : 1)
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
                .font(.caption2.monospacedDigit().weight(.semibold))
                .padding(.horizontal, 10).padding(.vertical, 5).background(.black.opacity(0.6), in: Capsule())
            }
        }
    }

    private func bar(_ name: String, _ f: Float, _ tint: Color) -> some View {
        HStack(spacing: 6) {
            Text(name).font(.caption2.weight(.semibold)).foregroundStyle(.white).frame(width: 28, alignment: .leading)
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.2)).frame(width: 90, height: 6)
                Capsule().fill(tint).frame(width: 90 * CGFloat(f), height: 6)
            }
            Text("\(Int(f * 100))%").font(.caption2.monospacedDigit()).foregroundStyle(.white)
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
        HStack(alignment: .top, spacing: 14) {
            switch ctl.state {
            case .idle:
                Button { ctl.begin(app: app) } label: { DockIcon(symbol: "record.circle", caption: "촬영 시작", tint: .red, active: true) }
                    .disabled(!canStart).opacity(canStart ? 1 : 0.4)
                structureButton
            case .scanning, .recovery:
                Button { ctl.pause() } label: { DockIcon(symbol: "pause.circle", caption: "일시정지") }
                Button { ctl.excludeRecent() } label: { DockIcon(symbol: "person.fill.xmark", caption: "구간 제외", tint: .orange, active: true) }
                Button { ctl.finish() } label: { DockIcon(symbol: "checkmark.circle.fill", caption: "촬영 완료", tint: .green, active: true) }
            case .paused:
                Button { ctl.resume() } label: { DockIcon(symbol: "play.circle.fill", caption: "이어서", tint: .red, active: true) }
                Button { ctl.excludeRecent() } label: { DockIcon(symbol: "person.fill.xmark", caption: "구간 제외", tint: .orange, active: true) }
                structureButton
                Button { ctl.finish() } label: { DockIcon(symbol: "checkmark.circle.fill", caption: "촬영 완료", tint: .green, active: true) }
            case .processing:
                DockIcon(symbol: "gearshape.2", caption: "처리 중")
            case .ready, .needMoreCapture, .failed:
                if ctl.state == .failed {
                    Button { ctl.retryUpload() } label: { DockIcon(symbol: "arrow.clockwise", caption: "다시 시도") }
                }
                Button { ctl.startOver() } label: {
                    DockIcon(symbol: "plus.viewfinder", caption: ctl.state == .needMoreCapture ? "추가 촬영" : "새 촬영", tint: .teal, active: true)
                }
            }
        }
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

// MARK: - 방위 링

struct CoverageRing: View {
    let grid: SpaceCoverageGrid
    let heading: Float

    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            let R = min(size.width, size.height) / 2
            let n = SpaceCoverageGrid.sectors
            // 바깥 = 벽(1), 가운데 = 천장(2), 안쪽 = 바닥(0)
            let rings: [(band: Int, r0: CGFloat, r1: CGFloat, base: Color)] = [
                (1, R * 0.70, R * 0.98, .green), (2, R * 0.44, R * 0.66, .cyan), (0, R * 0.18, R * 0.40, .yellow),
            ]
            ctx.fill(Path(ellipseIn: CGRect(x: c.x - R, y: c.y - R, width: R * 2, height: R * 2)), with: .color(.black.opacity(0.45)))
            for ring in rings {
                for s in 0..<n {
                    // 위쪽 = 지금 보는 방향: 섹터 각도에서 heading을 뺀다 (0 = 위, 시계방향)
                    let a0 = (Double(s) / Double(n)) * 2 * .pi - Double(heading) - .pi / 2 + 0.02
                    let a1 = (Double(s + 1) / Double(n)) * 2 * .pi - Double(heading) - .pi / 2 - 0.02
                    var p = Path()
                    p.addArc(center: c, radius: ring.r1, startAngle: .radians(a0), endAngle: .radians(a1), clockwise: false)
                    p.addArc(center: c, radius: ring.r0, startAngle: .radians(a1), endAngle: .radians(a0), clockwise: true)
                    p.closeSubpath()
                    let f = Double(grid.fill(band: ring.band, sector: s))
                    ctx.fill(p, with: .color(f > 0 ? ring.base.opacity(0.25 + 0.7 * f) : Color.red.opacity(0.28)))
                }
            }
            var needle = Path()
            needle.move(to: c); needle.addLine(to: CGPoint(x: c.x, y: c.y - R))
            ctx.stroke(needle, with: .color(.white), lineWidth: 2)
        }
        .accessibilityLabel("커버리지 링")
    }
}
