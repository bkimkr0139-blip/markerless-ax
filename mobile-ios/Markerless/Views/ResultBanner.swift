// ResultBanner.swift — 판정 결과 오버레이 카드 (decision 색상 + 편차 + 사유)

import SwiftUI

struct ResultBanner: View {
    let result: ResultJSON
    /// 접기 버튼 동작 (nil이면 버튼 없음)
    var onHide: (() -> Void)?

    private var color: Color {
        switch result.decision {
        case "PASS": return .green
        case "FAIL": return .red
        case "REVIEW": return .orange
        default: return .yellow
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: iconName)
                    .font(.title2)
                VStack(alignment: .leading) {
                    Text(result.decisionKorean).font(.headline)
                    Text("신뢰도: \(result.confidence)").font(.caption)
                }
                Spacer()
            }
            .foregroundStyle(color)
            .overlay(alignment: .topTrailing) {
                if let onHide {
                    Button(action: onHide) {
                        Label("숨기기", systemImage: "chevron.down.circle.fill")
                            .labelStyle(.iconOnly).font(.title3)
                    }
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("결과 카드 숨기기")
                }
            }

            Divider()

            if let fit = result.margins?.shapeFit {
                // 월드맵 미복원: 위치 편차 대신 저장 모델과의 형상 일치도
                HStack {
                    devItem("일치", fit.inlierRatio * 100, "%")
                    devItem("잔차", fit.rmseMm, "mm")
                }
            } else {
                HStack {
                    devItem("X", result.deviation.dxMm, "mm")
                    devItem("Y", result.deviation.dyMm, "mm")
                    devItem("Z", result.deviation.dzMm, "mm")
                    devItem("회전", result.deviation.maxRotDeg, "°")
                }
            }

            ForEach(result.reasons ?? [], id: \.code) { r in
                Text("· \(r.message)").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14).strokeBorder(color, lineWidth: 2)
        )
        .padding(.horizontal, 16)
    }

    private var iconName: String {
        switch result.decision {
        case "PASS": return "checkmark.seal.fill"
        case "FAIL": return "xmark.octagon.fill"
        case "REVIEW": return "exclamationmark.triangle.fill"
        default: return "arrow.clockwise.circle.fill"
        }
    }

    private func devItem(_ label: String, _ value: Double, _ unit: String) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text("\(value >= 0 ? "+" : "")\(String(format: "%.1f", value))")
                .font(.system(.body, design: .monospaced)).bold()
                .foregroundStyle(abs(value) >= 0.001 ? .primary : .secondary)
            Text(unit).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

/// 접힌 결과 — 판정 색 + 핵심 수치 한 줄. 누르면 다시 펼친다.
struct ResultPill: View {
    let result: ResultJSON
    let onShow: () -> Void

    private var color: Color {
        switch result.decision {
        case "PASS": return .green
        case "FAIL": return .red
        case "REVIEW": return .orange
        default: return .yellow
        }
    }

    private var summary: String {
        if let fit = result.margins?.shapeFit { return "일치 \(Int((fit.inlierRatio * 100).rounded()))%" }
        return String(format: "%.0fmm · %.1f°", result.deviation.distMm, result.deviation.maxRotDeg)
    }

    var body: some View {
        Button(action: onShow) {
            HStack(spacing: 6) {
                Circle().fill(color).frame(width: 10, height: 10)
                Text("\(result.decision) · \(summary)").font(.caption.bold())
                Image(systemName: "chevron.up").font(.caption2)
            }
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(.regularMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(color, lineWidth: 1.5))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("결과 카드 펼치기")
    }
}
