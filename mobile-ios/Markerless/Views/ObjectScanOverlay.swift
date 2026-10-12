// ObjectScanOverlay.swift — 사물 스캔 화면용 품질 UI 도우미 (공통 ScanQualityViews.swift 는 수정하지 않고 여기서 확장)

import SwiftUI

extension RingSpec {
    /// 사물 스캔 링 지도: 점검 격자(2링) 기준 — 링 0 = 둘레(옆·비스듬 합산, 바깥), 링 1 = 윗면(안쪽)
    static let object = [
        RingSpec(ring: 0, r0: 0.58, r1: 0.98, name: "둘레"),
        RingSpec(ring: 1, r0: 0.10, r1: 0.44, name: "윗면"),
    ]
}

extension ReviewItem {
    init(_ d: ScanCoverage.ProblemDesc) {
        self.init(title: d.title, detail: d.detail, severity: d.kind, ring: d.ring, sector: d.start)
    }
}

/// 짧게 떴다 사라지는 안내 알약
struct ScanToast: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "checkmark.circle.fill")
            .font(.footnote.weight(.semibold)).foregroundStyle(.white)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(CellState.ok.color.opacity(0.9), in: Capsule())
            .shadow(color: .black.opacity(0.3), radius: 4)
            .transition(.scale.combined(with: .opacity))
    }
}
