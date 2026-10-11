// SpaceScanLogic.swift — 공간 전체 스캔의 순수 로직 (ARKit/UI 무관 → Mac swiftc 하네스로 단독 시험 가능)
//   1) SpaceCoverageGrid : 방위 24섹터 × 3띠(바닥/벽/천장) 커버리지 격자 + 안내 문구
//   2) KeyframeGate      : 키프레임 저장 조건 (이동 ≥0.15m 또는 회전 ≥10°, 추적 정상, 블러 낮음) + 추적 점프(세그먼트) 감지
//   3) ChunkPlanner      : 업로드 청크 범위 계획 (재개 지원)
//   4~7) 선명도(Laplacian 분산)·블러 결합, 창 내 최선 프레임, 정지 깊이 평균, 고화질 스틸 보조
// 좌표: ARKit 세션 월드(오른손 Y-up, 카메라 −Z 전방). 각도 도(°), 길이 m.

import Foundation
import simd

// MARK: - 1) 커버리지 격자

struct SpaceCoverageGrid: Codable {
    static let sectors = 24
    static let bands = 3  // 0 = 바닥, 1 = 벽, 2 = 천장
    static let sectorDeg: Float = 360 / Float(sectors)
    /// 한 칸이 '채워짐'으로 보는 키프레임 수
    static let need = 2
    static let bandNames = ["바닥", "벽", "천장"]

    var counts = [Int](repeating: 0, count: sectors * bands)

    /// 시선 방향(월드 단위벡터)으로 칸을 정한다. 띠: 위로 >30° 천장, 아래로 <−30° 바닥, 그 사이 벽.
    /// 주의: 카메라가 '어디를 향했는가'(시선)만 센다 — 실제 표면 관측 면적이 아니라 촬영 가이드용 근사치다.
    /// 면적 기준 커버리지는 서버(GET /spaces/{id}/coverage)가 정본.
    static func cell(forward f: SIMD3<Float>) -> (band: Int, sector: Int) {
        let n = simd_normalize(f)
        let band = n.y > 0.5 ? 2 : (n.y < -0.5 ? 0 : 1)
        var az = atan2(n.x, -n.z)  // 0 = 세션 −Z 방향, 시계방향 +
        if az < 0 { az += 2 * .pi }
        let s = min(sectors - 1, Int(az / (2 * .pi) * Float(sectors)))
        return (band, s)
    }

    /// 새로 '채워진' 칸이면 true
    @discardableResult
    mutating func add(forward f: SIMD3<Float>) -> Bool {
        guard f.x.isFinite, f.y.isFinite, f.z.isFinite, simd_length(f) > 1e-4 else { return false }
        let c = Self.cell(forward: f)
        let i = c.band * Self.sectors + c.sector
        counts[i] += 1
        return counts[i] == Self.need
    }

    func isFilled(band: Int, sector: Int) -> Bool { counts[band * Self.sectors + sector] >= Self.need }
    func fill(band: Int, sector: Int) -> Float { min(1, Float(counts[band * Self.sectors + sector]) / Float(Self.need)) }

    func bandFraction(_ band: Int) -> Float {
        Float((0..<Self.sectors).filter { isFilled(band: band, sector: $0) }.count) / Float(Self.sectors)
    }
    var floorFraction: Float { bandFraction(0) }
    var wallFraction: Float { bandFraction(1) }
    var ceilingFraction: Float { bandFraction(2) }
    var overall: Float { (floorFraction + wallFraction * 2 + ceilingFraction) / 4 }  // 벽 가중 2

    mutating func reset() { self = SpaceCoverageGrid() }

    /// 지금 가장 도움 되는 한 줄. heading = 현재 시선 방위(rad, 위 azimuth 규약).
    func hint(headingRad: Float) -> String {
        if overall < 0.03 { return "천천히 방 안을 둘러보며 시작하세요" }
        // 벽 띠에서 비어 있는 섹터를 현재 방위에서 가장 가까운 순서로 찾는다
        var cur = headingRad
        while cur < 0 { cur += 2 * .pi }
        let curSec = min(Self.sectors - 1, Int(cur / (2 * .pi) * Float(Self.sectors)))
        if wallFraction < 0.9 {
            for off in 0...(Self.sectors / 2) {
                for sign in (off == 0 ? [1] : [1, -1]) {
                    let s = ((curSec + sign * off) % Self.sectors + Self.sectors) % Self.sectors
                    if !isFilled(band: 1, sector: s) {
                        if off == 0 { return "이 방향 벽을 잠시 더 비추세요" }
                        let deg = off * Int(Self.sectorDeg)
                        // 방위는 시계방향(오른쪽)으로 증가
                        let right = sign > 0
                        return off <= 2 ? "\(right ? "오른쪽" : "왼쪽") 벽 코너 재촬영"
                                        : "\(right ? "오른쪽" : "왼쪽")으로 약 \(deg)° 돌아 벽을 비추세요"
                    }
                }
            }
        }
        if ceilingFraction < 0.6 { return "천장을 더 비추세요" }
        if floorFraction < 0.6 { return "바닥을 더 비추세요" }
        return "충분합니다 — 끝내려면 완료를 누르세요"
    }
}

// MARK: - 2) 키프레임 게이트

struct KeyframeGate {
    var minMoveM: Float = 0.15
    var minTurnDeg: Float = 10
    var maxBlur: Float = 0.5
    /// 연속 두 프레임 사이 이동이 이 값을 넘으면 추적 점프로 보고 새 세그먼트
    var jumpM: Float = 0.5

    enum Decision: Equatable { case accept, skipTracking, skipBlur, skipNotMoved }

    private var lastKF: simd_float4x4?
    private var prevPos: SIMD3<Float>?
    private var wasNormal = true
    private(set) var segmentIndex = 0

    /// 매 프레임 호출. 반환 jump = 이번 프레임에서 새 세그먼트가 시작되었는가(점프·재위치).
    mutating func observe(transform t: simd_float4x4, trackingNormal: Bool) -> Bool {
        let p = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        var jump = false
        if let pp = prevPos, simd_distance(p, pp) > jumpM { jump = true }
        // 추적이 한 번 끊겼다가 돌아오면 재위치 인식으로 좌표가 옮겨졌을 수 있다
        if trackingNormal, !wasNormal { jump = true }
        prevPos = p
        wasNormal = trackingNormal
        if jump { startSegment() }
        return jump
    }

    mutating func startSegment() {
        segmentIndex += 1
        lastKF = nil  // 새 세그먼트의 첫 프레임은 무조건 저장
    }

    /// 이동/회전 조건만 본다 (상태 변경 없음). 창(window) 방식 선택에서 '열림' 판정에 쓴다.
    func motionOpen(transform t: simd_float4x4) -> Bool {
        guard let l = lastKF else { return true }
        return simd_distance(Self.pos(t), Self.pos(l)) >= minMoveM || Self.angleDeg(t, l) >= minTurnDeg
    }

    /// 창에서 고른 프레임을 확정한다 (다음 이동 기준점 갱신)
    mutating func commit(transform t: simd_float4x4) { lastKF = t }

    mutating func decide(transform t: simd_float4x4, trackingNormal: Bool, blur: Float) -> Decision {
        guard trackingNormal else { return .skipTracking }
        guard blur <= maxBlur else { return .skipBlur }
        if let l = lastKF {
            let moved = simd_distance(Self.pos(t), Self.pos(l)) >= minMoveM
            let turned = Self.angleDeg(t, l) >= minTurnDeg
            guard moved || turned else { return .skipNotMoved }
        }
        lastKF = t
        return .accept
    }

    /// 블러 근사: 각속도(rad/s)·선속도(m/s)·노출시간으로 픽셀 번짐 추정 → 0...1 (0.5 ≈ 4px 번짐)
    static func blurScore(angularRadPerS: Float, linearMPerS: Float, exposureS: Float?, fxPx: Float) -> Float {
        let exp = max(0.001, exposureS ?? (1.0 / 60))
        let px = angularRadPerS * exp * fxPx
        let byPx = px / 8
        let bySpeed = max(angularRadPerS / 2.0, linearMPerS / 1.5)  // 노출 정보가 부정확할 때의 안전망
        return min(1, max(byPx, bySpeed))
    }

    static func pos(_ t: simd_float4x4) -> SIMD3<Float> { SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z) }

    static func angleDeg(_ a: simd_float4x4, _ b: simd_float4x4) -> Float {
        let qa = simd_quatf(a), qb = simd_quatf(b)
        let d = abs(simd_dot(qa.vector, qb.vector))
        return 2 * acos(min(1, d)) * 180 / .pi
    }
}

// MARK: - 3) 업로드 청크 계획

enum ChunkPlanner {
    /// total 바이트 중 received까지 올렸을 때 남은 [offset, offset+len) 목록
    static func plan(total: Int, received: Int, chunk: Int = 1 << 20) -> [(offset: Int, length: Int)] {
        guard total > 0, chunk > 0 else { return [] }
        var off = max(0, min(received, total))
        var out: [(Int, Int)] = []
        while off < total {
            let n = min(chunk, total - off)
            out.append((off, n))
            off += n
        }
        return out
    }
}


// MARK: - 4) 선명도 (Laplacian 분산) · 블러 점수 결합

enum SharpnessMath {
    /// 휘도(0...255) 평면의 Laplacian(4-이웃) 분산. 클수록 선명. 가장자리 1픽셀은 제외.
    static func laplacianVariance(luma: [UInt8], width w: Int, height h: Int) -> Float {
        guard w >= 3, h >= 3, luma.count >= w * h else { return 0 }
        var sum: Double = 0, sum2: Double = 0
        var n = 0
        luma.withUnsafeBufferPointer { p in
            for y in 1..<(h - 1) {
                let r = y * w
                for x in 1..<(w - 1) {
                    let v = 4 * Int(p[r + x]) - Int(p[r + x - 1]) - Int(p[r + x + 1]) - Int(p[r - w + x]) - Int(p[r + w + x])
                    let d = Double(v)
                    sum += d; sum2 += d * d; n += 1
                }
            }
        }
        guard n > 0 else { return 0 }
        let mean = sum / Double(n)
        return Float(max(0, sum2 / Double(n) - mean * mean))
    }

    /// 선명도 → 블러 0...1. reference = 최근 창의 최대 선명도(피크 홀드, 서서히 감쇠).
    /// 장면마다 절대 선명도가 다르므로(무늬 없는 흰 벽 등) 기준이 의미 있을 때(≥15)만 상대 비교하고, 아니면 0(판단 보류).
    /// rel ≥ 0.65 → 0, rel ≤ 0.20 → 1.
    static func sharpBlur(sharpness s: Float, reference: Float?) -> Float {
        guard let ref = reference, ref >= 15 else { return 0 }
        let rel = s / ref
        return min(1, max(0, (0.65 - rel) / 0.45))
    }

    /// 속도 기반 블러와 선명도 기반 블러 결합: 큰 쪽 + 작은 쪽의 20% (둘 다 나쁘면 더 나쁘게).
    static func combine(speedBlur: Float, sharpBlur: Float) -> Float {
        let hi = max(speedBlur, sharpBlur), lo = min(speedBlur, sharpBlur)
        return min(1, hi + 0.2 * lo)
    }

    /// 창이 끝날 때마다 호출: 피크 홀드 + 감쇠(0.85). 연속 거절돼도 몇 창 뒤엔 새 장면에 적응한다.
    static func updateReference(_ ref: Float?, windowBest s: Float) -> Float {
        guard let r = ref else { return s }
        return max(s, r * 0.85)
    }
}

// MARK: - 5) 창 내 최선 프레임 선택

/// 이동 게이트가 열린 뒤 최대 maxWindowS초 / maxFrames장 동안 후보를 받아 점수(낮을수록 좋음 = 블러)가 가장 낮은 하나를 돌려준다.
struct BestFramePicker<Item> {
    var maxWindowS: Double = 0.4
    var maxFrames: Int = 6
    private(set) var best: (item: Item, score: Float, t: Double)?
    private var startT: Double?
    private(set) var count = 0

    var isOpen: Bool { startT != nil }

    /// 후보 제시. 창이 닫히면(시간 초과 또는 장수 도달) 최선 후보를 반환하고 창을 비운다. 동점이면 더 이른 프레임.
    mutating func offer(_ item: Item, score: Float, at t: Double) -> (item: Item, score: Float, t: Double)? {
        if startT == nil { startT = t }
        count += 1
        if best == nil || score < best!.score { best = (item, score, t) }
        if count >= maxFrames || t - (startT ?? t) >= maxWindowS {
            let out = best
            reset()
            return out
        }
        return nil
    }

    mutating func reset() { best = nil; startT = nil; count = 0 }
}

// MARK: - 6) 정지 시 깊이 시간 평균

struct DepthRing {
    static let capacity = 5
    private(set) var depths: [[Float]] = []
    private(set) var confs: [[UInt8]] = []
    var count: Int { depths.count }

    mutating func push(depth: [Float], conf: [UInt8]) {
        if let f = depths.first, f.count != depth.count { reset() }
        depths.append(depth); confs.append(conf)
        if depths.count > Self.capacity { depths.removeFirst(); confs.removeFirst() }
    }
    mutating func reset() { depths.removeAll(); confs.removeAll() }
}

enum DepthDenoise {
    struct Result {
        var depth: [Float]
        var conf: [UInt8]
        /// 2장 이상 평균된 픽셀 수 / 유효 픽셀 수
        var averagedPixels: Int
        var validPixels: Int
    }

    static let confWeight: [Float] = [0.25, 1, 2]

    /// 픽셀별 신뢰도 가중 평균. 0/비유한 깊이는 무시, 유효 표본 ≥2 이고 중앙값에서 max(3cm, 3%) 이내인 표본만 평균.
    /// 조건 미달이면 중앙값 표본 하나를 그대로 쓴다. conf = 기여 표본 conf 평균의 반올림.
    static func average(depths: [[Float]], confs: [[UInt8]]) -> Result {
        let n = depths.count
        precondition(n > 0 && confs.count == n)
        let px = depths[0].count
        var outD = [Float](repeating: 0, count: px)
        var outC = [UInt8](repeating: 0, count: px)
        var averaged = 0, valid = 0
        var ds = [Float](repeating: 0, count: n), cs = [UInt8](repeating: 0, count: n)
        for i in 0..<px {
            var m = 0
            for k in 0..<n {
                let d = depths[k][i]
                if d.isFinite, d > 0 { ds[m] = d; cs[m] = min(2, confs[k][i]); m += 1 }
            }
            if m == 0 { continue }
            valid += 1
            if m == 1 { outD[i] = ds[0]; outC[i] = cs[0]; continue }
            let sorted = ds[0..<m].sorted()
            let med = sorted[m / 2]
            let tol = max(0.03, 0.03 * med)
            var wsum: Float = 0, dsum: Float = 0, csum = 0, kept = 0
            for j in 0..<m where abs(ds[j] - med) <= tol {
                let w = confWeight[Int(cs[j])]
                wsum += w; dsum += w * ds[j]; csum += Int(cs[j]); kept += 1
            }
            if kept >= 2 {
                outD[i] = dsum / wsum
                outC[i] = UInt8((Float(csum) / Float(kept)).rounded())
                averaged += 1
            } else {
                let j = (0..<m).min { abs(ds[$0] - med) < abs(ds[$1] - med) } ?? 0
                outD[i] = ds[j]; outC[i] = cs[j]
            }
        }
        return Result(depth: outD, conf: outC, averagedPixels: averaged, validPixels: valid)
    }

    /// 유효 깊이 픽셀의 평균 신뢰도(0...2). 유효 픽셀이 없으면 0.
    static func meanConfidence(depth: [Float], conf: [UInt8]) -> Float {
        var s = 0, n = 0
        for i in 0..<min(depth.count, conf.count) where depth[i].isFinite && depth[i] > 0 { s += Int(min(2, conf[i])); n += 1 }
        return n > 0 ? Float(s) / Float(n) : 0
    }
}

// MARK: - 7) 고화질 스틸 보조 (내부 파라미터 스케일 · 포즈 호환)

enum StillCapture {
    /// 내부 파라미터를 다른 픽셀 격자로 옮긴다. 규약: 정수 i인 픽셀의 중심은 i+0.5, cx/cy는 이미지 좌상단 '모서리' 기준
    /// (spatial_core/reconstruction/camera.py). 모서리 기준 연속 좌표는 해상도에 선형 비례하므로 fx,cx는 W'/W, fy,cy는 H'/H 배.
    /// (중심 기준 정수 격자였다면 (c+0.5)*s-0.5 보정이 필요하지만 이 계약은 모서리 기준이라 단순 비례.)
    /// 반환: 열 우선 3x3 [fx,0,0, 0,fy,0, cx,cy,1]
    static func scaleIntrinsics(fx: Float, fy: Float, cx: Float, cy: Float,
                                fromW: Float, fromH: Float, toW: Float, toH: Float) -> [Float] {
        let sx = toW / fromW, sy = toH / fromH
        return [fx * sx, 0, 0, 0, fy * sy, 0, cx * sx, cy * sy, 1]
    }

    /// 스틸 포즈와 일반 프레임(깊이 제공) 포즈 차이가 1cm, 0.5° 미만인가
    static func poseCompatible(_ a: simd_float4x4, _ b: simd_float4x4, maxMoveM: Float = 0.01, maxDeg: Float = 0.5) -> Bool {
        simd_distance(KeyframeGate.pos(a), KeyframeGate.pos(b)) < maxMoveM && KeyframeGate.angleDeg(a, b) < maxDeg
    }
}
