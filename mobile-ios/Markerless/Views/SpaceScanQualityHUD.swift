// SpaceScanQualityHUD.swift — 공간/시설물 스캔 화면의 품질 모니터링 HUD 조각 (ScanQualityViews.swift 위에 얹는다)
//   SpaceScanUI.specs    : 링 반경 규격 (벽 바깥 / 천장 가운데 / 바닥 안쪽)
//   SpaceQualityHUD      : 방위 링(≈110pt) + 벽/천장/바닥 진행 칩
//   SpaceEdgeGuide       : 격자에서 계산한 가장자리 화살표 (재스캔 목표 > 가장 가까운 문제 칸)
//   SpaceReviewScreen    : 완료 전 점검(ScanReviewView) 래퍼 + 닫기

import SwiftUI

enum SpaceScanUI {
    static let specs = [
        RingSpec(ring: 1, r0: 0.70, r1: 0.98, name: "벽"),
        RingSpec(ring: 2, r0: 0.44, r1: 0.66, name: "천장"),
        RingSpec(ring: 0, r0: 0.18, r1: 0.40, name: "바닥"),
    ]
}

private struct ProgressChip: View {
    let name: String
    let value: Float
    var body: some View {
        HStack(spacing: 5) {
            ZStack {
                Circle().stroke(.white.opacity(0.22), lineWidth: 3)
                Circle().trim(from: 0, to: CGFloat(max(0.02, value)))
                    .stroke(value >= 1 ? CellState.great.color : (value >= 0.6 ? CellState.ok.color : CellState.low.color),
                            style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }.frame(width: 16, height: 16)
            Text(name).font(.system(size: 10, weight: .semibold))
            Text("\(Int((value * 100).rounded()))%").font(.system(size: 10, weight: .bold).monospacedDigit())
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(.black.opacity(0.55), in: Capsule())
    }
}

struct SpaceQualityHUD: View {
    @ObservedObject var ctl: SpaceScanController

    private var targetCell: (ring: Int, sector: Int)? {
        if let t = ctl.target, let s = t.next(in: ctl.grid) { return (t.ring, s) }
        return nil
    }

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            QualityRing(grid: ctl.grid, specs: SpaceScanUI.specs, headingRad: ctl.heading, fovDeg: 62,
                        target: targetCell, percent: ctl.overallProgress, caption: "%")
                .frame(width: 110, height: 110)
            VStack(alignment: .leading, spacing: 4) {
                ProgressChip(name: "벽", value: ctl.grid.progress(ring: 1))
                ProgressChip(name: "천장", value: ctl.grid.progress(ring: 2))
                ProgressChip(name: "바닥", value: ctl.grid.progress(ring: 0))
                if ctl.lastExcluded > 0 { Text("제외 \(ctl.lastExcluded)장").font(.system(size: 9)).foregroundStyle(.white.opacity(0.8)) }
            }
            Spacer(minLength: 0)
        }
        .opacity(ctl.state == .idle && ctl.keyframeCount == 0 ? 0.7 : 1)
    }
}

/// 재스캔 목표 또는 가장 가까운 문제 칸을 가리키는 화살표. 시작 직후(진행 <25%)에는 모든 칸이 비어 있어 의미가 없으므로 숨긴다.
struct SpaceEdgeGuide: View {
    @ObservedObject var ctl: SpaceScanController

    var body: some View {
        let active = (ctl.state == .scanning) && (ctl.target != nil || ctl.overallProgress >= 0.25)
        let cell = active ? ctl.guideCell : nil
        let v = SpaceGuide.vertical(for: cell, viewRing: ctl.viewRing)
        let vertical: EdgeGuide.Vertical = v == .up ? .up : (v == .down ? .down : .none)
        let delta: Float? = (cell != nil && v == .none)
            ? GuideMath.deltaDeg(headingRad: ctl.heading, targetSector: cell!.sector, sectors: ctl.grid.sectors) : nil
        EdgeGuide(deltaDeg: delta, vertical: vertical,
                  label: cell.map { "\(SpaceGridMap.ringNames[$0.ring]) \($0.sector + 1)번" } ?? "")
    }
}

struct SpaceReviewScreen: View {
    @ObservedObject var ctl: SpaceScanController
    var title: String

    private static func stableID(_ d: ProblemDescriptor) -> UUID {
        // 같은 문제면 같은 id — 헤딩 갱신으로 화면이 다시 그려져도 선택이 풀리지 않는다
        UUID(uuid: (UInt8(d.ring), UInt8(d.sector), UInt8(d.length), UInt8(d.severity.rawValue), 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    }

    private var descriptors: [ProblemDescriptor] { ctl.reviewItems }

    var body: some View {
        let ds = descriptors
        let items = ds.map { ReviewItem(id: Self.stableID($0), title: $0.title, detail: $0.detail, severity: $0.severity, ring: $0.ring, sector: $0.sector) }
        ZStack(alignment: .topTrailing) {
            ScanReviewView(title: title, grid: ctl.grid, specs: SpaceScanUI.specs, percent: ctl.overallProgress, items: items,
                           onRescan: { it in
                               let d = it.flatMap { i in ds.first { $0.ring == i.ring && $0.sector == i.sector } }
                               ctl.rescan(d)
                           },
                           onProceed: { ctl.finish() })
            Button { ctl.rescan(nil) } label: {
                Image(systemName: "xmark.circle.fill").font(.system(size: 28)).foregroundStyle(.white.opacity(0.85))
            }
            .padding(.top, 10).padding(.trailing, 14)
            .accessibilityLabel("점검 닫고 계속 촬영")
        }
    }
}
