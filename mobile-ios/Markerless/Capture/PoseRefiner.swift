// PoseRefiner.swift — 맞춘 포즈를 실시간 깊이 점으로 계속 다듬는다 (UI·ARKit 무관, Mac에서 단독 시험 가능)
//
// 서버가 한 번 맞춘 포즈는 그 순간의 점으로 구한 것이다. 이후 ARKit이 재위치 인식·드리프트로 좌표계를 조금 옮기거나
// 맞추는 데 걸린 시간 동안 폰이 움직이면 오버레이가 사물 옆으로 빗나가 보인다.
// 그래서 오버레이가 떠 있는 동안, 지금 보이는 깊이 점(관측)에 모델 표면을 다시 맞춘다:
//  - 4자유도(중력축 yaw + 이동 3축) — 서버 정합과 같은 제약이라 기울어지지 않는다
//  - 관측 점마다 가장 가까운 모델 점과 짝을 짓고, 먼 짝(배경·잡음) 20%는 버린다(트림) — 일부만 보여도 동작
//  - 이전 포즈에서 시작하므로 작은 보정만 한다. 많이 움직이면(>10cm, >15°) 믿지 않고 버린다.

import Foundation
import simd

struct RefinePose {
    var position: SIMD3<Float>
    var yawRad: Float          // 중력축(Y) 회전 — orientation = Ry(yawRad) * base
    var base: simd_quatf       // 서버가 준 원래 자세 (고정)

    var orientation: simd_quatf { simd_quatf(angle: yawRad, axis: SIMD3(0, 1, 0)) * base }
}

struct RefineResult {
    var pose: RefinePose
    var rmse: Float            // 짝 지은 점의 제곱평균 거리(m)
    var inlier: Float          // 1.5cm 안 점 비율
    var shiftM: Float          // 시작 포즈 대비 이동량
    var yawDeg: Float          // 시작 포즈 대비 회전량
    var accepted: Bool
    var reason: String
}

enum PoseRefiner {
    static let maxShiftM: Float = 0.10
    static let maxYawDeg: Float = 15
    static let inlierDist: Float = 0.015

    /// model: 자산 로컬 표면 점, live: 현재 월드 좌표 관측 점, start: 지금 오버레이 포즈
    /// modelNormals: model과 같은 순서의 로컬 법선(estimateNormals). 있으면 점-면(point-to-plane) 오차를 주로 써서
    /// 평평한 면에서 점이 면을 따라 미끄러지는 문제(옆으로 빗나감·회전 모호)를 줄이고, 점-점 항을 약하게 섞어 경계를 잡는다.
    nonisolated static func refine(model: [SIMD3<Float>], live: [SIMD3<Float>], start: RefinePose, modelNormals: [SIMD3<Float>]? = nil,
                                   corr: Float = 0.06, iterations: Int = 15, trim: Float = 0.2) -> RefineResult {
        guard model.count >= 50, live.count >= 80 else {
            return RefineResult(pose: start, rmse: .infinity, inlier: 0, shiftM: 0, yawDeg: 0, accepted: false, reason: "점 부족")
        }
        var pose = start
        let before = score(model: model, live: live, pose: start, corr: corr)
        var lastRmse = Float.infinity
        for _ in 0..<iterations {
            let q = pose.orientation
            let world = model.map { q.act($0) + pose.position }
            let grid = Grid(points: world, cell: corr)
            // 관측 점 → 가장 가까운 모델 점 (corr 안에서만)
            var pairs: [(o: SIMD3<Float>, m: SIMD3<Float>, n: SIMD3<Float>, d: Float)] = []
            pairs.reserveCapacity(live.count)
            for o in live {
                if let (idx, d) = grid.nearestIndex(to: o, within: corr) {
                    let n = modelNormals.map { q.act($0[idx]) } ?? .zero
                    pairs.append((o, world[idx], n, d))
                }
            }
            guard pairs.count >= 40 else { break }
            pairs.sort { $0.d < $1.d }
            let keep = max(40, Int(Float(pairs.count) * (1 - trim)))
            let use = pairs[0..<keep]
            var cm = SIMD3<Float>.zero
            for p in use { cm += p.m }
            cm /= Float(use.count)
            // 미지수 u = (dθ, tx, ty, tz): 월드 변환 W(x) = c + Ry(dθ)(x−c) + t, 작은 각 근사 Ry(θ)a ≈ a + θ(az, 0, −ax)
            var AtA = [Float](repeating: 0, count: 16), Atb = [Float](repeating: 0, count: 4)
            let wn: Float = modelNormals == nil ? 0 : 1, wp: Float = modelNormals == nil ? 1 : 0.08
            func add(_ g: [Float], _ r: Float, _ w: Float) {
                for i in 0..<4 { Atb[i] += w * g[i] * r; for j in 0..<4 { AtA[i * 4 + j] += w * g[i] * g[j] } }
            }
            for p in use {
                let a = p.m - cm, r3 = p.o - p.m
                let rot = SIMD3<Float>(a.z, 0, -a.x)
                if wn > 0 {
                    let g = [simd_dot(p.n, rot), p.n.x, p.n.y, p.n.z]
                    add(g, simd_dot(p.n, r3), wn)
                }
                add([rot.x, 1, 0, 0], r3.x, wp); add([rot.y, 0, 1, 0], r3.y, wp); add([rot.z, 0, 0, 1], r3.z, wp)
            }
            for i in 0..<4 { AtA[i * 4 + i] += 1e-4 }  // 안정화
            guard let u = solve4(AtA, Atb) else { break }
            let dTheta = max(-0.35, min(0.35, u[0]))
            let c = cos(dTheta), s = sin(dTheta)
            let t = SIMD3<Float>(u[1], u[2], u[3])
            let rotC = SIMD3<Float>(cm.x * c + cm.z * s, cm.y, -cm.x * s + cm.z * c)
            let dt = cm - rotC + t  // W(x) = Ry·x + (c − Ry·c + t)
            pose.position = SIMD3<Float>(pose.position.x * c + pose.position.z * s, pose.position.y,
                                         -pose.position.x * s + pose.position.z * c) + dt
            pose.yawRad += dTheta
            let rmse = (use.reduce(0) { $0 + $1.d * $1.d } / Float(use.count)).squareRoot()
            if abs(lastRmse - rmse) < 1e-5 && abs(dTheta) < 1e-4 { break }
            lastRmse = rmse
        }
        let after = score(model: model, live: live, pose: pose, corr: corr)
        let shift = simd_distance(pose.position, start.position)
        var dy = (pose.yawRad - start.yawRad) * 180 / .pi
        while dy > 180 { dy -= 360 }; while dy < -180 { dy += 360 }
        var ok = true, why = "ok"
        if shift > maxShiftM { ok = false; why = "이동 \(Int(shift * 100))cm 초과" }
        else if abs(dy) > maxYawDeg { ok = false; why = "회전 \(Int(abs(dy)))° 초과" }
        else if after.inlier + 0.02 < before.inlier { ok = false; why = "일치율 하락" }
        else if after.inlier < 0.25 { ok = false; why = "일치율 낮음" }
        return RefineResult(pose: ok ? pose : start, rmse: after.rmse, inlier: after.inlier, shiftM: shift, yawDeg: dy, accepted: ok, reason: why)
    }

    nonisolated static func solve4(_ A: [Float], _ b: [Float]) -> [Float]? {
        var m = A, r = b
        for col in 0..<4 {
            var piv = col
            for i in col..<4 where abs(m[i * 4 + col]) > abs(m[piv * 4 + col]) { piv = i }
            if abs(m[piv * 4 + col]) < 1e-9 { return nil }
            if piv != col { for j in 0..<4 { m.swapAt(col * 4 + j, piv * 4 + j) }; r.swapAt(col, piv) }
            for i in (col + 1)..<4 where i < 4 {
                let f = m[i * 4 + col] / m[col * 4 + col]
                for j in col..<4 { m[i * 4 + j] -= f * m[col * 4 + j] }
                r[i] -= f * r[col]
            }
        }
        var x = [Float](repeating: 0, count: 4)
        for i in stride(from: 3, through: 0, by: -1) {
            var s = r[i]
            for j in (i + 1)..<4 where j < 4 { s -= m[i * 4 + j] * x[j] }
            x[i] = s / m[i * 4 + i]
        }
        return x
    }

    /// 점마다 이웃(반경 radius)의 주성분 중 가장 작은 방향 = 표면 법선 (부호는 의미 없음 — 점-면 오차는 제곱)
    nonisolated static func estimateNormals(_ pts: [SIMD3<Float>], radius: Float = 0.03) -> [SIMD3<Float>] {
        let grid = Grid(points: pts, cell: radius)
        return pts.map { p in
            let nb = grid.neighbors(of: p, within: radius)
            guard nb.count >= 6 else { return SIMD3(0, 1, 0) }
            var mean = SIMD3<Float>.zero
            for q in nb { mean += q }
            mean /= Float(nb.count)
            var c = [Float](repeating: 0, count: 6)  // xx xy xz yy yz zz
            for q in nb { let d = q - mean; c[0] += d.x * d.x; c[1] += d.x * d.y; c[2] += d.x * d.z; c[3] += d.y * d.y; c[4] += d.y * d.z; c[5] += d.z * d.z }
            return smallestEigenvector(c)
        }
    }

    /// 대칭 3×3 공분산의 최소 고유벡터 (Jacobi 회전)
    nonisolated static func smallestEigenvector(_ c: [Float]) -> SIMD3<Float> {
        var a: [[Float]] = [[c[0], c[1], c[2]], [c[1], c[3], c[4]], [c[2], c[4], c[5]]]
        var v: [[Float]] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        for _ in 0..<8 {
            for (p, q) in [(0, 1), (0, 2), (1, 2)] {
                if abs(a[p][q]) < 1e-12 { continue }
                let th = 0.5 * atan2(2 * a[p][q], a[q][q] - a[p][p])
                let cs = cos(th), sn = sin(th)
                for k in 0..<3 { let akp = a[k][p], akq = a[k][q]; a[k][p] = cs * akp - sn * akq; a[k][q] = sn * akp + cs * akq }
                for k in 0..<3 { let apk = a[p][k], aqk = a[q][k]; a[p][k] = cs * apk - sn * aqk; a[q][k] = sn * apk + cs * aqk }
                for k in 0..<3 { let vkp = v[k][p], vkq = v[k][q]; v[k][p] = cs * vkp - sn * vkq; v[k][q] = sn * vkp + cs * vkq }
            }
        }
        var m = 0
        for i in 1..<3 where a[i][i] < a[m][m] { m = i }
        return simd_normalize(SIMD3(v[0][m], v[1][m], v[2][m]))
    }

    /// 포즈를 적용했을 때 관측 점의 모델과의 거리: (rmse, 일치율)
    nonisolated static func score(model: [SIMD3<Float>], live: [SIMD3<Float>], pose: RefinePose, corr: Float) -> (rmse: Float, inlier: Float) {
        let grid = Grid(points: transformed(model, pose), cell: corr)
        var n = 0, inl = 0
        var s: Float = 0
        for o in live {
            if let (_, d) = grid.nearest(to: o, within: corr) { n += 1; s += d * d; if d <= inlierDist { inl += 1 } }
        }
        return (n > 0 ? (s / Float(n)).squareRoot() : .infinity, Float(inl) / Float(max(live.count, 1)))
    }

    nonisolated static func transformed(_ model: [SIMD3<Float>], _ pose: RefinePose) -> [SIMD3<Float>] {
        let q = pose.orientation
        return model.map { q.act($0) + pose.position }
    }

    /// 균일 격자 최근접 탐색 (셀 크기 = 탐색 반경)
    struct Grid {
        let cell: Float
        let pts: [SIMD3<Float>]
        var buckets: [Int64: [Int]] = [:]
        init(points: [SIMD3<Float>], cell: Float) {
            self.cell = cell
            self.pts = points
            for (i, p) in points.enumerated() { buckets[Self.key(p, cell), default: []].append(i) }
        }
        nonisolated static func key(_ p: SIMD3<Float>, _ cell: Float) -> Int64 {
            Self.key(Int64(floor(p.x / cell)), Int64(floor(p.y / cell)), Int64(floor(p.z / cell)))
        }
        nonisolated static func key(_ x: Int64, _ y: Int64, _ z: Int64) -> Int64 { (x & 0x1FFFFF) | ((y & 0x1FFFFF) << 21) | ((z & 0x1FFFFF) << 42) }
        nonisolated func nearest(to q: SIMD3<Float>, within r: Float) -> (SIMD3<Float>, Float)? {
            guard let (i, d) = nearestIndex(to: q, within: r) else { return nil }
            return (pts[i], d)
        }
        nonisolated func nearestIndex(to q: SIMD3<Float>, within r: Float) -> (Int, Float)? {
            let cx = Int64(floor(q.x / cell)), cy = Int64(floor(q.y / cell)), cz = Int64(floor(q.z / cell))
            var best = -1
            var bd = r * r
            for dx in -1...1 { for dy in -1...1 { for dz in -1...1 {
                guard let b = buckets[Self.key(cx + Int64(dx), cy + Int64(dy), cz + Int64(dz))] else { continue }
                for i in b {
                    let d = simd_distance_squared(pts[i], q)
                    if d < bd { bd = d; best = i }
                }
            } } }
            return best >= 0 ? (best, bd.squareRoot()) : nil
        }
        nonisolated func neighbors(of q: SIMD3<Float>, within r: Float) -> [SIMD3<Float>] {
            let cx = Int64(floor(q.x / cell)), cy = Int64(floor(q.y / cell)), cz = Int64(floor(q.z / cell))
            var out: [SIMD3<Float>] = []
            for dx in -1...1 { for dy in -1...1 { for dz in -1...1 {
                guard let b = buckets[Self.key(cx + Int64(dx), cy + Int64(dy), cz + Int64(dz))] else { continue }
                for i in b where simd_distance_squared(pts[i], q) <= r * r { out.append(pts[i]) }
            } } }
            return out
        }
    }
}
