// SpaceQualityLogic.swift — 공간/시설물 스캔의 '품질 격자' 순수 로직 (ARKit/UI 무관 → Mac swiftc 하네스로 시험)
//   ScanQualityKit.swift(QualityGrid·LiveQuality·GuideMath)를 공간 스캔에 맞게 얹는 어댑터. 그 파일은 수정하지 않는다.
//   1) SpaceGridMap     : 시선 벡터 → (링, 섹터), FOV 이웃 채우기, 표본 로그(구간 제외 시 되돌리기)
//   2) DepthWindowStats : 깊이 맵 중앙 창의 거리 중앙값·신뢰도 평균·유효 비율 (stride 샘플링)
//   3) SpaceReview      : 문제 구간 → 사람이 읽는 제목/방향 (ReviewItem 재료), 재스캔 목표(RescanTarget)
//   4) SpaceGuide       : 격자에서 만든 안내 문구·가장자리 화살표 대상
// 링 번호: 0 = 바닥, 1 = 벽, 2 = 천장. 섹터: 세션 −Z 기준 방위 시계방향 24등분(15°). 각도 도(°).

import Foundation
import simd
#if canImport(CoreVideo)
import CoreVideo
#endif

// MARK: - 1) 격자 매핑

enum SpaceGridMap {
    static let rings = 3, sectors = 24
    static let sectorDeg: Float = 360 / Float(sectors)
    static let ringNames = ["바닥", "벽", "천장"]
    static func newGrid() -> QualityGrid { QualityGrid(rings: rings, sectors: sectors, need: 3) }

    /// 시선 방향 → 링/섹터/피치. 띠 경계는 예전과 같다: 위로 >30° 천장, 아래로 <−30° 바닥.
    static func cell(forward f: SIMD3<Float>) -> (ring: Int, sector: Int, pitchDeg: Float)? {
        guard f.x.isFinite, f.y.isFinite, f.z.isFinite, simd_length(f) > 1e-4 else { return nil }
        let n = simd_normalize(f)
        let ring = n.y > 0.5 ? 2 : (n.y < -0.5 ? 0 : 1)
        var az = atan2(n.x, -n.z)
        if az < 0 { az += 2 * .pi }
        let s = min(sectors - 1, Int(az / (2 * .pi) * Float(sectors)))
        return (ring, s, asin(max(-1, min(1, n.y))) * 180 / .pi)
    }

    static func sector(headingRad: Float) -> Int {
        var h = headingRad.truncatingRemainder(dividingBy: 2 * .pi); if h < 0 { h += 2 * .pi }
        return min(sectors - 1, Int(h / (2 * .pi) * Float(sectors)))
    }

    /// FOV 이웃 채우기 규칙 (한 번 훑어도 여러 칸이 차게 한다):
    ///  - 중심 칸(시선이 놓인 섹터)에는 표본 품질 q 그대로 1프레임.
    ///  - 같은 링의 좌우 이웃 섹터에는, 중심에서 span° 이내(span = FOV × 0.3 ÷ max(0.35, cos(피치)))이면
    ///    q × 0.85^(칸 떨어진 수) 로 1프레임씩 — 중심보다 약간 낮은 가중. 카메라는 한 번에 FOV 전체를 보지만
    ///    가장자리 픽셀은 덜 믿을 만하므로 0.3(=양쪽 합 60%)만 인정한다.
    ///  - 천장/바닥을 볼수록(피치↑) 방위 한 칸의 실제 폭이 좁아지므로 span이 1/cos 로 늘어난다(최대 0.35 하한 → ×2.9).
    static func neighborOffsets(fovDeg: Float, pitchDeg: Float) -> Int {
        let c = max(0.35, cos(pitchDeg * .pi / 180))
        let span = fovDeg * 0.3 / c
        return min(sectors / 2 - 1, Int(span / sectorDeg))
    }

    struct Hit: Equatable { var ring: Int; var sector: Int; var q: Float; var old: CellState; var new: CellState }

    /// 표본 하나를 격자에 반영. 적용된 칸 목록(되돌리기용)을 돌려준다.
    @discardableResult
    static func add(to g: inout QualityGrid, forward: SIMD3<Float>, quality: Float, fovDeg: Float = 62,
                    neighborFactor: Float = 0.85) -> [Hit] {
        guard let c = cell(forward: forward), quality.isFinite else { return [] }
        var hits: [Hit] = []
        func put(_ s: Int, _ q: Float) {
            let r = g.add(ring: c.ring, sector: s, quality: q)
            hits.append(Hit(ring: c.ring, sector: ((s % sectors) + sectors) % sectors, q: max(0, min(1, q)), old: r.old, new: r.new))
        }
        put(c.sector, quality)
        let k = neighborOffsets(fovDeg: fovDeg, pitchDeg: c.pitchDeg)
        if k > 0 {
            for off in 1...k {
                let f = powf(neighborFactor, Float(off))
                put(c.sector + off, quality * f)
                put(c.sector - off, quality * f)
            }
        }
        return hits
    }

    /// 표본 1개를 되돌린다 (구간 제외).
    static func remove(from g: inout QualityGrid, _ h: Hit) {
        let i = g.idx(h.ring, h.sector)
        g.cells[i].frames = max(0, g.cells[i].frames - 1)
        g.cells[i].sumQ = g.cells[i].frames == 0 ? 0 : max(0, g.cells[i].sumQ - h.q)
    }

    /// 격자 전체 평균 품질 (본 칸만)
    static func meanQuality(_ g: QualityGrid) -> Float? {
        let seen = g.cells.filter { $0.frames > 0 }
        guard !seen.isEmpty else { return nil }
        return seen.map(\.avg).reduce(0, +) / Float(seen.count)
    }
}

/// 최근 표본 로그 — [방금 구간 제외] 때 격자에서 그 표본들을 빼기 위해 짧게 보관한다.
struct SampleLog {
    private(set) var entries: [(t: TimeInterval, hits: [SpaceGridMap.Hit])] = []
    var keepSeconds: TimeInterval = 15

    mutating func append(t: TimeInterval, hits: [SpaceGridMap.Hit]) {
        entries.append((t, hits))
        while let f = entries.first, t - f.t > keepSeconds { entries.removeFirst() }
    }
    /// t >= cut 인 표본을 격자에서 빼고 로그에서도 지운다. 반환: 뺀 표본 수
    @discardableResult
    mutating func rollback(since cut: TimeInterval, grid: inout QualityGrid) -> Int {
        let drop = entries.filter { $0.t >= cut }
        for e in drop { for h in e.hits { SpaceGridMap.remove(from: &grid, h) } }
        entries.removeAll { $0.t >= cut }
        return drop.count
    }
    mutating func reset() { entries.removeAll() }
}

// MARK: - 2) 깊이 중앙 창 통계

enum DepthWindowStats {
    struct Result: Equatable {
        var medianM: Float?    // 유효 깊이의 중앙값 (없으면 nil)
        var meanConf: Float?   // 유효 깊이 픽셀의 신뢰도 평균 0...2 (신뢰도 맵이 없으면 nil)
        var validRatio: Float  // 창 안 표본 중 유효(유한·양수) 비율
        var samples: Int
    }

    /// 깊이 맵(행 stride는 float 개수 단위) 중앙 `fraction`×`fraction` 창을 `step` 간격으로 훑는다.
    /// 256×192 맵 기준 step 2 → 약 2천 표본. 0/NaN/음수는 무효로 보고 중앙값·신뢰도에서 제외한다.
    static func compute(width w: Int, height h: Int,
                        depth: UnsafePointer<Float>, depthStride: Int,
                        conf: UnsafePointer<UInt8>?, confStride: Int,
                        fraction: Float = 0.4, step: Int = 2) -> Result {
        guard w > 0, h > 0 else { return Result(medianM: nil, meanConf: nil, validRatio: 0, samples: 0) }
        let f = max(0.05, min(1, fraction)), st = max(1, step)
        let x0 = Int(Float(w) * (1 - f) / 2), x1 = max(x0 + 1, Int(Float(w) * (1 + f) / 2))
        let y0 = Int(Float(h) * (1 - f) / 2), y1 = max(y0 + 1, Int(Float(h) * (1 + f) / 2))
        var vals: [Float] = []
        vals.reserveCapacity(((x1 - x0) / st + 1) * ((y1 - y0) / st + 1))
        var total = 0, confSum = 0
        var y = y0
        while y < min(y1, h) {
            var x = x0
            while x < min(x1, w) {
                total += 1
                let v = depth[y * depthStride + x]
                if v.isFinite, v > 0 {
                    vals.append(v)
                    if let c = conf { confSum += Int(min(2, c[y * confStride + x])) }
                }
                x += st
            }
            y += st
        }
        guard total > 0 else { return Result(medianM: nil, meanConf: nil, validRatio: 0, samples: 0) }
        let ratio = Float(vals.count) / Float(total)
        guard !vals.isEmpty else { return Result(medianM: nil, meanConf: nil, validRatio: 0, samples: total) }
        vals.sort()
        let m = vals.count / 2
        let med = vals.count % 2 == 1 ? vals[m] : (vals[m - 1] + vals[m]) / 2
        return Result(medianM: med, meanConf: conf == nil ? nil : Float(confSum) / Float(vals.count), validRatio: ratio, samples: total)
    }

    #if canImport(CoreVideo)
    /// ARFrame.sceneDepth.depthMap(Float32) + confidenceMap(UInt8) 래퍼
    static func compute(depthMap: CVPixelBuffer, confidenceMap: CVPixelBuffer?, fraction: Float = 0.4, step: Int = 2) -> Result? {
        guard CVPixelBufferGetPixelFormatType(depthMap) == kCVPixelFormatType_DepthFloat32 else { return nil }
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }
        guard let db = CVPixelBufferGetBaseAddress(depthMap) else { return nil }
        let w = CVPixelBufferGetWidth(depthMap), h = CVPixelBufferGetHeight(depthMap)
        let dstride = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float>.size
        var cptr: UnsafePointer<UInt8>?
        var cstride = 0
        if let cm = confidenceMap, CVPixelBufferGetWidth(cm) == w, CVPixelBufferGetHeight(cm) == h {
            CVPixelBufferLockBaseAddress(cm, .readOnly)
            if let cb = CVPixelBufferGetBaseAddress(cm) { cptr = UnsafePointer(cb.assumingMemoryBound(to: UInt8.self)); cstride = CVPixelBufferGetBytesPerRow(cm) }
            defer { CVPixelBufferUnlockBaseAddress(cm, .readOnly) }
            return compute(width: w, height: h, depth: db.assumingMemoryBound(to: Float.self), depthStride: dstride,
                           conf: cptr, confStride: cstride, fraction: fraction, step: step)
        }
        return compute(width: w, height: h, depth: db.assumingMemoryBound(to: Float.self), depthStride: dstride,
                       conf: nil, confStride: 0, fraction: fraction, step: step)
    }
    #endif
}

// MARK: - 3) 점검 항목 / 재스캔 목표

struct ProblemDescriptor: Equatable {
    var title: String
    var detail: String
    var severity: CellState   // .missing | .low
    var ring: Int
    var sector: Int           // 구간의 첫 섹터
    var length: Int
    var sectors: [Int]
}

enum SpaceReview {
    /// 부호 있는 각도(°, +오른쪽) → "오른쪽 105°" 같은 문구 (5° 단위 반올림)
    static func sideText(deg: Float) -> String {
        let a = Int((abs(deg) / 5).rounded()) * 5
        if a < 10 { return "정면" }
        if a > 170 { return "정반대(뒤쪽)" }
        return "\(deg > 0 ? "오른쪽" : "왼쪽") \(a)°"
    }

    static func normalize(_ d: Float) -> Float {
        var x = d.truncatingRemainder(dividingBy: 360)
        if x > 180 { x -= 360 }; if x <= -180 { x += 360 }
        return x
    }

    static func range(_ p: QualityGrid.Problem, sectors: Int) -> String {
        if p.length >= sectors { return "전체" }
        let a = p.start % sectors + 1
        return p.length == 1 ? "\(a)" : "\(a)~\((p.start + p.length - 1) % sectors + 1)"
    }

    static func describe(_ p: QualityGrid.Problem, sectors: Int = SpaceGridMap.sectors, startHeadingRad: Float) -> ProblemDescriptor {
        let name = SpaceGridMap.ringNames[p.ring]
        let r = range(p, sectors: sectors)
        let title = p.length >= sectors ? "\(name) 전체 · \(p.kind.label)" : "\(name) 구간 \(r) · \(p.kind.label)"
        var detail: String
        if p.length >= sectors {
            detail = "모든 방향"
        } else {
            let centerDeg = (Float(p.start) + Float(p.length) / 2) * 360 / Float(sectors)
            let rel = normalize(centerDeg - startHeadingRad * 180 / .pi)
            let t = sideText(deg: rel)
            detail = t == "정면" ? "시작 방향 부근" : (t.hasPrefix("정반대") ? "시작 방향의 정반대" : "시작 방향에서 \(t) 부근")
        }
        if p.ring == 2 { detail += " · 위쪽을 비추세요" } else if p.ring == 0 { detail += " · 아래쪽을 비추세요" }
        return ProblemDescriptor(title: title, detail: detail, severity: p.kind, ring: p.ring, sector: p.start % sectors,
                                 length: p.length, sectors: p.sectorList(sectors))
    }

    /// 비어 있음 먼저, 같은 심각도 안에서는 긴 구간 먼저 (그 다음 링·시작 번호)
    static func items(_ g: QualityGrid, startHeadingRad: Float) -> [ProblemDescriptor] {
        g.problems()
            .sorted { a, b in
                if a.kind != b.kind { return a.kind == .missing }
                if a.length != b.length { return a.length > b.length }
                if a.ring != b.ring { return a.ring < b.ring }
                return a.start < b.start
            }
            .map { describe($0, sectors: g.sectors, startHeadingRad: startHeadingRad) }
    }

    /// 완료 전 점검이 필요한가
    /// 매번 뜨면 성가시므로(바닥·천장 칸을 전부 채우기는 어렵다) 다음 중 하나일 때만:
    /// ① 전체 진행도 < minOverall ② 벽 링에 빈/낮은 칸이 있다 ③ 바닥 또는 천장 링 진행도 < 50%.
    /// 바닥·천장의 작은 구멍은 점검 화면에는 목록으로 보이지만 완료를 막거나 자동으로 띄우지 않는다.
    static func needsReview(_ g: QualityGrid, minOverall: Float = 0.8) -> Bool {
        if g.overall(weights: [1, 2, 1]) < minOverall { return true }
        if g.problems().contains(where: { $0.ring == 1 }) { return true }
        return g.progress(ring: 0) < 0.5 || g.progress(ring: 2) < 0.5
    }
}

/// [부족한 곳 다시 스캔]의 목표: 한 구간(링 + 섹터들). 앞에서부터 '양호' 미만인 첫 칸이 현재 목표 칸.
struct RescanTarget: Equatable {
    var ring: Int
    var sectors: [Int]
    func next(in g: QualityGrid) -> Int? { sectors.first { g.state(ring, $0) < .ok } }
    func satisfied(in g: QualityGrid) -> Bool { next(in: g) == nil }
}

// MARK: - 4) 안내 (격자 → 문구·화살표)

struct GuideCell: Equatable { var ring: Int; var sector: Int }

enum SpaceGuide {
    enum Vertical { case none, up, down }

    /// 안내 대상 칸: 재스캔 목표가 있으면 그 칸, 없으면 현재 시선에서 가장 가까운 문제 칸(벽 우선).
    static func pick(grid g: QualityGrid, headingRad: Float, target: RescanTarget?) -> GuideCell? {
        if let t = target, let s = t.next(in: g) { return GuideCell(ring: t.ring, sector: s) }
        guard let n = g.nearestProblem(from: SpaceGridMap.sector(headingRad: headingRad), preferRing: 1) else { return nil }
        return GuideCell(ring: n.ring, sector: n.sector)
    }

    /// 대상이 천장/바닥 칸이고 지금 그 띠를 보고 있지 않으면 위/아래 화살표
    static func vertical(for cell: GuideCell?, viewRing: Int) -> Vertical {
        guard let c = cell, c.ring != viewRing, c.ring != 1 else { return .none }
        return c.ring == 2 ? .up : .down
    }

    /// 벽 띠를 보고 있지 않을 때 벽 대상: 시선을 정면으로 (좌우 화살표만 의미 있음)
    static func hint(grid g: QualityGrid, headingRad: Float, viewRing: Int, target: RescanTarget?, facility: Bool = false) -> String {
        let seen = g.cells.reduce(0) { $0 + $1.frames }
        if seen == 0 { return facility ? "시설물 주변을 천천히 한 바퀴 둘러보세요" : "천천히 방 안을 둘러보며 시작하세요" }
        guard let c = pick(grid: g, headingRad: headingRad, target: target) else {
            return "충분합니다 — 끝내려면 완료를 누르세요"
        }
        let st = g.state(c.ring, c.sector)
        let name = SpaceGridMap.ringNames[c.ring]
        // 그 칸이 속한 연속 구간 (같은 종류)
        let prob = g.problems().first { $0.ring == c.ring && $0.kind == st && $0.sectorList(g.sectors).contains(c.sector) }
        let rng = prob.map { SpaceReview.range($0, sectors: g.sectors) } ?? "\(c.sector + 1)"
        let what = rng == "전체" ? "\(name) 전체가" : "\(name) \(rng)번 구간이"
        let state = st == .missing ? "비어 있습니다" : "품질이 낮습니다"
        let d = GuideMath.deltaDeg(headingRad: headingRad, targetSector: c.sector, sectors: g.sectors)
        var dir: String
        switch vertical(for: c, viewRing: viewRing) {
        case .up: dir = "위쪽(천장)을 비추세요"
        case .down: dir = "아래쪽(바닥)을 비추세요"
        case .none:
            dir = abs(d) <= 12 ? "지금 방향을 잠시 더 비추세요"
                : "\(d > 0 ? "오른쪽" : "왼쪽")으로 약 \(Int((abs(d) / 5).rounded()) * 5)° 돌아 비추세요"
        }
        return "\(what) \(state) — \(dir)"
    }
}
