// DepthFusion.swift — LiDAR 기기(iPhone 12 Pro 이상)용 깊이 융합
//
// ARKit sceneDepth(256×192, 미터) + confidence 맵을 프레임마다 월드 좌표로 역투영해 6mm 복셀에 누적한다.
// - confidence가 high인 픽셀만, 0.25~2.5m 범위만 사용 (먼 거리는 LiDAR 오차가 커진다)
// - 복셀마다 위치 평균 + 관측 횟수: 2회 이상 관측된 복셀만 '안정 점'으로 쓴다 (한 프레임 노이즈 제거)
// - ARMeshAnchor 메시(삼각형 한 변 수 cm)보다 훨씬 촘촘하고 정확해서 사물 분리·외곽선·ICP 정합 품질이 올라간다
// 폰 고정 상태에서도 같은 복셀을 반복 관측하므로 안정 점이 빨리 쌓인다 (VIO 특징점과 달리 이동이 필수가 아니다).

import CoreVideo
import Foundation
import simd

final class DepthFusion: @unchecked Sendable {
    struct Cell { var pos: SIMD3<Float>; var n: UInt16 }

    let voxel: Float = 0.006
    let minDepth: Float = 0.25
    let maxDepth: Float = 2.5
    let cap = 600_000
    /// 안정 점 기준 관측 횟수
    let minN: UInt16 = 2

    private let q = DispatchQueue(label: "markerless.depthfusion", qos: .userInitiated)
    private let lock = NSLock()
    private var cells: [Int64: Cell] = [:]
    private var stable = 0
    private var busy = false
    /// 대상 주변만 누적한다 (POI를 먼저 지정한 스캔) — 주변 사물·바닥·벽이 섞이지 않고 메모리도 아낀다
    private var focus: (c: SIMD3<Float>, r2: Float)?

    private func currentFocus() -> (c: SIMD3<Float>, r2: Float)? { lock.lock(); defer { lock.unlock() }; return focus }

    /// center=nil이면 해제. 설정하면 반경 밖에 이미 쌓인 점은 버린다.
    func setFocus(center: SIMD3<Float>?, radius: Float) {
        lock.lock(); focus = center.map { ($0, radius * radius) }; lock.unlock()
        guard let c = center else { return }
        q.async { [self] in
            let r2 = radius * radius
            cells = cells.filter { simd_distance_squared($0.value.pos, c) <= r2 }
            let s = cells.values.reduce(0) { $0 + ($1.n >= minN ? 1 : 0) }
            lock.lock(); stable = s; lock.unlock()
        }
    }

    /// 현재 안정 점 수 (스레드 안전, UI 표시용)
    var stableCount: Int { lock.lock(); defer { lock.unlock() }; return stable }

    private func setBusy(_ v: Bool) -> Bool {
        lock.lock(); defer { lock.unlock() }
        if v && busy { return false }
        busy = v
        return true
    }

    func reset() {
        q.sync {
            cells.removeAll(keepingCapacity: false)
            lock.lock(); stable = 0; lock.unlock()
        }
    }

    // MARK: - 누적

    /// 한 프레임 누적. 이전 프레임을 아직 처리 중이면 이 프레임은 건너뛴다(밀리지 않게).
    /// imageSize/intrinsics는 capturedImage 기준 — 깊이 해상도에 맞춰 비율 보정한다.
    func integrate(depth: PixelBufferBox, confidence: PixelBufferBox?, intrinsics K: simd_float3x3,
                   imageSize: CGSize, transform: simd_float4x4) {
        guard setBusy(true) else { return }
        let fo = currentFocus()
        q.async { [self] in
            defer { _ = setBusy(false) }
            let dmap = depth.buffer
            CVPixelBufferLockBaseAddress(dmap, .readOnly)
            defer { CVPixelBufferUnlockBaseAddress(dmap, .readOnly) }
            guard let dBase = CVPixelBufferGetBaseAddress(dmap) else { return }
            let w = CVPixelBufferGetWidth(dmap), h = CVPixelBufferGetHeight(dmap)
            let dStride = CVPixelBufferGetBytesPerRow(dmap) / MemoryLayout<Float32>.size

            var cBase: UnsafeMutableRawPointer?
            var cStride = 0
            if let cm = confidence?.buffer {
                CVPixelBufferLockBaseAddress(cm, .readOnly)
                cBase = CVPixelBufferGetBaseAddress(cm)
                cStride = CVPixelBufferGetBytesPerRow(cm)
            }
            defer { if let cm = confidence?.buffer { CVPixelBufferUnlockBaseAddress(cm, .readOnly) } }

            let sx = Float(imageSize.width) / Float(w), sy = Float(imageSize.height) / Float(h)
            let fx = K[0][0], fy = K[1][1], cx = K[2][0], cy = K[2][1]
            let dp = dBase.assumingMemoryBound(to: Float32.self)
            let cp = cBase?.assumingMemoryBound(to: UInt8.self)
            let inv = 1 / voxel

            for y in stride(from: 1, to: h - 1, by: 1) {  // 대상 주변은 모든 픽셀 사용 (mm 단위 촘촘함)
                for x in stride(from: 1, to: w - 1, by: 1) {
                    let d = dp[y * dStride + x]
                    guard d.isFinite, d >= minDepth, d <= maxDepth else { continue }
                    if let cp, cp[y * cStride + x] < 2 { continue }  // ARConfidenceLevel.high
                    // 깊이 경계(물체 가장자리)의 끌림(flying pixel) 제거: 이웃과 깊이 차가 크면 버린다
                    let dl = dp[y * dStride + x - 1], dr = dp[y * dStride + x + 1]
                    let du = dp[(y - 1) * dStride + x], dd = dp[(y + 1) * dStride + x]
                    let tol = 0.02 + 0.01 * d
                    if abs(dl - d) > tol || abs(dr - d) > tol || abs(du - d) > tol || abs(dd - d) > tol { continue }
                    let px = (Float(x) + 0.5) * sx, py = (Float(y) + 0.5) * sy
                    let cam = SIMD4<Float>((px - cx) / fx * d, -(py - cy) / fy * d, -d, 1)  // ARKit 카메라: +x 오른쪽, +y 위, 카메라는 -z를 본다
                    let wp = transform * cam
                    let p = SIMD3<Float>(wp.x, wp.y, wp.z)
                    if let fo, simd_distance_squared(p, fo.c) > fo.r2 { continue }
                    let key = Self.key(p, inv)
                    if var c = cells[key] {
                        c.n = min(c.n + 1, 60)
                        c.pos += (p - c.pos) / Float(min(c.n, 24))  // 처음엔 누적 평균, 이후 지수 평균
                        cells[key] = c
                        if c.n == minN { lock.lock(); stable += 1; lock.unlock() }
                    } else {
                        cells[key] = Cell(pos: p, n: 1)
                    }
                }
            }
            if cells.count > cap { compact() }
        }
    }

    private static func key(_ p: SIMD3<Float>, _ inv: Float) -> Int64 {
        let ix = Int64(floor(p.x * inv)) & 0x1FFFFF
        let iy = Int64(floor(p.y * inv)) & 0x1FFFFF
        let iz = Int64(floor(p.z * inv)) & 0x1FFFFF
        return ix | (iy << 21) | (iz << 42)
    }

    /// 상한 초과: 1회 관측(미확정) 복셀부터 버리고, 그래도 많으면 절반을 버린다
    private func compact() {
        cells = cells.filter { $0.value.n >= minN }
        if cells.count > cap { for k in cells.keys.prefix(cells.count / 2) { cells.removeValue(forKey: k) } }
        let s = cells.values.reduce(0) { $0 + ($1.n >= minN ? 1 : 0) }
        lock.lock(); stable = s; lock.unlock()
    }

    // MARK: - 조회

    /// center 반경 안의 안정 점 (월드 좌표). maxPoints 초과 시 균등 간격으로 줄인다.
    func points(center: SIMD3<Float>, radius: Float, maxPoints: Int = .max) -> [SIMD3<Float>] {
        q.sync {
            let r2 = radius * radius
            var out: [SIMD3<Float>] = []
            for c in cells.values where c.n >= minN && simd_distance_squared(c.pos, center) <= r2 { out.append(c.pos) }
            guard out.count > maxPoints else { return out }
            let step = Float(out.count) / Float(maxPoints)
            return (0..<maxPoints).map { out[Int(Float($0) * step)] }
        }
    }

    func count(center: SIMD3<Float>, radius: Float) -> Int {
        q.sync {
            let r2 = radius * radius
            var n = 0
            for c in cells.values where c.n >= minN && simd_distance_squared(c.pos, center) <= r2 { n += 1 }
            return n
        }
    }

    // MARK: - 조준점 표면 (화면 중앙 깊이)

    /// 현재 프레임 깊이맵 중앙 창(±6px)에서 앞면 깊이(하위 25%)를 골라 월드 좌표로 반환.
    /// 메시 전체와 레이 교차하는 것보다 훨씬 싸고 정확하다.
    static func centerSurface(depth: CVPixelBuffer, confidence: CVPixelBuffer?, intrinsics K: simd_float3x3,
                              imageSize: CGSize, transform: simd_float4x4) -> SIMD3<Float>? {
        CVPixelBufferLockBaseAddress(depth, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depth, .readOnly) }
        guard let dBase = CVPixelBufferGetBaseAddress(depth) else { return nil }
        let w = CVPixelBufferGetWidth(depth), h = CVPixelBufferGetHeight(depth)
        let dStride = CVPixelBufferGetBytesPerRow(depth) / MemoryLayout<Float32>.size
        var cp: UnsafeMutablePointer<UInt8>?
        var cStride = 0
        if let c = confidence {
            CVPixelBufferLockBaseAddress(c, .readOnly)
            cp = CVPixelBufferGetBaseAddress(c)?.assumingMemoryBound(to: UInt8.self)
            cStride = CVPixelBufferGetBytesPerRow(c)
        }
        defer { if let c = confidence { CVPixelBufferUnlockBaseAddress(c, .readOnly) } }
        let dp = dBase.assumingMemoryBound(to: Float32.self)
        var ds: [Float] = []
        for y in (h / 2 - 6)...(h / 2 + 6) {
            for x in (w / 2 - 6)...(w / 2 + 6) {
                let d = dp[y * dStride + x]
                guard d.isFinite, d > 0.2, d < 4 else { continue }
                if let cp, cp[y * cStride + x] < 1 { continue }  // 중간 이상 신뢰
                ds.append(d)
            }
        }
        guard ds.count >= 12 else { return nil }
        ds.sort()
        let d = ds[ds.count / 4]
        let sx = Float(imageSize.width) / Float(w), sy = Float(imageSize.height) / Float(h)
        let px = (Float(w / 2) + 0.5) * sx, py = (Float(h / 2) + 0.5) * sy
        let cam = SIMD4<Float>((px - K[2][0]) / K[0][0] * d, -(py - K[2][1]) / K[1][1] * d, -d, 1)
        let wp = transform * cam
        return SIMD3<Float>(wp.x, wp.y, wp.z)
    }
}
