// ScanQualityViews.swift — 스캔 품질·누락 모니터링 공통 UI (공간 스캔·사물 스캔이 함께 사용)
//
//  QualityRing        : 얼굴/지문 등록처럼 둘러 채우는 링 지도. 칸 색 = 상태(비어 있음 빨강 / 품질 낮음 주황 / 양호 초록 / 좋음 민트).
//                       지금 보는 방향 쐐기, 다음 목표 칸 맥박, 가운데 진행률.
//  LiveQualityBar     : 지금 이 순간의 촬영 상태(좋음/주의/나쁨) + "왜"(너무 빠름·가까움·어두움…) + 바로 할 행동.
//  EdgeGuide          : 빠진 곳이 화면 밖에 있으면 화면 가장자리에 방향 화살표와 각도.
//  ScanReviewView     : 완료 직전 점검 — 펼친 지도 + 문제 목록(누락/품질 낮음) + [부족한 곳 다시 스캔] / [그대로 진행].
//  ScanHaptics        : 칸이 채워질 때·품질이 나빠질 때·완료 때 촉각 (연타 방지).

import SwiftUI

// MARK: - 색·문구

extension CellState {
    var color: Color {
        switch self {
        case .missing: return Color(red: 1.0, green: 0.27, blue: 0.23)
        case .low: return Color(red: 1.0, green: 0.62, blue: 0.04)
        case .ok: return Color(red: 0.20, green: 0.78, blue: 0.35)
        case .great: return Color(red: 0.25, green: 0.95, blue: 0.70)
        }
    }
    var symbol: String { ["exclamationmark.circle.fill", "exclamationmark.triangle.fill", "checkmark.circle.fill", "checkmark.seal.fill"][rawValue] }
}

extension QualityLevel {
    var color: Color { [Color(red: 1.0, green: 0.27, blue: 0.23), Color(red: 1.0, green: 0.62, blue: 0.04), Color(red: 0.20, green: 0.78, blue: 0.35)][rawValue] }
    var title: String { ["촬영 상태 나쁨", "촬영 상태 주의", "촬영 상태 좋음"][rawValue] }
    var symbol: String { ["xmark.octagon.fill", "exclamationmark.triangle.fill", "checkmark.circle.fill"][rawValue] }
}

/// 링 하나의 반경 범위(0...1 비율)와 이름. 격자 링 번호와 1:1.
struct RingSpec: Identifiable {
    var id: Int { ring }
    var ring: Int
    var r0: CGFloat
    var r1: CGFloat
    var name: String
}

// MARK: - 링 지도

struct QualityRing: View {
    let grid: QualityGrid
    let specs: [RingSpec]
    /// 지금 시선 방위(rad). 주면 링이 돌아 '위쪽 = 지금 보는 방향'이 된다. nil이면 고정 지도(위 = 기준 방향).
    var headingRad: Float? = nil
    /// 고정 지도에서 지금 카메라가 있는 섹터(점 표시)
    var viewSector: Int? = nil
    var fovDeg: Double = 62
    var target: (ring: Int, sector: Int)? = nil
    /// 방위가 반시계(왼쪽)로 늘어나는 좌표계(사물 둘레)면 true
    var mirror = false
    var percent: Float? = nil
    var caption: String? = nil

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 24)) { tl in
            let pulse = 0.5 + 0.5 * sin(tl.date.timeIntervalSinceReferenceDate * 5)
            Canvas { ctx, size in draw(&ctx, size, pulse: pulse) }
        }
        .overlay(centerLabel)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("스캔 커버리지 \(Int((percent ?? 0) * 100))퍼센트")
    }

    private func angle(_ s: Int, _ frac: Double) -> Double {
        let n = Double(grid.sectors)
        let base = (Double(s) + frac) / n * 2 * .pi
        let h = Double(headingRad ?? 0)
        let signed = mirror ? -(base - h) : (base - h)
        return signed - .pi / 2
    }

    private func draw(_ ctx: inout GraphicsContext, _ size: CGSize, pulse: Double) {
        let c = CGPoint(x: size.width / 2, y: size.height / 2)
        let R = min(size.width, size.height) / 2 - 2
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - R, y: c.y - R, width: R * 2, height: R * 2)), with: .color(.black.opacity(0.5)))
        let gap = 0.018
        for sp in specs {
            for s in 0..<grid.sectors {
                let st = grid.state(sp.ring, s)
                let f = Double(grid.fill(sp.ring, s))
                var a0 = angle(s, 0) + gap, a1 = angle(s, 1) - gap
                if mirror { swap(&a0, &a1) }
                var p = Path()
                p.addArc(center: c, radius: R * sp.r1, startAngle: .radians(a0), endAngle: .radians(a1), clockwise: false)
                p.addArc(center: c, radius: R * sp.r0, startAngle: .radians(a1), endAngle: .radians(a0), clockwise: true)
                p.closeSubpath()
                let op: Double
                switch st {
                case .missing: op = 0.46
                case .low: op = 0.45 + 0.4 * f
                case .ok: op = 0.85
                case .great: op = 0.95
                }
                ctx.fill(p, with: .color(st.color.opacity(op)))
                if st == .missing { ctx.stroke(p, with: .color(st.color.opacity(0.9)), style: StrokeStyle(lineWidth: 0.8, dash: [2, 2])) }
                if let t = target, t.ring == sp.ring, t.sector == s {
                    ctx.stroke(p, with: .color(.white.opacity(0.55 + 0.45 * pulse)), lineWidth: 2.2)
                    ctx.fill(p, with: .color(.white.opacity(0.10 + 0.22 * pulse)))
                }
            }
        }
        // 지금 보는 방향 쐐기(위쪽 중심, 시야각)
        if headingRad != nil {
            var w = Path()
            let half = fovDeg / 2 * .pi / 180
            w.move(to: c)
            w.addArc(center: c, radius: R, startAngle: .radians(-.pi / 2 - half), endAngle: .radians(-.pi / 2 + half), clockwise: false)
            w.closeSubpath()
            ctx.fill(w, with: .color(.white.opacity(0.13)))
            var l = Path()
            l.move(to: c); l.addLine(to: CGPoint(x: c.x, y: c.y - R))
            ctx.stroke(l, with: .color(.white), lineWidth: 2)
        } else if let vs = viewSector {
            let a = angle(vs, 0.5)
            let pt = CGPoint(x: c.x + CGFloat(cos(a)) * (R - 3), y: c.y + CGFloat(sin(a)) * (R - 3))
            ctx.fill(Path(ellipseIn: CGRect(x: pt.x - 4, y: pt.y - 4, width: 8, height: 8)), with: .color(.white))
            ctx.stroke(Path(ellipseIn: CGRect(x: pt.x - 4, y: pt.y - 4, width: 8, height: 8)), with: .color(.black.opacity(0.6)), lineWidth: 1)
        }
    }

    @ViewBuilder private var centerLabel: some View {
        if let p = percent {
            VStack(spacing: 0) {
                Text("\(Int((p * 100).rounded()))").font(.system(size: 22, weight: .bold, design: .rounded).monospacedDigit())
                if let cap = caption { Text(cap).font(.system(size: 9, weight: .semibold)).opacity(0.85) }
            }
            .foregroundStyle(.white).shadow(color: .black.opacity(0.7), radius: 2)
        }
    }
}

// MARK: - 실시간 품질 바

struct LiveQualityBar: View {
    let level: QualityLevel
    let score: Float
    let reasons: [QualityReason]
    var distanceM: Float? = nil
    var hint: String? = nil

    private var primary: QualityReason? { reasons.first }

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().stroke(.white.opacity(0.18), lineWidth: 4)
                Circle().trim(from: 0, to: CGFloat(max(0.04, min(1, score))))
                    .stroke(level.color, style: StrokeStyle(lineWidth: 4, lineCap: .round)).rotationEffect(.degrees(-90))
                Image(systemName: primary?.symbol ?? level.symbol).font(.system(size: 14, weight: .bold)).foregroundStyle(level.color)
            }
            .frame(width: 36, height: 36)
            .animation(.easeOut(duration: 0.25), value: score)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(level.title).font(.footnote.weight(.bold)).foregroundStyle(.white)
                    if let d = distanceM { Text(String(format: "%.1fm", d)).font(.caption2.monospacedDigit().weight(.semibold)).foregroundStyle(.white.opacity(0.75)) }
                }
                Text(primary?.advice ?? hint ?? "이대로 천천히 계속하세요").font(.caption).foregroundStyle(.white.opacity(0.9)).lineLimit(2)
            }
            Spacer(minLength: 0)
            if reasons.count > 1 {
                VStack(alignment: .trailing, spacing: 3) {
                    ForEach(Array(reasons.dropFirst().prefix(2)), id: \.self) { r in
                        Label(r.short, systemImage: r.symbol).font(.system(size: 10, weight: .semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 6).padding(.vertical, 2).background(level.color.opacity(0.55), in: Capsule())
                    }
                }
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(.black.opacity(0.62), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(level.color.opacity(level == .good ? 0.35 : 0.9), lineWidth: level == .good ? 1 : 2))
        .animation(.easeOut(duration: 0.2), value: level)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - 화면 가장자리 안내

struct EdgeGuide: View {
    enum Vertical { case none, up, down }
    /// 목표까지 좌우 각도(°, +는 오른쪽). nil이면 안내 없음. 화면 안(±28°)이면 숨김.
    var deltaDeg: Float?
    var vertical: Vertical = .none
    var label: String = ""
    @State private var phase = false

    var body: some View {
        GeometryReader { geo in
            ZStack {
                if let d = deltaDeg, abs(d) > 28 {
                    chevron(right: d > 0, text: "\(abs(Int(d)))°")
                        .position(x: d > 0 ? geo.size.width - 34 : 34, y: geo.size.height * 0.46)
                }
                if vertical != .none {
                    vChevron(up: vertical == .up)
                        .position(x: geo.size.width / 2, y: vertical == .up ? geo.size.height * 0.2 : geo.size.height * 0.62)
                }
            }
            .onAppear { withAnimation(.easeInOut(duration: 0.7).repeatForever(autoreverses: true)) { phase = true } }
        }
        .allowsHitTesting(false)
    }

    private func chevron(right: Bool, text: String) -> some View {
        VStack(spacing: 2) {
            Image(systemName: right ? "chevron.right.2" : "chevron.left.2").font(.system(size: 30, weight: .black))
            Text(text).font(.caption.weight(.bold).monospacedDigit())
            if !label.isEmpty { Text(label).font(.system(size: 9, weight: .semibold)).lineLimit(1) }
        }
        .foregroundStyle(.white).padding(8)
        .background(CellState.missing.color.opacity(0.78), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .shadow(color: .black.opacity(0.4), radius: 4)
        .offset(x: phase ? (right ? 6 : -6) : 0)
    }

    private func vChevron(up: Bool) -> some View {
        VStack(spacing: 0) {
            Image(systemName: up ? "chevron.up.2" : "chevron.down.2").font(.system(size: 26, weight: .black))
            Text(up ? "위를 비추세요" : "아래를 비추세요").font(.caption.weight(.bold))
        }
        .foregroundStyle(.white).padding(.horizontal, 12).padding(.vertical, 6)
        .background(CellState.missing.color.opacity(0.78), in: Capsule())
        .shadow(color: .black.opacity(0.4), radius: 4)
        .offset(y: phase ? (up ? -5 : 5) : 0)
    }
}

// MARK: - 점검 화면

struct ReviewItem: Identifiable, Equatable {
    var id = UUID()
    var title: String
    var detail: String
    var severity: CellState  // .missing 또는 .low
    var ring: Int
    var sector: Int  // 대표(첫) 섹터 — 재스캔 목표
}

/// 링×섹터를 가로로 펼친 지도 (파노라마처럼 한눈에).
struct QualityStrip: View {
    let grid: QualityGrid
    let specs: [RingSpec]
    var mirror = false
    var highlight: (ring: Int, sector: Int)? = nil

    var body: some View {
        VStack(spacing: 3) {
            ForEach(specs) { sp in
                HStack(spacing: 6) {
                    Text(sp.name).font(.system(size: 10, weight: .semibold)).foregroundStyle(.white.opacity(0.85)).frame(width: 30, alignment: .leading)
                    HStack(spacing: 1.5) {
                        ForEach(0..<grid.sectors, id: \.self) { i in
                            let s = mirror ? (grid.sectors - 1 - i) : i
                            RoundedRectangle(cornerRadius: 2.5)
                                .fill(grid.state(sp.ring, s).color.opacity(grid.state(sp.ring, s) == .missing ? 0.35 : 0.92))
                                .overlay(RoundedRectangle(cornerRadius: 2.5).strokeBorder(.white, lineWidth: (highlight?.ring == sp.ring && highlight?.sector == s) ? 1.6 : 0))
                                .frame(height: 20)
                        }
                    }
                }
            }
            HStack(spacing: 10) {
                ForEach([CellState.missing, .low, .ok, .great], id: \.rawValue) { st in
                    HStack(spacing: 3) {
                        RoundedRectangle(cornerRadius: 2).fill(st.color.opacity(st == .missing ? 0.45 : 0.92)).frame(width: 10, height: 10)
                        Text(st.label).font(.system(size: 9)).foregroundStyle(.white.opacity(0.8))
                    }
                }
            }.padding(.top, 4)
        }
    }
}

struct ScanReviewView: View {
    let title: String
    let grid: QualityGrid
    let specs: [RingSpec]
    var mirror = false
    let percent: Float
    /// 이 값 미만이면 [그대로 진행]에 경고 문구
    var minRecommended: Float = 0.8
    let items: [ReviewItem]
    var onRescan: (ReviewItem?) -> Void
    var onProceed: () -> Void
    @State private var selected: ReviewItem.ID?

    private var verdict: (String, String, Color) {
        if items.isEmpty { return ("충분히 스캔됐습니다", "빠진 곳이 없고 품질도 양호합니다", CellState.ok.color) }
        let miss = items.filter { $0.severity == .missing }.count
        let low = items.count - miss
        return ("보강이 필요합니다", "비어 있음 \(miss)곳 · 품질 낮음 \(low)곳", percent >= minRecommended ? CellState.low.color : CellState.missing.color)
    }

    var body: some View {
        ZStack {
            Color.black.opacity(0.88).ignoresSafeArea()
            VStack(spacing: 14) {
                Text(title).font(.headline).foregroundStyle(.white).padding(.top, 8)
                HStack(spacing: 14) {
                    QualityRing(grid: grid, specs: specs, mirror: mirror, percent: percent, caption: "%")
                        .frame(width: 104, height: 104)
                    VStack(alignment: .leading, spacing: 4) {
                        Label(verdict.0, systemImage: items.isEmpty ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                            .font(.subheadline.weight(.bold)).foregroundStyle(verdict.2)
                        Text(verdict.1).font(.caption).foregroundStyle(.white.opacity(0.85))
                        Text("아래 지도에서 빨강·주황 칸을 다시 비추면 채워집니다").font(.caption2).foregroundStyle(.white.opacity(0.6))
                    }
                    Spacer(minLength: 0)
                }
                QualityStrip(grid: grid, specs: specs, mirror: mirror,
                             highlight: items.first { $0.id == selected }.map { ($0.ring, $0.sector) })
                    .padding(10).background(.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(items) { it in
                            Button { selected = it.id } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: it.severity.symbol).foregroundStyle(it.severity.color).font(.system(size: 18))
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(it.title).font(.footnote.weight(.semibold)).foregroundStyle(.white)
                                        Text(it.detail).font(.caption2).foregroundStyle(.white.opacity(0.7))
                                    }
                                    Spacer()
                                    Button { onRescan(it) } label: { Text("여기 다시").font(.caption.weight(.bold)) }
                                        .buttonStyle(.borderedProminent).tint(it.severity.color).controlSize(.small)
                                }
                                .padding(10).background(.white.opacity(selected == it.id ? 0.16 : 0.08), in: RoundedRectangle(cornerRadius: 10))
                            }
                        }
                        if items.isEmpty {
                            Text("문제 없음").font(.footnote).foregroundStyle(.white.opacity(0.7)).padding(20)
                        }
                    }
                }
                VStack(spacing: 8) {
                    if !items.isEmpty {
                        Button { onRescan(items.first { $0.id == selected } ?? items.first) } label: {
                            Label("부족한 곳 다시 스캔", systemImage: "viewfinder").frame(maxWidth: .infinity).padding(.vertical, 6)
                        }.buttonStyle(.borderedProminent).tint(.blue)
                    }
                    Button(action: onProceed) {
                        Label(items.isEmpty ? "다음 단계로" : "그대로 진행", systemImage: "arrow.right.circle").frame(maxWidth: .infinity).padding(.vertical, 6)
                    }
                    .buttonStyle(.bordered).tint(items.isEmpty ? .green : .white)
                    if !items.isEmpty && percent < minRecommended {
                        Text("권장 \(Int(minRecommended * 100))% 미만 — 이대로 진행하면 3D 모델에 구멍이 생기거나 부정확할 수 있습니다")
                            .font(.caption2).foregroundStyle(CellState.low.color).multilineTextAlignment(.center)
                    }
                }
            }
            .padding(16)
        }
    }
}

// MARK: - 촉각

@MainActor
enum ScanHaptics {
    private static var last: [String: Date] = [:]
    private static func ok(_ key: String, _ gap: TimeInterval) -> Bool {
        let now = Date()
        if let t = last[key], now.timeIntervalSince(t) < gap { return false }
        last[key] = now
        return true
    }
    /// 칸이 '양호'로 올라갈 때 가볍게
    static func cellFilled() { if ok("fill", 0.25) { UIImpactFeedbackGenerator(style: .light).impactOccurred(intensity: 0.7) } }
    /// 품질이 나빠질 때 경고
    static func qualityBad() { if ok("bad", 2.0) { UINotificationFeedbackGenerator().notificationOccurred(.warning) } }
    /// 링/구간 완성
    static func complete() { if ok("done", 1.0) { UINotificationFeedbackGenerator().notificationOccurred(.success) } }
}

// MARK: - 시연/검증용 갤러리 (실행 인자 -qualitygallery 로만 열린다 — 시뮬레이터 스크린샷 검증용)

struct QualityKitGallery: View {
    @State private var grid: QualityGrid = {
        var g = QualityGrid(rings: 3, sectors: 24, need: 2)
        for s in 0..<24 {
            let kinds = [0, 0, 3, 3, 3, 2, 2, 1, 1, 3, 3, 3, 3, 0, 3, 3, 2, 3, 3, 3, 1, 3, 3, 3]
            for _ in 0..<[0, 1, 3, 2][kinds[s] % 4] { g.add(ring: 1, sector: s, quality: kinds[s] == 1 ? 0.3 : 0.9) }
            if s % 3 != 0 { for _ in 0..<3 { g.add(ring: 2, sector: s, quality: 0.8) } }
            if s % 2 == 0 { for _ in 0..<2 { g.add(ring: 0, sector: s, quality: 0.6) } }
        }
        return g
    }()
    private let specs = [RingSpec(ring: 1, r0: 0.70, r1: 0.98, name: "벽"), RingSpec(ring: 2, r0: 0.44, r1: 0.66, name: "천장"), RingSpec(ring: 0, r0: 0.18, r1: 0.40, name: "바닥")]

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(white: 0.25), Color(white: 0.1)], startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            ScrollView {
                VStack(spacing: 14) {
                    Text("스캔 품질 모니터링 UI").font(.headline).foregroundStyle(.white)
                    HStack(spacing: 14) {
                        QualityRing(grid: grid, specs: specs, headingRad: 1.2, target: (1, 7), percent: grid.overall(weights: [1, 2, 1]), caption: "%")
                            .frame(width: 150, height: 150)
                        QualityRing(grid: grid, specs: specs, viewSector: 5, percent: 0.42, caption: "%").frame(width: 100, height: 100)
                    }
                    LiveQualityBar(level: .good, score: 0.92, reasons: [], distanceM: 1.8)
                    LiveQualityBar(level: .warn, score: 0.55, reasons: [.tooFast, .blur], distanceM: 2.4)
                    LiveQualityBar(level: .bad, score: 0.15, reasons: [.tooClose, .dark, .lowConfidence], distanceM: 0.2)
                    ZStack { Color.black.opacity(0.4); EdgeGuide(deltaDeg: 110, vertical: .up, label: "벽 7~9번") }.frame(height: 200).clipShape(RoundedRectangle(cornerRadius: 12))
                    QualityStrip(grid: grid, specs: specs, highlight: (1, 7)).padding(10).background(.black.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
                }.padding(16)
            }
        }
    }
}


/// 검증용: `-qualityreview` 실행 인자로 점검 화면을 샘플 데이터로 연다 (시뮬레이터 스크린샷 확인)
struct QualityReviewDemo: View {
    private let specs = [RingSpec(ring: 1, r0: 0.70, r1: 0.98, name: "벽"), RingSpec(ring: 2, r0: 0.44, r1: 0.66, name: "천장"), RingSpec(ring: 0, r0: 0.18, r1: 0.40, name: "바닥")]
    private var grid: QualityGrid {
        var g = QualityGrid(rings: 3, sectors: 24, need: 2)
        for s in 0..<24 {
            if !(7...9).contains(s) && s != 20 { for _ in 0..<3 { g.add(ring: 1, sector: s, quality: 0.85) } }
            if s == 20 { for _ in 0..<2 { g.add(ring: 1, sector: s, quality: 0.25) } }
            if s % 4 != 0 { for _ in 0..<3 { g.add(ring: 2, sector: s, quality: 0.8) } }
            if s < 14 { for _ in 0..<3 { g.add(ring: 0, sector: s, quality: 0.7) } }
        }
        return g
    }
    var body: some View {
        let g = grid
        let items = g.problems().map { p in
            ReviewItem(title: "\(["바닥", "벽", "천장"][p.ring]) 구간 \(p.start + 1)~\(p.start + p.length) · \(p.kind.label)",
                       detail: p.ring == 1 ? "시작 방향에서 오른쪽 \(p.start * 15)° 부근" : "위/아래를 비추세요", severity: p.kind, ring: p.ring, sector: p.start)
        }
        ScanReviewView(title: "스캔 점검", grid: g, specs: specs, percent: g.overall(weights: [1, 2, 1]), items: items, onRescan: { _ in }, onProceed: {})
    }
}
