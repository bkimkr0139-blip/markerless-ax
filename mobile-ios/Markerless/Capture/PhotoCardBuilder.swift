// PhotoCardBuilder.swift — 등록한 사물의 둘레 64방향 흑백 실루엣 외곽선 카드
//
// 검수 때 카메라를 돌려도 실물과 같은 방향·같은 크기로 겹쳐 보이게, 사물 둘레를 5.625° 간격(64컷, 수평)으로 돈다.
// 카드는 사진이 아니라 3D 점의 실루엣이다 — 사진처럼 보이면 CAD/BIM처럼 보이지 않고, 사진 배경 분리(Vision)는
// 방향마다 크기가 들쭉날쭉했다(로그: 앞 24cm / 옆 9~12cm, 실물 17cm). LiDAR 깊이 점은 mm 정확도라 투영 실루엣이 정확하다.
//
// 방향마다:
//  1. 가상 카메라: 사물 중심에서 그 방향으로 1.0m 떨어진 수평 시점(세계 위쪽이 위)
//  2. 사물 점을 시선에 수직인 평면(사물 중심 깊이)에 원근 투영 → 점 도장(dilate) → 구멍 메우기 → 같은 반경만큼 침식(closing)
//     — 점 사이 틈은 메우고 크기는 그대로 유지
//  3. 흑백: 가는 흰 외곽선(~1%) + 속은 아주 옅은 검정. 알파로 배경은 투명
//  4. PNG + 카드 배치(자산 로컬 프레임, 촬영 방향 forward)를 저장

import CoreGraphics
import ImageIO
import simd
import UniformTypeIdentifiers

struct PhotoCard {
    var side: String  // a00..a23 (a00 = 앞, 시계 방향으로 15°씩)
    var azimuthDeg: Float
    var png: Data
    var center: SIMD3<Float>  // 자산 로컬 프레임
    var right: SIMD3<Float>
    var up: SIMD3<Float>
    var forward: SIMD3<Float>  // 이 방향에서 사물을 바라보는 시선
    var widthM: Float
    var heightM: Float
}

enum PhotoCardBuilder {
    /// 둘레 컷 수 (64컷 = 5.625° 간격). 카메라를 돌려도 거의 같은 방향·크기로 맞춘다
    static let count = 64
    /// 스캔 중 "한 바퀴 돌았나" 링 칸 수 (사람이 채우는 안내용이라 24칸이면 충분)
    static let ringCount = 24
    static let sides: [String] = (0..<count).map { String(format: "a%02d", $0) }
    static let viewDistance: Float = 1.0

    nonisolated static func azimuth(index: Int) -> Float { Float(index) * 360 / Float(count) }

    /// 방향 index에서 관찰자가 서는 수평 방향. OBB 축 u,v는 서버 외형 뷰와 같은 정의: front=−v, right=+u, back=+v, left=−u
    nonisolated static func viewerDir(azimuthDeg: Float, yawDeg: Float) -> SIMD3<Float> {
        let th = yawDeg * .pi / 180
        let u = SIMD3<Float>(cos(th), 0, sin(th))
        let v = SIMD3<Float>(-sin(th), 0, cos(th))
        let a = azimuthDeg * .pi / 180
        return -v * cos(a) + u * sin(a)  // 0°=앞(−v), 90°=오른쪽(+u), 180°=뒤(+v), 270°=왼쪽(−u)
    }

    // MARK: - 카드 생성

    /// objectPoints: 사물의 세션 좌표 점. stampM: 점 도장 반경(m) — LiDAR 0.012, VIO 0.025(점이 성기다).
    nonisolated static func build(index: Int, objectPoints: [SIMD3<Float>], obbCenter: SIMD3<Float>, yawDeg: Float,
                                  origin: SIMD3<Float>, stampM: Float) -> PhotoCard? {
        guard objectPoints.count >= 30 else { return nil }
        let az = azimuth(index: index)
        let vd = viewerDir(azimuthDeg: az, yawDeg: yawDeg)
        let f = -vd  // 시선: 관찰자 → 사물
        let C = obbCenter + vd * viewDistance
        let up = SIMD3<Float>(0, 1, 0)  // f가 수평이라 up은 이미 f에 수직
        let right = simd_normalize(simd_cross(f, up))
        let dz = viewDistance  // 평면 = 사물 중심 깊이
        let Pc = C + f * dz

        // 원근 투영: 카메라→점 광선과 평면의 교점 (a: right, b: up)
        var ab: [(Float, Float)] = []
        ab.reserveCapacity(objectPoints.count)
        for p in objectPoints {
            let d = p - C
            let dd = simd_dot(d, f)
            guard dd > 0.1 else { continue }
            let q = C + d * (dz / dd) - Pc
            ab.append((simd_dot(q, right), simd_dot(q, up)))
        }
        guard ab.count >= 30 else { return nil }
        let pad = stampM * 2.2 + 0.01
        let amin = ab.map(\.0).min()! - pad, amax = ab.map(\.0).max()! + pad
        let bmin = ab.map(\.1).min()! - pad, bmax = ab.map(\.1).max()! + pad
        let wa = amax - amin, hb = bmax - bmin
        let res = max(max(wa, hb) / 300, 0.0004)  // m/px: 긴 변 약 300px
        let W = Int(ceil(wa / res)), H = Int(ceil(hb / res))
        guard W >= 16, H >= 16, W <= 420, H <= 420 else { return nil }

        // 1) 점 도장 (원) — 점이 많으면 균등하게 줄여 비용을 고정한다
        let r = max(3, Int((stampM / res).rounded()))
        var mask = [UInt8](repeating: 0, count: W * H)
        let step = max(1, ab.count / 5000)
        for k in stride(from: 0, to: ab.count, by: step) {
            let ci = Int((ab[k].0 - amin) / res), cj = Int((bmax - ab[k].1) / res)
            for dj in -r...r {
                let j = cj + dj
                guard j >= 0, j < H else { continue }
                let span = Int(Double(r * r - dj * dj).squareRoot())
                for i in max(0, ci - span)...min(W - 1, ci + span) { mask[j * W + i] = 1 }
            }
        }

        // 2) 구멍 메우기 (바깥에서 flood fill → 닿지 않은 빈 곳은 사물 안쪽)
        fillHoles(&mask, W: W, H: H)

        // 3) 침식: 도장으로 부푼 만큼(r) 되돌린다 → 틈은 메우고 실제 크기는 유지 (chamfer 거리변환)
        erode(&mask, W: W, H: H, radius: Float(r) * 0.92)
        var area = 0
        for m in mask where m != 0 { area += 1 }
        guard area >= 80 else { return nil }

        // 사물 영역 bounding box
        var x0 = W, y0 = H, x1 = -1, y1 = -1
        for j in 0..<H { for i in 0..<W where mask[j * W + i] != 0 {
            x0 = min(x0, i); x1 = max(x1, i); y0 = min(y0, j); y1 = max(y1, j)
        } }
        guard x1 > x0, y1 > y0 else { return nil }
        // 4) 외곽선: 마스크 경계까지의 거리(안·밖 모두)로 가는 안티앨리어싱 선을 그린다.
        //    흐림(blur) 방식은 가늘게 하면 점선처럼 끊겨서, 거리 기반으로 연속된 가는 선(약 2px)을 만든다.
        let lineHalf: Float = 1.0  // 선 반폭(px). 300px 카드 기준 긴 변의 약 0.3%
        let pad2 = 4
        x0 = max(0, x0 - pad2); y0 = max(0, y0 - pad2); x1 = min(W - 1, x1 + pad2); y1 = min(H - 1, y1 + pad2)
        let bw = x1 - x0 + 1, bh = y1 - y0 + 1
        // 경계의 픽셀 계단을 줄이기 위해 살짝 매끄럽게 (블러 후 0.5로 이진화)
        var sm = mask.map { $0 != 0 ? Float(1) : 0 }
        boxBlur(&sm, W: W, H: H, radius: 2)
        let smooth = sm.map { $0 >= 0.5 }
        let dIn = chamfer(seed: smooth.map { !$0 }, W: W, H: H)   // 사물 안쪽 픽셀 → 배경까지 거리
        let dOut = chamfer(seed: smooth, W: W, H: H)              // 배경 픽셀 → 사물까지 거리
        var rgba = [UInt8](repeating: 0, count: bw * bh * 4)
        for j in 0..<bh {
            for i in 0..<bw {
                let o = (y0 + j) * W + (x0 + i)
                let inside = smooth[o]
                let dist = max(0, (inside ? dIn[o] : dOut[o]) - 0.5)  // 경계까지 거리
                let line = max(0, 1 - dist / lineHalf)
                let fill: Float = inside ? 0.14 : 0  // 속은 아주 옅은 검정
                let a = line + (1 - line) * fill
                let k = (j * bw + i) * 4
                let g = UInt8(max(0, min(255, line * 255)))  // 프리멀티플라이드: 흰 선만 밝기에 기여, 속은 검정
                rgba[k] = g; rgba[k + 1] = g; rgba[k + 2] = g
                rgba[k + 3] = UInt8(max(0, min(255, a * 255)))
            }
        }
        guard let png = encodePNG(rgba: rgba, w: bw, h: bh) else { return nil }

        // 카드 배치: bounding box 중심
        let ac = amin + (Float(x0) + Float(bw) / 2) * res
        let bc = bmax - (Float(y0) + Float(bh) / 2) * res
        let centerWorld = Pc + right * ac + up * bc
        return PhotoCard(side: sides[index], azimuthDeg: az, png: png, center: centerWorld - origin, right: right, up: up,
                         forward: f, widthM: Float(bw) * res, heightM: Float(bh) * res)
    }

    // MARK: - 래스터 유틸

    /// 가장자리에서 flood fill로 바깥을 찾고, 바깥이 아닌 모든 칸을 채운다
    nonisolated static func fillHoles(_ m: inout [UInt8], W: Int, H: Int) {
        var outside = [Bool](repeating: false, count: W * H)
        var stack: [Int] = []
        func push(_ i: Int, _ j: Int) {
            guard i >= 0, j >= 0, i < W, j < H else { return }
            let o = j * W + i
            if m[o] == 0, !outside[o] { outside[o] = true; stack.append(o) }
        }
        for i in 0..<W { push(i, 0); push(i, H - 1) }
        for j in 0..<H { push(0, j); push(W - 1, j) }
        while let o = stack.popLast() {
            let i = o % W, j = o / W
            push(i + 1, j); push(i - 1, j); push(i, j + 1); push(i, j - 1)
        }
        for o in 0..<(W * H) where !outside[o] { m[o] = 1 }
    }

    /// seed 픽셀까지의 거리(px, chamfer 1/1.414). seed 자신은 0
    nonisolated static func chamfer(seed: [Bool], W: Int, H: Int) -> [Float] {
        var d = seed.map { $0 ? Float(0) : 1e6 }
        for j in 0..<H {
            for i in 0..<W {
                let o = j * W + i
                if d[o] == 0 { continue }
                var v = d[o]
                if i > 0 { v = min(v, d[o - 1] + 1) }
                if j > 0 {
                    v = min(v, d[o - W] + 1)
                    if i > 0 { v = min(v, d[o - W - 1] + 1.414) }
                    if i < W - 1 { v = min(v, d[o - W + 1] + 1.414) }
                }
                d[o] = v
            }
        }
        for j in stride(from: H - 1, through: 0, by: -1) {
            for i in stride(from: W - 1, through: 0, by: -1) {
                let o = j * W + i
                var v = d[o]
                if i < W - 1 { v = min(v, d[o + 1] + 1) }
                if j < H - 1 {
                    v = min(v, d[o + W] + 1)
                    if i < W - 1 { v = min(v, d[o + W + 1] + 1.414) }
                    if i > 0 { v = min(v, d[o + W - 1] + 1.414) }
                }
                d[o] = v
            }
        }
        return d
    }

    /// 배경까지 거리(chamfer 3-4)가 radius 미만인 칸을 지운다 (원형 침식)
    nonisolated static func erode(_ m: inout [UInt8], W: Int, H: Int, radius: Float) {
        let inf: Float = 1e6
        var d = m.map { $0 == 0 ? Float(0) : inf }
        func relax(_ o: Int, _ n: Int, _ w: Float) { if d[n] + w < d[o] { d[o] = d[n] + w } }
        for j in 0..<H {
            for i in 0..<W {
                let o = j * W + i
                if d[o] == 0 { continue }
                if i == 0 || j == 0 || i == W - 1 || j == H - 1 { d[o] = 1; continue }  // 화면 밖은 배경
                relax(o, o - 1, 1); relax(o, o - W, 1); relax(o, o - W - 1, 1.414); relax(o, o - W + 1, 1.414)
            }
        }
        for j in stride(from: H - 1, through: 0, by: -1) {
            for i in stride(from: W - 1, through: 0, by: -1) {
                let o = j * W + i
                if d[o] == 0 || i == 0 || j == 0 || i == W - 1 || j == H - 1 { continue }
                relax(o, o + 1, 1); relax(o, o + W, 1); relax(o, o + W - 1, 1.414); relax(o, o + W + 1, 1.414)
            }
        }
        for o in 0..<(W * H) where m[o] != 0 && d[o] < radius { m[o] = 0 }
    }

    /// 분리형 박스 블러 (적분 없이 이동 합)
    nonisolated static func boxBlur(_ a: inout [Float], W: Int, H: Int, radius r: Int) {
        guard r > 0 else { return }
        var tmp = a
        let n = Float(2 * r + 1)
        for j in 0..<H {
            var s: Float = 0
            for i in -r...r { s += a[j * W + min(max(i, 0), W - 1)] }
            for i in 0..<W {
                tmp[j * W + i] = s / n
                s += a[j * W + min(i + r + 1, W - 1)] - a[j * W + max(i - r, 0)]
            }
        }
        for i in 0..<W {
            var s: Float = 0
            for j in -r...r { s += tmp[min(max(j, 0), H - 1) * W + i] }
            for j in 0..<H {
                a[j * W + i] = s / n
                s += tmp[min(j + r + 1, H - 1) * W + i] - tmp[max(j - r, 0) * W + i]
            }
        }
    }

    nonisolated static func encodePNG(rgba: [UInt8], w: Int, h: Int) -> Data? {
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        var buf = rgba
        let cg: CGImage? = buf.withUnsafeMutableBytes { raw in
            CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                      space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)?.makeImage()
        }
        guard let cg else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, cg, nil)
        return CGImageDestinationFinalize(dest) ? out as Data : nil
    }
}
