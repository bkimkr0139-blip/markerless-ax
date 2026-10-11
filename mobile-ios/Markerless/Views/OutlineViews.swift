// OutlineViews.swift — 4방향(앞·오른쪽·뒤·왼쪽) 외형 선화 + 방향별 면 확보 표시

import SwiftUI

struct FourViewStrip: View {
    let views: [String: OutlineJSON.ViewOutline]
    /// 방향별로 실제 복원된 면이 있는가 (서버 등록 품질 평가 sideCoveredFlags)
    let coveredFlags: [String: Bool]

    private let order: [(key: String, label: String)] = [
        ("front", "앞"), ("right", "오른쪽"), ("back", "뒤"), ("left", "왼쪽"),
    ]

    var coveredCount: Int { order.filter { coveredFlags[$0.key] ?? false }.count }

    var body: some View {
        // 네 칸이 같은 축척이 되도록 가장 큰 변 기준
        let maxDim = max(views.values.map { max($0.w, $0.h) }.max() ?? 0.2, 0.05)
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                ForEach(order, id: \.key) { item in
                    let covered = coveredFlags[item.key] ?? false
                    VStack(spacing: 2) {
                        Canvas { ctx, size in
                            guard let v = views[item.key] else { return }
                            let scale = (min(size.width, size.height) - 8) / CGFloat(maxDim)
                            func pt(_ x: Double, _ y: Double) -> CGPoint {
                                CGPoint(x: size.width / 2 + CGFloat(x) * scale, y: size.height / 2 - CGFloat(y) * scale)
                            }
                            var path = Path()
                            for s in v.segments where s.count == 4 {
                                path.move(to: pt(s[0], s[1]))
                                path.addLine(to: pt(s[2], s[3]))
                            }
                            ctx.stroke(path, with: .color(covered ? .green : .orange), lineWidth: 1)
                        }
                        .frame(width: 64, height: 64)
                        .background(Color.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(covered ? Color.green : Color.orange, lineWidth: 1))
                        Text("\(item.label) \(covered ? "✓" : "✗")")
                            .font(.caption2).foregroundStyle(covered ? .green : .orange)
                    }
                }
            }
            if coveredCount < 3 {
                Text("⚠️ \(coveredCount)방향 면만 확보됨 — 빠진 방향(주황)을 더 스캔한 뒤 [다시 분리]를 누르세요. 한쪽만 스캔하면 다른 방향에서 검수할 때 모델이 맞지 않습니다")
                    .font(.caption2).foregroundStyle(.orange)
            } else if coveredCount < 4 {
                Text("1방향이 부족합니다 — 가능하면 4방향을 모두 스캔하세요").font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}
