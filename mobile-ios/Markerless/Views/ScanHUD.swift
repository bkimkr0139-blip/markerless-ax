// ScanHUD.swift — 스캔·검수 화면을 가리지 않는 작은 보조 UI 모음
//
// 숫자보다 그림: 상단 작은 썸네일(스캔된 점이 채워지는 모습 + 둘레 방향 지도), 한 줄 어시스트(다음에 어디로 움직일지),
// 중심 미세조정 패드. 전부 눈(👁) 버튼 하나로 숨기고 다시 볼 수 있다 (설정은 기억).

import SwiftUI
import simd

// MARK: - 숨기기/보이기 토글

struct HudEyeButton: View {
    @Binding var hidden: Bool

    var body: some View {
        Button {
            withAnimation(.snappy(duration: 0.2)) { hidden.toggle() }
            UIImpactFeedbackGenerator(style: .soft).impactOccurred()
        } label: {
            Image(systemName: hidden ? "eye.slash.fill" : "eye.fill")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(hidden ? Color.orange : Color.primary)
                .frame(width: 38, height: 38)
                .background(.thinMaterial, in: Circle())
        }
        .accessibilityLabel(hidden ? "안내 보이기" : "안내 숨기기")
    }
}

// MARK: - 둘레 돔 지도 (24방위 × 3고도 띠)

/// 위에서 본 지도: 위쪽 = 대상을 지정한 시작 위치. 바깥 고리 = 옆에서, 가운데 고리 = 비스듬히, 안쪽 원 = 위에서 내려다본 칸.
/// 회색 = 비었음, 주황 = 채우는 중, 초록 = 채워짐. 흰 점 = 지금 카메라, 노란 테두리 = 다음 목표.
struct DomeMap: View {
    let coverage: ScanCoverage
    var camSector: Int?
    var targetSector: Int?

    var body: some View {
        Canvas { ctx, size in
            let n = ScanCoverage.sectors
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            let R = min(size.width, size.height) / 2 - 1.5
            // 안쪽 반경(고리 경계): [안쪽 원 0~0.36R] [비스듬 0.36~0.68R] [옆 0.68~1.0R]
            let edges: [CGFloat] = [0.0, 0.36 * R, 0.68 * R, R]
            let bandRadii: [(CGFloat, CGFloat)] = [(edges[2], edges[3]), (edges[1], edges[2]), (edges[0], edges[1])]  // band0=바깥, 1=가운데, 2=안쪽
            func color(_ f: Float) -> Color {
                f >= 1 ? Color.green.opacity(0.9) : (f > 0 ? Color.orange.opacity(0.35 + 0.5 * Double(f)) : Color(white: 0.35, opacity: 0.55))
            }
            for band in 0..<ScanCoverage.bands {
                let (r0, r1) = bandRadii[band]
                for s in 0..<n {
                    let a0 = Angle.degrees(Double(s) / Double(n) * 360 - 90 + 0.8)
                    let a1 = Angle.degrees(Double(s + 1) / Double(n) * 360 - 90 - 0.8)
                    var p = Path()
                    p.addArc(center: c, radius: r1 - 0.7, startAngle: a0, endAngle: a1, clockwise: false)
                    if r0 > 0.5 { p.addArc(center: c, radius: r0 + 0.7, startAngle: a1, endAngle: a0, clockwise: true) }
                    else { p.addLine(to: c) }
                    p.closeSubpath()
                    ctx.fill(p, with: .color(color(coverage.fill(band: band, sector: s))))
                }
            }
            // 다음 목표 방위 강조
            if let t = targetSector {
                let a0 = Angle.degrees(Double(t) / Double(n) * 360 - 90)
                let a1 = Angle.degrees(Double(t + 1) / Double(n) * 360 - 90)
                var p = Path()
                p.addArc(center: c, radius: R, startAngle: a0, endAngle: a1, clockwise: false)
                p.addArc(center: c, radius: edges[1], startAngle: a1, endAngle: a0, clockwise: true)
                p.closeSubpath()
                ctx.stroke(p, with: .color(.yellow), lineWidth: 1.6)
            }
            // 시작 위치 표시(▲)
            var tri = Path()
            tri.move(to: CGPoint(x: c.x, y: c.y - R - 0.5))
            tri.addLine(to: CGPoint(x: c.x - 3, y: c.y - R + 4)); tri.addLine(to: CGPoint(x: c.x + 3, y: c.y - R + 4)); tri.closeSubpath()
            ctx.fill(tri, with: .color(.white.opacity(0.9)))
            // 지금 카메라
            if let cs = camSector {
                let ang = (Double(cs) + 0.5) / Double(n) * 2 * .pi - .pi / 2
                let pt = CGPoint(x: c.x + CGFloat(cos(ang)) * (R - 4), y: c.y + CGFloat(sin(ang)) * (R - 4))
                ctx.fill(Path(ellipseIn: CGRect(x: pt.x - 3.2, y: pt.y - 3.2, width: 6.4, height: 6.4)), with: .color(.white))
                ctx.stroke(Path(ellipseIn: CGRect(x: pt.x - 3.2, y: pt.y - 3.2, width: 6.4, height: 6.4)), with: .color(.black.opacity(0.6)), lineWidth: 0.8)
            }
        }
    }
}

// MARK: - 상단 스캔 썸네일

/// 스캔된 점이 채워지는 모습(위에서 본 점 이미지) + 둘레 지도. 탭하면 크게/작게.
struct ScanThumbnail: View {
    let data: ScanSessionController.ThumbData
    var targetSector: Int?
    @Binding var expanded: Bool

    var body: some View {
        HStack(spacing: 6) {
            if expanded {
                VStack(spacing: 1) {
                    TopViewDots(points: data.points, cam: data.cam)
                        .frame(width: 84, height: 84)
                        .background(Color.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 6))
                    Text("스캔된 모습").font(.system(size: 8)).foregroundStyle(.secondary)
                }
            }
            VStack(spacing: 1) {
                DomeMap(coverage: data.coverage, camSector: data.camSector, targetSector: targetSector)
                    .frame(width: expanded ? 84 : 52, height: expanded ? 84 : 52)
                Text("돈 방향").font(.system(size: 8)).foregroundStyle(.secondary)
            }
        }
        .padding(6)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
        .onTapGesture { withAnimation(.snappy(duration: 0.2)) { expanded.toggle() } }
        .accessibilityLabel("스캔 진행 썸네일")
    }
}

/// 대상 중심 기준 위에서 본 점 이미지 (높이에 따라 파랑→주황). 흰 삼각형 = 카메라 위치.
struct TopViewDots: View {
    let points: [SIMD3<Float>]
    let cam: SIMD3<Float>

    var body: some View {
        Canvas { ctx, size in
            let half = min(size.width, size.height) / 2
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            var reach: Float = 0.2
            for p in points { reach = max(reach, simd_length(SIMD2<Float>(p.x, p.z))) }
            reach = min(reach, 1.0) * 1.12
            let ys = points.map(\.y)
            let y0 = ys.min() ?? 0, y1 = max(ys.max() ?? 0.1, y0 + 0.05)
            let k = half / CGFloat(reach)
            for p in points {
                let t = Double((p.y - y0) / (y1 - y0))
                let col = Color(hue: 0.62 - 0.5 * t, saturation: 0.85, brightness: 0.95)
                let pt = CGPoint(x: c.x + CGFloat(p.x) * k, y: c.y + CGFloat(p.z) * k)
                if pt.x < 0 || pt.y < 0 || pt.x > size.width || pt.y > size.height { continue }
                ctx.fill(Path(CGRect(x: pt.x - 0.9, y: pt.y - 0.9, width: 1.8, height: 1.8)), with: .color(col))
            }
            // 중심 십자
            var cross = Path()
            cross.move(to: CGPoint(x: c.x - 4, y: c.y)); cross.addLine(to: CGPoint(x: c.x + 4, y: c.y))
            cross.move(to: CGPoint(x: c.x, y: c.y - 4)); cross.addLine(to: CGPoint(x: c.x, y: c.y + 4))
            ctx.stroke(cross, with: .color(.teal), lineWidth: 1)
            // 카메라 (가장자리로 클램프)
            var cp = CGPoint(x: c.x + CGFloat(cam.x) * k, y: c.y + CGFloat(cam.z) * k)
            let dx = cp.x - c.x, dy = cp.y - c.y, d = max(1, (dx * dx + dy * dy).squareRoot())
            if d > half - 5 { cp = CGPoint(x: c.x + dx / d * (half - 5), y: c.y + dy / d * (half - 5)) }
            ctx.fill(Path(ellipseIn: CGRect(x: cp.x - 3, y: cp.y - 3, width: 6, height: 6)), with: .color(.white))
        }
    }
}

// MARK: - 한 줄 어시스트

struct AssistChip: View {
    let assist: ScanCoverage.Assist

    private var tint: Color {
        switch assist.level {
        case .ok: return .green
        case .info: return .cyan
        case .warn: return .orange
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: assist.symbol).font(.system(size: 16, weight: .semibold)).foregroundStyle(tint)
            Text(assist.text).font(.footnote.weight(.semibold)).foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.thinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(tint.opacity(0.7), lineWidth: 1))
        .padding(.horizontal, 10)
        .transition(.opacity)
    }
}

// MARK: - 중심 미세조정 패드

/// 화면 기준 위/아래/왼/오른쪽, 깊이(안/바깥)로 대상 중심을 옮긴다. 마커를 보면서 사물 한가운데에 맞춘다.
struct NudgePad: View {
    var onMove: (_ right: Float, _ up: Float, _ fwd: Float) -> Void
    var onConfirm: () -> Void
    var confirmed: Bool
    @State private var big = false

    private var step: Float { big ? 0.03 : 0.01 }

    private func arrow(_ symbol: String, _ r: Float, _ u: Float, _ f: Float) -> some View {
        Button { onMove(r * step, u * step, f * step); UIImpactFeedbackGenerator(style: .light).impactOccurred() } label: {
            Image(systemName: symbol).font(.system(size: 15, weight: .bold)).frame(width: 38, height: 34)
                .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    var body: some View {
        VStack(spacing: 6) {
            Text("중심 보정 — 마커가 사물 한가운데에 오게").font(.caption2.weight(.semibold))
            HStack(spacing: 10) {
                VStack(spacing: 3) {
                    arrow("chevron.up", 0, 1, 0)
                    HStack(spacing: 3) { arrow("chevron.left", -1, 0, 0); arrow("chevron.down", 0, -1, 0); arrow("chevron.right", 1, 0, 0) }
                }
                VStack(spacing: 3) {
                    arrow("arrow.up.and.line.horizontal.and.arrow.down", 0, 0, 1).overlay(Text("안").font(.system(size: 8)).offset(y: 12))
                    arrow("minus", 0, 0, -1).overlay(Text("바깥").font(.system(size: 8)).offset(y: 12))
                }
            }
            HStack(spacing: 8) {
                Picker("", selection: $big) { Text("1cm").tag(false); Text("3cm").tag(true) }
                    .pickerStyle(.segmented).frame(width: 110)
                Button(action: onConfirm) {
                    Label(confirmed ? "확인됨" : "중심 확인", systemImage: confirmed ? "checkmark.circle.fill" : "checkmark.circle")
                        .font(.caption.weight(.semibold))
                }
                .buttonStyle(.borderedProminent).tint(confirmed ? .green : .blue).controlSize(.small)
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - 단계 알약 (눌러서 전체 안내 펼치기)

struct PhasePill: View {
    let title: String
    let index: Int
    let total: Int
    @Binding var expanded: Bool

    var body: some View {
        Button { withAnimation(.snappy(duration: 0.2)) { expanded.toggle() } } label: {
            HStack(spacing: 6) {
                Text("\(index)/\(total)").font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(.secondary)
                Text(title).font(.caption.weight(.semibold))
                Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.system(size: 9)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(.thinMaterial, in: Capsule())
        }
        .foregroundStyle(.primary)
    }
}
