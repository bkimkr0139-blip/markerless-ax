// SurfelMesh.swift — LiDAR 없는 기기(카메라+IMU VIO)용: 특징점 → 표면 조각(surfel) 메시
//
// ARKit rawFeaturePoints는 점뿐이라 삼각형이 없다. 서버(GLB 표면 샘플링 + 면 법선)가 요구하는
// 형태로 맞추기 위해 점마다 접평면 위의 작은 정삼각형을 만든다.
// 법선 = 이웃 k개 점의 공분산 최소 고유벡터(PCA), 시점(viewpoint) 쪽으로 정렬.

import simd

enum SurfelMesh {
    /// points: 자산 로컬 좌표(중심 원점). viewpoint: 같은 좌표계의 촬영 위치(법선 방향 정렬용)
    static func build(points: [SIMD3<Float>], viewpoint: SIMD3<Float>,
                      maxPoints: Int = 2000, k: Int = 10, size: Float = 0.012)
        -> (positions: [SIMD3<Float>], indices: [UInt32]) {
        var pts = points
        if pts.count > maxPoints {
            let step = Float(pts.count) / Float(maxPoints)
            pts = (0..<maxPoints).map { points[Int(Float($0) * step)] }
        }
        guard pts.count >= 4 else { return ([], []) }
        let kk = min(k, pts.count - 1)

        var positions: [SIMD3<Float>] = []
        var indices: [UInt32] = []
        positions.reserveCapacity(pts.count * 3)
        indices.reserveCapacity(pts.count * 3)

        for (i, p) in pts.enumerated() {
            // k 최근접 이웃 (브루트포스 — maxPoints 상한으로 비용 고정)
            var best: [(d: Float, j: Int)] = []
            best.reserveCapacity(kk + 1)
            for (j, q) in pts.enumerated() where j != i {
                let d = simd_distance_squared(p, q)
                if best.count < kk {
                    best.append((d, j))
                    if best.count == kk { best.sort { $0.d < $1.d } }
                } else if d < best[kk - 1].d {
                    best[kk - 1] = (d, j)
                    best.sort { $0.d < $1.d }
                }
            }
            var n = estimateNormal(center: p, neighbors: best.map { pts[$0.j] })
            if simd_dot(n, viewpoint - p) < 0 { n = -n }

            let ref: SIMD3<Float> = abs(n.y) < 0.9 ? SIMD3(0, 1, 0) : SIMD3(1, 0, 0)
            let u = simd_normalize(simd_cross(n, ref))
            let v = simd_cross(n, u)
            let base = UInt32(positions.count)
            positions.append(p + size * u)
            positions.append(p + size * (-0.5 * u + 0.866 * v))
            positions.append(p + size * (-0.5 * u - 0.866 * v))  // 권선 순서 = +n 방향
            indices.append(contentsOf: [base, base + 1, base + 2])
        }
        return (positions, indices)
    }

    /// 서버가 정제한 점+바깥쪽 법선으로 바로 surfel 생성 (PCA 불필요 → 점을 더 많이 쓸 수 있다)
    static func build(points: [SIMD3<Float>], normals: [SIMD3<Float>], maxPoints: Int = 3500, size: Float = 0.012)
        -> (positions: [SIMD3<Float>], indices: [UInt32]) {
        guard points.count == normals.count, points.count >= 4 else { return ([], []) }
        var pts = points, nrm = normals
        if pts.count > maxPoints {
            let step = Float(pts.count) / Float(maxPoints)
            let sel = (0..<maxPoints).map { Int(Float($0) * step) }
            pts = sel.map { points[$0] }
            nrm = sel.map { normals[$0] }
        }
        var positions: [SIMD3<Float>] = []
        var indices: [UInt32] = []
        positions.reserveCapacity(pts.count * 3)
        indices.reserveCapacity(pts.count * 3)
        for (p, n0) in zip(pts, nrm) {
            let n = simd_length(n0) > 1e-6 ? simd_normalize(n0) : SIMD3<Float>(0, 1, 0)
            let ref: SIMD3<Float> = abs(n.y) < 0.9 ? SIMD3(0, 1, 0) : SIMD3(1, 0, 0)
            let u = simd_normalize(simd_cross(n, ref))
            let v = simd_cross(n, u)
            let base = UInt32(positions.count)
            positions.append(p + size * u)
            positions.append(p + size * (-0.5 * u + 0.866 * v))
            positions.append(p + size * (-0.5 * u - 0.866 * v))
            indices.append(contentsOf: [base, base + 1, base + 2])
        }
        return (positions, indices)
    }

    /// 공분산의 최소 고유벡터. (tI − C)의 최대 고유벡터를 거듭제곱법으로 구한다.
    private static func estimateNormal(center: SIMD3<Float>, neighbors: [SIMD3<Float>]) -> SIMD3<Float> {
        var mean = center
        for q in neighbors { mean += q }
        mean /= Float(neighbors.count + 1)
        var cov = simd_float3x3()
        for q in neighbors + [center] {
            let d = q - mean
            cov += simd_float3x3(columns: (d * d.x, d * d.y, d * d.z))
        }
        let trace = cov.columns.0.x + cov.columns.1.y + cov.columns.2.z
        guard trace > 1e-12 else { return SIMD3(0, 1, 0) }
        let shifted = simd_float3x3(diagonal: SIMD3(repeating: trace)) - cov
        var x = simd_normalize(SIMD3<Float>(0.577, 0.577, 0.577) + SIMD3(0.01, -0.02, 0.03))
        for _ in 0..<24 {
            let y = shifted * x
            let len = simd_length(y)
            if len < 1e-12 { break }
            x = y / len
        }
        return x
    }
}
