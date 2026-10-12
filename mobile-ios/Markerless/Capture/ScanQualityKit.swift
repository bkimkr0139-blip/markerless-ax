// ScanQualityKit.swift — 스캔 품질·누락 실시간 모니터링의 공통 순수 로직 (ARKit/UI 무관 → Mac swiftc 하네스로 단독 시험)
//
// 공간 스캔(SpaceScanSession)과 사물 스캔(ScanAssetView)이 함께 쓴다.
//  1) QualityGrid   : 링 × 섹터 격자. 칸마다 "몇 프레임이, 어느 품질로" 찍혔는지 → 상태(누락/낮음/보통/좋음)
//  2) LiveQuality   : 지금 이 프레임의 품질(속도·흔들림·거리·깊이 신뢰도·밝기·추적) → 점수 + 이유(왜 나쁜가)
//  3) LevelFilter   : 품질 표시가 깜빡이지 않게 하는 완화(EMA + 연속 N회 확인)와 '나빠짐' 감지(촉각 알림용)
//  4) 문제 목록·가장 가까운 문제 칸 탐색 : 촬영 중 화살표 안내와 완료 전 점검 화면의 재료
// 얼굴/지문 등록 UI처럼 "채워질 때까지 비추게" 하되, '찍혔다'가 아니라 '잘 찍혔다'를 기준으로 한다.

import Foundation
import simd

// MARK: - 1) 격자

enum CellState: Int, Codable, Comparable {
    case missing = 0  // 한 번도 안 찍힘
    case low = 1      // 찍혔지만 프레임이 부족하거나 품질이 낮음 → 재촬영 권장
    case ok = 2       // 충분
    case great = 3    // 여유 있게 좋음

    static func < (a: CellState, b: CellState) -> Bool { a.rawValue < b.rawValue }
    var label: String { ["비어 있음", "품질 낮음", "양호", "좋음"][rawValue] }
}

struct QualityCell: Codable, Equatable {
    var frames = 0
    var sumQ: Float = 0
    var avg: Float { frames > 0 ? sumQ / Float(frames) : 0 }
}

struct QualityGrid: Codable, Equatable {
    var rings: Int
    var sectors: Int
    /// 한 칸이 '충분'이 되기 위한 최소 프레임 수
    var need: Int
    var cells: [QualityCell]

    /// 상태 판정 기준 (평균 품질)
    static let lowBelow: Float = 0.5
    static let greatAtLeast: Float = 0.75

    init(rings: Int, sectors: Int, need: Int) {
        self.rings = rings; self.sectors = sectors; self.need = max(1, need)
        self.cells = Array(repeating: QualityCell(), count: rings * sectors)
    }

    func idx(_ ring: Int, _ sector: Int) -> Int { ring * sectors + ((sector % sectors) + sectors) % sectors }

    func cell(_ ring: Int, _ sector: Int) -> QualityCell { cells[idx(ring, sector)] }

    func state(_ ring: Int, _ sector: Int) -> CellState {
        let c = cell(ring, sector)
        if c.frames == 0 { return .missing }
        if c.frames < need || c.avg < Self.lowBelow { return .low }
        if c.avg >= Self.greatAtLeast && c.frames >= need + 1 { return .great }
        return .ok
    }

    /// 0...1 — 채워지는 중의 애니메이션/진행 표시용 (프레임 수 기준, 품질 낮으면 상한 0.6)
    func fill(_ ring: Int, _ sector: Int) -> Float {
        let c = cell(ring, sector)
        let base = min(1, Float(c.frames) / Float(need))
        return state(ring, sector) == .low && c.frames >= need ? min(base, 0.6) : base
    }

    /// 표본 하나 추가. (이전 상태, 새 상태) — 상태가 올라가면 촉각·애니메이션
    @discardableResult
    mutating func add(ring: Int, sector: Int, quality: Float) -> (old: CellState, new: CellState) {
        guard ring >= 0, ring < rings, quality.isFinite else { return (.missing, .missing) }
        let i = idx(ring, sector)
        let old = state(ring, sector)
        cells[i].frames += 1
        cells[i].sumQ += max(0, min(1, quality))
        return (old, state(ring, sector))
    }

    /// 한 칸을 비운다 (구간 제외·재스캔 시작)
    mutating func clear(ring: Int, sector: Int) { cells[idx(ring, sector)] = QualityCell() }

    mutating func reset() { cells = Array(repeating: QualityCell(), count: rings * sectors) }

    // MARK: 집계

    /// 링의 '양호 이상' 비율
    func progress(ring: Int) -> Float {
        Float((0..<sectors).filter { state(ring, $0) >= .ok }.count) / Float(sectors)
    }

    /// 링별 가중 평균 진행도 (weights 길이 = rings)
    func overall(weights: [Float]) -> Float {
        let w = weights.count == rings ? weights : Array(repeating: 1, count: rings)
        let tot = w.reduce(0, +)
        guard tot > 0 else { return 0 }
        return (0..<rings).reduce(0) { $0 + progress(ring: $1) * w[$1] } / tot
    }

    func count(_ s: CellState, ring: Int) -> Int { (0..<sectors).filter { state(ring, $0) == s }.count }

    /// 품질이 낮은 칸의 평균 (진단 표시용)
    func meanQuality(ring: Int) -> Float? {
        let seen = (0..<sectors).map { cell(ring, $0) }.filter { $0.frames > 0 }
        guard !seen.isEmpty else { return nil }
        return seen.map(\.avg).reduce(0, +) / Float(seen.count)
    }

    // MARK: 문제 목록

    struct Problem: Equatable {
        var ring: Int
        var start: Int       // 연속 구간의 첫 섹터
        var length: Int      // 연속 섹터 수
        var kind: CellState  // .missing 또는 .low
        /// 구간에 속한 섹터 번호들 (원형이라 격자의 sectors 수가 필요)
        func sectorList(_ total: Int) -> [Int] { (0..<length).map { (start + $0) % total } }
    }

    /// 링마다 연속된 '비어 있음'/'품질 낮음' 구간으로 묶는다 (원형이라 0번과 마지막이 이어지면 합친다).
    func problems() -> [Problem] {
        var out: [Problem] = []
        for r in 0..<rings {
            for kind in [CellState.missing, .low] {
                let bad = (0..<sectors).map { state(r, $0) == kind }
                if bad.allSatisfy({ $0 }) { out.append(Problem(ring: r, start: 0, length: sectors, kind: kind)); continue }
                // 시작점: 처음으로 'bad가 아닌' 칸 다음부터 돌며 구간을 센다
                guard let anchor = bad.firstIndex(of: false) else { continue }
                var i = 1
                while i <= sectors {
                    let s = (anchor + i) % sectors
                    if bad[s] {
                        var len = 0
                        while len < sectors, bad[(s + len) % sectors] { len += 1 }
                        out.append(Problem(ring: r, start: s, length: len, kind: kind))
                        i += len
                    } else { i += 1 }
                }
            }
        }
        return out
    }

    /// 지금 시선 섹터에서 가장 가까운 문제 칸. delta > 0 = 섹터 번호가 늘어나는 쪽.
    func nearestProblem(from sector: Int, preferRing: Int? = nil, kinds: [CellState] = [.missing, .low],
                        rings only: [Int]? = nil) -> (ring: Int, sector: Int, delta: Int)? {
        var best: (ring: Int, sector: Int, delta: Int, cost: Int)?
        for r in (only ?? Array(0..<rings)) {
            for off in 0...(sectors / 2) {
                for sign in (off == 0 ? [1] : [1, -1]) {
                    let s = ((sector + sign * off) % sectors + sectors) % sectors
                    guard kinds.contains(state(r, s)) else { continue }
                    var cost = off * 2 + (state(r, s) == .low ? 1 : 0)
                    if let p = preferRing, p != r { cost += 3 }
                    if best == nil || cost < best!.cost { best = (r, s, sign * off, cost) }
                    break
                }
            }
        }
        return best.map { ($0.ring, $0.sector, $0.delta) }
    }
}

// MARK: - 2) 실시간 품질

struct QualityProfile {
    var idealMin: Float
    var idealMax: Float
    var hardMin: Float
    var hardMax: Float
    /// 이보다 빠르면 점수가 깎이기 시작하는 이동 속도(m/s)·회전 속도(rad/s)
    var softSpeed: Float
    var softAng: Float

    /// 방 전체: 0.5~3.5m가 이상적 (LiDAR 유효 범위 ≈ 5m)
    static let space = QualityProfile(idealMin: 0.6, idealMax: 3.5, hardMin: 0.3, hardMax: 5.0, softSpeed: 0.35, softAng: 0.8)
    /// 사물 하나: 0.4~1.2m
    static let object = QualityProfile(idealMin: 0.4, idealMax: 1.2, hardMin: 0.22, hardMax: 2.4, softSpeed: 0.25, softAng: 0.7)
}

enum QualityReason: String, CaseIterable, Codable {
    case tracking, tooFast, blur, tooClose, tooFar, lowConfidence, sparseDepth, dark

    var symbol: String {
        switch self {
        case .tracking: return "exclamationmark.triangle.fill"
        case .tooFast: return "hare.fill"
        case .blur: return "camera.metering.unknown"
        case .tooClose: return "arrow.up.left.and.arrow.down.right"
        case .tooFar: return "arrow.down.right.and.arrow.up.left"
        case .lowConfidence: return "cube.transparent"
        case .sparseDepth: return "circle.dotted"
        case .dark: return "moon.fill"
        }
    }
    var short: String {
        switch self {
        case .tracking: return "추적 불안"
        case .tooFast: return "너무 빠름"
        case .blur: return "흔들림"
        case .tooClose: return "너무 가까움"
        case .tooFar: return "너무 멂"
        case .lowConfidence: return "깊이 불확실"
        case .sparseDepth: return "깊이 부족"
        case .dark: return "어두움"
        }
    }
    /// 사용자가 바로 할 행동
    var advice: String {
        switch self {
        case .tracking: return "무늬가 있는 곳을 함께 비추며 천천히"
        case .tooFast: return "천천히 움직이세요"
        case .blur: return "멈추고 초점이 잡힐 때까지 잠시 고정"
        case .tooClose: return "조금 뒤로 물러나세요"
        case .tooFar: return "더 가까이 다가가세요"
        case .lowConfidence: return "표면이 반사/투명하면 각도를 바꿔 보세요"
        case .sparseDepth: return "표면이 비어 보입니다 — 각도를 바꿔 다시 비추세요"
        case .dark: return "조명을 밝히거나 밝은 쪽을 비추세요"
        }
    }
}

enum QualityLevel: Int, Codable { case bad = 0, warn = 1, good = 2 }

struct LiveQuality {
    struct Input {
        var speedMPerS: Float = 0
        var angRadPerS: Float = 0
        /// 0(선명) ... 1(매우 흔들림) — SpaceScan의 motionBlurScore와 같은 척도
        var blur: Float = 0
        var distanceM: Float? = nil
        /// 시선 중앙 영역 깊이 신뢰도 평균 0...2
        var depthConf: Float? = nil
        /// 깊이 유효 픽셀 비율 0...1
        var depthValid: Float? = nil
        var trackingNormal = true
        /// ARKit ambientIntensity(lux 근사). nil이면 판단 안 함
        var ambient: Float? = nil
    }

    struct Output: Equatable {
        var score: Float
        var level: QualityLevel
        /// 점수를 깎는 순서대로 (가장 심한 것이 먼저)
        var reasons: [QualityReason]
        var primary: QualityReason? { reasons.first }
    }

    @inline(__always) private static func clamp(_ x: Float, _ a: Float = 0, _ b: Float = 1) -> Float { min(b, max(a, x)) }

    static func evaluate(_ i: Input, profile p: QualityProfile = .space) -> Output {
        if !i.trackingNormal { return Output(score: 0, level: .bad, reasons: [.tracking]) }
        var f: [(QualityReason, Float)] = []
        // 이동·회전 속도
        let sp = max(i.speedMPerS / p.softSpeed, i.angRadPerS / p.softAng)
        f.append((.tooFast, sp <= 1 ? 1 : clamp(1 - (sp - 1) * 0.8, 0.05, 1)))
        f.append((.blur, clamp(1 - i.blur * 1.3, 0.05, 1)))
        if let d = i.distanceM, d.isFinite, d > 0 {
            if d < p.idealMin { f.append((.tooClose, d <= p.hardMin ? 0.1 : clamp(0.1 + 0.9 * (d - p.hardMin) / (p.idealMin - p.hardMin), 0.1, 1))) }
            else if d > p.idealMax { f.append((.tooFar, d >= p.hardMax ? 0.1 : clamp(1 - 0.9 * (d - p.idealMax) / (p.hardMax - p.idealMax), 0.1, 1))) }
        }
        if let c = i.depthConf { f.append((.lowConfidence, clamp(c / 1.4, 0.05, 1))) }
        if let v = i.depthValid { f.append((.sparseDepth, clamp(v / 0.55, 0.05, 1))) }
        if let a = i.ambient { f.append((.dark, a >= 60 ? 1 : clamp(0.35 + a / 100, 0.35, 1))) }
        let score = f.reduce(1) { $0 * $1.1 }
        let reasons = f.filter { $0.1 < 0.8 }.sorted { $0.1 < $1.1 }.map(\.0)
        let level: QualityLevel = score >= 0.7 ? .good : (score >= 0.4 ? .warn : .bad)
        return Output(score: score, level: level, reasons: reasons)
    }
}

// MARK: - 3) 표시 완화

/// 프레임마다 흔들리는 점수를 부드럽게 하고, 레벨은 연속 N회 같은 값이어야 바꾼다.
struct LevelFilter {
    var alpha: Float = 0.35
    var confirm = 3
    private(set) var score: Float = 1
    private(set) var level: QualityLevel = .good
    private(set) var reasons: [QualityReason] = []
    private var pending: QualityLevel?
    private var pendingCount = 0

    struct Change { var from: QualityLevel; var to: QualityLevel; var worse: Bool { to.rawValue < from.rawValue } }

    /// 새 표본을 넣고, 레벨이 바뀌었으면 Change를 돌려준다.
    @discardableResult
    mutating func push(_ o: LiveQuality.Output) -> Change? {
        score += alpha * (o.score - score)
        reasons = o.reasons
        let raw: QualityLevel = score >= 0.7 ? .good : (score >= 0.4 ? .warn : .bad)
        if raw == level { pending = nil; pendingCount = 0; return nil }
        if raw == pending { pendingCount += 1 } else { pending = raw; pendingCount = 1 }
        // 나빠지는 건 빨리(2회), 좋아지는 건 천천히(confirm회) 반영 — 경고를 놓치지 않게
        let need = raw.rawValue < level.rawValue ? max(1, confirm - 1) : confirm
        if pendingCount >= need {
            let c = Change(from: level, to: raw)
            level = raw; pending = nil; pendingCount = 0
            return c
        }
        return nil
    }

    mutating func reset() { self = LevelFilter(alpha: alpha, confirm: confirm) }
}

// MARK: - 4) 화살표 안내 계산

enum GuideMath {
    /// 섹터 차이(delta) → 도(°). 양수 = 번호가 늘어나는 방향
    static func degrees(delta: Int, sectors: Int) -> Int { Int((Float(delta) * 360 / Float(sectors)).rounded()) }

    /// 현재 시선 방위(rad, 0 = 기준 방향, 시계방향 +)와 목표 섹터 중심의 부호 있는 각도 차(°, -180...180).
    static func deltaDeg(headingRad: Float, targetSector: Int, sectors: Int) -> Float {
        let tgt = (Float(targetSector) + 0.5) / Float(sectors) * 360
        var h = headingRad * 180 / .pi
        h = h.truncatingRemainder(dividingBy: 360); if h < 0 { h += 360 }
        var d = tgt - h
        while d > 180 { d -= 360 }
        while d < -180 { d += 360 }
        return d
    }

    /// 목표 방향이 이미 화면 안(±halfFov)에 들어와 있나
    static func inView(deltaDeg: Float, halfFovDeg: Float = 28) -> Bool { abs(deltaDeg) <= halfFovDeg }
}
