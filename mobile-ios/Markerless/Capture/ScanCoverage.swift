// ScanCoverage.swift — 대상 사물 둘레 스캔 진행도(돔 지도)와 카메라 워크 어시스트 (UI·ARKit 무관, Mac에서 단독 시험 가능)
//
// 대상 중심(POI)을 먼저 정하고 나면, 카메라가 그 사물을 어느 방향·높이에서 비췄는지를 24방위 × 3고도 격자에 쌓는다.
//  - 방위 0 = 대상을 지정한 카메라 위치(시작 위치), 방위가 늘어나면 사물을 보는 사람이 '왼쪽'으로 도는 방향
//  - 고도 띠: 0 = 옆(<25°), 1 = 비스듬(25~55°), 2 = 위(>55°, 내려다봄)
//  - 한 칸은 사물을 향해 적당한 거리(0.22~2.6m)에서 표본 3개 이상, 평균 품질 0.5 이상이면 '양호'(QualityGrid)
// 이 격자로 "어느 방향이 비었는지 / 너무 빠른지 / 너무 먼지"를 한 줄 어시스트로 알려 준다.

import Foundation
import simd

struct ScanCoverage {
    static let sectors = 24
    static let bands = 3
    static let need = 3  // 한 칸이 '양호'가 되는 최소 표본 수 (QualityGrid.need)
    static let sectorDeg: Float = 360 / Float(sectors)

    /// 품질 인식 격자: 링 0 = 옆(<25°), 1 = 비스듬(25~55°), 2 = 위(>55°) × 24방위. 칸 상태는 '몇 번 + 어떤 품질로' 찍혔나.
    var grid = QualityGrid(rings: bands, sectors: sectors, need: need)
    /// 시작 위치의 방위각(rad). 이 방향이 지도의 위쪽(방위 0)
    var origin: Float = 0
    var originSet = false

    struct Assist {
        enum Level { case ok, info, warn }
        var level: Level
        var symbol: String
        var text: String
        var targetSector: Int? = nil  // 지도에서 강조할 다음 목표 칸(방위)
    }

    struct Pose {
        var azimuth: Float  // 시작 위치 기준 0..<2π (왼쪽으로 돌면 증가)
        var sector: Int
        var elevationDeg: Float
        var band: Int
        var distance: Float
        var lookDot: Float  // 카메라 시선이 대상 중심을 향하는 정도 (1 = 정면)
    }

    // MARK: - 자세 계산

    static func azimuthRaw(rel: SIMD3<Float>) -> Float {
        var a = atan2(rel.x, -rel.z)  // 0 = 대상의 −Z쪽에서 바라봄
        if a < 0 { a += 2 * .pi }
        return a
    }

    func pose(camPos: SIMD3<Float>, camFwd: SIMD3<Float>, center: SIMD3<Float>) -> Pose {
        let rel = camPos - center
        let dh = simd_length(SIMD2<Float>(rel.x, rel.z))
        let dist = simd_length(rel)
        var az = Self.azimuthRaw(rel: rel) - origin
        while az < 0 { az += 2 * .pi }
        while az >= 2 * .pi { az -= 2 * .pi }
        let el = atan2(rel.y, max(dh, 1e-4)) * 180 / .pi
        let band = el < 25 ? 0 : (el < 55 ? 1 : 2)
        let look = dist > 1e-4 ? simd_dot(simd_normalize(camFwd), simd_normalize(-rel)) : 0
        let sec = min(Self.sectors - 1, Int(az / (2 * .pi) * Float(Self.sectors)))
        return Pose(azimuth: az, sector: sec, elevationDeg: el, band: band, distance: dist, lookDot: look)
    }

    // MARK: - 누적

    /// 대상을 지정한 순간의 카메라 위치를 지도의 '시작(위)'으로 삼는다
    mutating func setOrigin(camPos: SIMD3<Float>, center: SIMD3<Float>) {
        origin = Self.azimuthRaw(rel: camPos - center)
        originSet = true
    }

    /// 이웃 방위에 주는 표본의 품질 가중 (본 방위의 70%) — 부드럽게 돌면 칸이 끊기지 않고 이어서 채워진다
    static let neighbourWeight: Float = 0.7
    /// 방위 칸 안에서 경계에 이만큼(칸 폭의 30%) 가까우면 그쪽 이웃에도 표본을 준다
    static let neighbourEdge: Float = 0.3
    /// 대상을 이만큼 이상 향해야(시선·중심 내적) 표본으로 인정
    static let minLookDot: Float = 0.7

    /// 이 프레임의 표본 품질: LiveQuality(사물 프로필) 점수 × 조준 정확도(lookDot 0.6→0, 1.0→1)
    struct Sample {
        var pose: Pose
        var live: LiveQuality.Output  // 조준을 반영하지 않은 촬영 상태 (실시간 바 표시용)
        var quality: Float  // 격자에 넣는 값
        var usable: Bool  // 거리·조준 조건을 만족해 격자에 넣을 수 있나
    }

    /// base: 속도·흔들림·깊이 신뢰도·밝기·추적 (distanceM은 여기서 채운다)
    func evaluate(camPos: SIMD3<Float>, camFwd: SIMD3<Float>, center: SIMD3<Float>, base: LiveQuality.Input) -> Sample {
        let p = pose(camPos: camPos, camFwd: camFwd, center: center)
        var inp = base
        inp.distanceM = p.distance
        let live = LiveQuality.evaluate(inp, profile: .object)
        let aim = max(0, min(1, (p.lookDot - 0.6) / 0.4))
        let usable = p.distance >= 0.22 && p.distance <= 2.6 && p.lookDot > Self.minLookDot
        return Sample(pose: p, live: live, quality: live.score * aim, usable: usable)
    }

    /// 표본을 격자에 넣는다. 반환: 어떤 칸이든 '양호' 이상으로 올라갔나 (촉각용)
    @discardableResult
    mutating func record(_ s: Sample) -> Bool {
        guard s.usable else { return false }
        let p = s.pose
        var up = false
        func put(_ sector: Int, _ q: Float) {
            let r = grid.add(ring: p.band, sector: sector, quality: q)
            if r.old < .ok && r.new >= .ok { up = true }
        }
        put(p.sector, s.quality)
        let frac = p.azimuth / (2 * .pi) * Float(Self.sectors) - Float(p.sector)
        if frac < Self.neighbourEdge { put((p.sector + Self.sectors - 1) % Self.sectors, s.quality * Self.neighbourWeight) }
        else if frac > 1 - Self.neighbourEdge { put((p.sector + 1) % Self.sectors, s.quality * Self.neighbourWeight) }
        return up
    }

    /// 표본 하나 추가 (기본: 깨끗한 촬영 조건). 새로 '양호'가 된 칸이 있으면 true
    @discardableResult
    mutating func add(camPos: SIMD3<Float>, camFwd: SIMD3<Float>, center: SIMD3<Float>, base: LiveQuality.Input = LiveQuality.Input()) -> Bool {
        record(evaluate(camPos: camPos, camFwd: camFwd, center: center, base: base))
    }

    mutating func reset() { self = ScanCoverage() }

    // MARK: - 조회

    func fill(band: Int, sector: Int) -> Float { grid.fill(band, sector) }
    /// '양호' 이상 (충분한 표본 + 평균 품질 0.5 이상)
    func isFilled(band: Int, sector: Int) -> Bool { grid.state(band, sector) >= .ok }

    /// 옆·비스듬(띠 0·1)에서 한 번이라도 채워진 방위 수 — 둘레를 얼마나 돌았나
    var azimuthFilled: Int { (0..<Self.sectors).filter { isFilled(band: 0, sector: $0) || isFilled(band: 1, sector: $0) }.count }
    /// 위에서 내려다본(띠 2) 방위 비율
    var topFraction: Float { Float((0..<Self.sectors).filter { isFilled(band: 2, sector: $0) }.count) / Float(Self.sectors) }
    var missingSectors: [Int] { (0..<Self.sectors).filter { !(isFilled(band: 0, sector: $0) || isFilled(band: 1, sector: $0)) } }
    /// 분리 확인을 눌러도 되는 최소 수준 (둘레의 절반 이상)
    var canReview: Bool { azimuthFilled >= 12 }
    /// 충분히 한 바퀴 돌았나
    var complete: Bool { azimuthFilled >= 22 }

    // MARK: - 어시스트 (한 줄)

    /// 지금 가장 도움이 되는 한 가지 행동. 우선순위: 추적 → 속도 → 거리 → 조준 → 빈 방위 → 윗면 → 완료
    func assist(camPos: SIMD3<Float>, camFwd: SIMD3<Float>, center: SIMD3<Float>, tracking: Bool, tooFast: Bool) -> Assist {
        let p = pose(camPos: camPos, camFwd: camFwd, center: center)
        if !tracking {
            return Assist(level: .warn, symbol: "exclamationmark.triangle.fill", text: "추적이 불안정합니다 — 천천히, 무늬가 있는 곳을 함께 비추세요")
        }
        if tooFast {
            return Assist(level: .warn, symbol: "tortoise.fill", text: "천천히 움직이세요 — 빠르면 기록되지 않습니다")
        }
        if p.distance > 1.7 {
            return Assist(level: .info, symbol: "arrow.down.right.and.arrow.up.left", text: "사물에 더 가까이 — 0.5~1m가 좋습니다")
        }
        if p.distance < 0.28 {
            return Assist(level: .info, symbol: "arrow.up.left.and.arrow.down.right", text: "너무 가깝습니다 — 조금 뒤로 물러나세요")
        }
        if p.lookDot < 0.8 {
            return Assist(level: .info, symbol: "scope", text: "십자선을 대상 중심(마커) 쪽으로 향하게 하세요")
        }
        if complete {
            if topFraction >= 0.2 {
                return Assist(level: .ok, symbol: "checkmark.circle.fill", text: "충분합니다 — [분리 확인]을 누르세요")
            }
            return Assist(level: .info, symbol: "arrow.up.circle.fill", text: "위에서 내려다보며 윗면도 훑어 주세요")
        }
        // 현재 서 있는 방위가 아직 비었으면 이동하지 말고 잠시 더 비추게 한다 (칸이 채워지는 중)
        let missing = Set(missingSectors)
        if missing.contains(p.sector) {
            return Assist(level: .ok, symbol: "viewfinder", text: "좋습니다 — 이 방향을 잠시 더 비추세요", targetSector: p.sector)
        }
        // 현재 방위에서 가장 가까운 빈 방위 (양쪽 탐색)
        for off in 1...(Self.sectors / 2) {
            for sign in [1, -1] {
                let s = ((p.sector + sign * off) % Self.sectors + Self.sectors) % Self.sectors
                if missing.contains(s) {
                    let deg = off * Int(Self.sectorDeg)
                    // 방위가 늘어나는 쪽 = 사물을 보는 사람의 왼쪽
                    let left = sign > 0
                    return Assist(level: .info, symbol: left ? "arrow.turn.up.left" : "arrow.turn.up.right",
                                  text: "\(left ? "왼쪽" : "오른쪽")으로 약 \(deg)° 돌아 이동하며 비추세요", targetSector: s)
                }
            }
        }
        return Assist(level: .info, symbol: "arrow.triangle.2.circlepath", text: "사물 둘레를 천천히 한 바퀴 돌며 비추세요")
    }
}

// MARK: - 점검(분리 확인 전)·재스캔·화살표 안내
//
// 점검용 격자(reviewGrid)는 2링: 링 0 = 둘레(옆+비스듬 합산 — 사람은 한 방위를 옆이든 비스듬이든 한 번만 잘 찍으면 되므로),
// 링 1 = 윗면(위에서 내려다봄). 링 0/1 를 따로 빨갛게 두면 "옆 띠가 항상 비어 있음"으로 오해되기 때문이다.
// 방위 번호는 시작 위치(0)에서 사람의 '왼쪽'으로 늘어난다 → 90° 왼쪽, 180° 뒤, 270° 오른쪽.

extension ScanCoverage {
    static let reviewAzimuthRing = 0
    static let reviewTopRing = 1
    /// 윗면이 이 비율 미만이면 점검 목록에 '윗면'이 오르고, 재스캔은 이 비율(+여유)을 넘으면 만족
    static let topListBelow: Float = 0.2
    static let topSatisfiedAt: Float = 0.3

    var reviewGrid: QualityGrid {
        var g = QualityGrid(rings: 2, sectors: Self.sectors, need: Self.need)
        for s in 0..<Self.sectors {
            let a = grid.cell(0, s), b = grid.cell(1, s)
            g.cells[g.idx(0, s)] = QualityCell(frames: a.frames + b.frames, sumQ: a.sumQ + b.sumQ)
            g.cells[g.idx(1, s)] = grid.cell(2, s)
        }
        return g
    }

    /// 둘레(옆·비스듬) '양호' 이상 비율 0...1
    var azimuthProgress: Float { reviewGrid.progress(ring: 0) }
    var topProgress: Float { reviewGrid.progress(ring: 1) }

    /// 분리 확인 전에 점검 화면을 띄워야 하나: 둘레 90% 미만이거나 품질 낮은 둘레 칸이 있을 때
    func needsReview(threshold: Float = 0.9) -> Bool {
        let g = reviewGrid
        return g.progress(ring: 0) < threshold || g.count(.low, ring: 0) > 0
    }

    /// 방위 구간(시작 섹터, 길이) → 사람이 알아듣는 면 이름. 중심 각도 기준 8방향.
    static func sideName(start: Int, length: Int) -> String {
        let center = (Float(start) + Float(length) / 2) * sectorDeg
        let deg = center.truncatingRemainder(dividingBy: 360)
        let idx = Int(((deg + 22.5) / 45).rounded(.down)) % 8  // 0=앞 1=왼앞 2=왼 3=왼뒤 4=뒤 5=오른뒤 6=오른 7=오른앞
        return ["앞면", "왼쪽 앞면", "왼쪽 면", "왼쪽 뒷면", "뒷면", "오른쪽 뒷면", "오른쪽 면", "오른쪽 앞면"][idx]
    }

    struct ProblemDesc: Equatable {
        var ring: Int   // 점검 격자 링 (0 둘레, 1 윗면)
        var start: Int
        var length: Int
        var kind: CellState  // .missing / .low
        var title: String    // "뒷면 · 비어 있음"
        var detail: String
    }

    /// QualityGrid.problems() 를 점검 격자에서 뽑아 한국어 제목을 붙인다. 윗면은 20% 미만일 때 한 줄로.
    func problemDescs() -> [ProblemDesc] {
        let g = reviewGrid
        var out: [ProblemDesc] = []
        for p in g.problems() where p.ring == 0 {
            let name = Self.sideName(start: p.start, length: p.length)
            out.append(ProblemDesc(ring: 0, start: p.start, length: p.length, kind: p.kind,
                                   title: "\(name) · \(p.kind.label)",
                                   detail: "약 \(Int(Self.sectorDeg) * p.length)° 구간"
                                       + (p.kind == .low ? " — 흔들리거나 멀리서 찍혀 다시 비추면 좋습니다" : " — 이 방향에서 비춘 적이 없습니다")))
        }
        if g.progress(ring: 1) < Self.topListBelow {
            let seen = (0..<Self.sectors).contains { g.cell(1, $0).frames > 0 }
            out.append(ProblemDesc(ring: 1, start: 0, length: Self.sectors, kind: seen ? .low : .missing,
                                   title: "윗면 · \(seen ? CellState.low.label : CellState.missing.label)",
                                   detail: "위에서 내려다보며 윗면을 한 번 훑어 주세요"))
        }
        return out
    }

    // MARK: 재스캔 목표

    struct RescanTarget: Equatable {
        var ring: Int        // 점검 격자 링
        var sectors: [Int]
        var title: String
    }

    func rescanTarget(for d: ProblemDesc) -> RescanTarget {
        RescanTarget(ring: d.ring, sectors: (0..<d.length).map { (d.start + $0) % Self.sectors }, title: d.title)
    }

    /// 아직 부족한 목표 섹터. 윗면은 30% 이상 확보되면 모두 만족으로 본다.
    func remaining(_ t: RescanTarget) -> [Int] {
        let g = reviewGrid
        if t.ring == Self.reviewTopRing, g.progress(ring: 1) >= Self.topSatisfiedAt { return [] }
        return t.sectors.filter { g.state(t.ring, $0) < .ok }
    }
    func isSatisfied(_ t: RescanTarget) -> Bool { remaining(t).isEmpty }

    /// 재스캔 시작: '품질 낮음'이던 칸은 옛 낮은 평균이 새 표본을 가리지 않도록 비운다 (비어 있던 칸은 그대로).
    mutating func prepareRescan(_ t: RescanTarget) {
        let g = reviewGrid
        for s in t.sectors where g.state(t.ring, s) == .low {
            if t.ring == Self.reviewTopRing { grid.clear(ring: 2, sector: s) }
            else { grid.clear(ring: 0, sector: s); grid.clear(ring: 1, sector: s) }
        }
    }

    // MARK: 화살표

    struct Guidance: Equatable {
        var ring: Int       // 점검 격자 링 (링 지도 맥박 위치)
        var sector: Int
        var deltaSectors: Int  // + = 방위 번호가 늘어나는 쪽 = 사람의 왼쪽
        var up: Bool        // 윗면: 위에서 내려다보기
        /// EdgeGuide 용 부호 있는 각도(°): + = 화면 오른쪽, − = 왼쪽. 번호가 늘어나는 쪽이 왼쪽이므로 부호가 뒤집힌다.
        var edgeDeg: Float? { up || deltaSectors == 0 ? nil : -Float(GuideMath.degrees(delta: deltaSectors, sectors: ScanCoverage.sectors)) }
    }

    /// 지금 카메라 섹터에서 가장 가까운 부족 칸 안내. 재스캔 목표가 있으면 그것만 본다.
    func guidance(camSector: Int, rescan: RescanTarget?) -> Guidance? {
        let g = reviewGrid
        let n = Self.sectors
        if let t = rescan {
            let rem = remaining(t)
            guard !rem.isEmpty else { return nil }
            if t.ring == Self.reviewTopRing { return Guidance(ring: 1, sector: camSector, deltaSectors: 0, up: true) }
            var best = rem[0], bd = Int.max
            for s in rem {
                var d = ((s - camSector) % n + n) % n
                if d > n / 2 { d -= n }
                if abs(d) < abs(bd) || bd == Int.max { best = s; bd = d }
            }
            return Guidance(ring: 0, sector: best, deltaSectors: bd, up: false)
        }
        // 둘레를 어느 정도 돈 뒤에만 자동 안내 (처음엔 어차피 전부 비어 있어 화살표가 의미 없다)
        guard g.progress(ring: 0) >= 0.4 else { return nil }
        if let p = g.nearestProblem(from: camSector, rings: [0]) {
            return Guidance(ring: 0, sector: p.sector, deltaSectors: p.delta, up: false)
        }
        // 둘레는 빠진 곳이 없고 윗면만 비었다
        if g.progress(ring: 1) < Self.topListBelow { return Guidance(ring: 1, sector: camSector, deltaSectors: 0, up: true) }
        return nil
    }
}

// MARK: - 링 지도 목표 · 요약

extension ScanCoverage {
    /// 링 지도에서 맥박칠 칸(점검 격자 좌표). 재스캔 목표가 있으면 그 중 가장 가까운 칸, 없으면 가장 가까운 빈/낮은 칸(둘레 우선, 없으면 윗면).
    func ringTarget(camSector: Int, rescan: RescanTarget?) -> (ring: Int, sector: Int)? {
        if let t = rescan, let g = guidance(camSector: camSector, rescan: t) { return (g.ring, g.sector) }
        if rescan != nil { return nil }
        let g = reviewGrid
        if let p = g.nearestProblem(from: camSector, rings: [0]) { return (0, p.sector) }
        if g.progress(ring: 1) < Self.topListBelow, let p = g.nearestProblem(from: camSector, rings: [1]) { return (1, p.sector) }
        return nil
    }

    /// 전체 표본의 평균 품질 (표본이 없으면 nil)
    var meanQuality: Float? {
        let f = grid.cells.reduce(0) { $0 + $1.frames }
        guard f > 0 else { return nil }
        return grid.cells.reduce(0) { $0 + $1.sumQ } / Float(f)
    }

    /// 스캔 종료 시 디버그 로그 한 줄: 링별 진행 %, 문제 수, 평균 품질
    func summaryLine() -> String {
        func pct(_ x: Float) -> Int { Int((x * 100).rounded()) }
        let mq = meanQuality.map { String(format: "%.2f", $0) } ?? "-"
        return "스캔 종료 점검: 옆 \(pct(grid.progress(ring: 0)))% · 비스듬 \(pct(grid.progress(ring: 1)))% · 위 \(pct(grid.progress(ring: 2)))%"
            + " · 둘레(합산) \(pct(azimuthProgress))% · 문제 \(problemDescs().count)건 · 평균 품질 \(mq)"
    }
}
