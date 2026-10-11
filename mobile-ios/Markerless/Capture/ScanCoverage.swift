// ScanCoverage.swift — 대상 사물 둘레 스캔 진행도(돔 지도)와 카메라 워크 어시스트 (UI·ARKit 무관, Mac에서 단독 시험 가능)
//
// 대상 중심(POI)을 먼저 정하고 나면, 카메라가 그 사물을 어느 방향·높이에서 비췄는지를 24방위 × 3고도 격자에 쌓는다.
//  - 방위 0 = 대상을 지정한 카메라 위치(시작 위치), 방위가 늘어나면 사물을 보는 사람이 '왼쪽'으로 도는 방향
//  - 고도 띠: 0 = 옆(<25°), 1 = 비스듬(25~55°), 2 = 위(>55°, 내려다봄)
//  - 한 칸은 사물을 향해 적당한 거리(0.22~2.6m)에서 일정 시간(표본 4개 ≈ 0.4초) 비추면 채워진다
// 이 격자로 "어느 방향이 비었는지 / 너무 빠른지 / 너무 먼지"를 한 줄 어시스트로 알려 준다.

import Foundation
import simd

struct ScanCoverage {
    static let sectors = 24
    static let bands = 3
    static let need = 4
    static let sectorDeg: Float = 360 / Float(sectors)

    var counts = [Int](repeating: 0, count: sectors * bands)
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

    /// 표본 하나 추가. 새로 채워진 칸이 있으면 true (촉각 피드백용)
    @discardableResult
    mutating func add(camPos: SIMD3<Float>, camFwd: SIMD3<Float>, center: SIMD3<Float>) -> Bool {
        let p = pose(camPos: camPos, camFwd: camFwd, center: center)
        guard p.distance >= 0.22, p.distance <= 2.6, p.lookDot > 0.8 else { return false }
        let i = p.band * Self.sectors + p.sector
        counts[i] += 1
        return counts[i] == Self.need
    }

    mutating func reset() { self = ScanCoverage() }

    // MARK: - 조회

    func fill(band: Int, sector: Int) -> Float { min(1, Float(counts[band * Self.sectors + sector]) / Float(Self.need)) }
    func isFilled(band: Int, sector: Int) -> Bool { counts[band * Self.sectors + sector] >= Self.need }

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
