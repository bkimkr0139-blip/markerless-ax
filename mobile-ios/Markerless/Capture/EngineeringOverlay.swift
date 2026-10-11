// EngineeringOverlay.swift — 엔지니어링 도면 느낌의 AR 오버레이: 치수선·mm 표기, 사진 실루엣 카드
//
// - 치수선: 외곽 박스(OBB)의 가로·깊이·높이에 보조선 + 사선 눈금 + mm 치수 (제도 방식). 글자는 항상 카메라를 향한다.
// - 사진 카드: 등록 때 4방향에서 찍은 사진을 사물만 오려 흑백 처리한 실루엣. 현재 보는 방향과 가장 가까운 카드를 보여 준다.

import RealityKit
import simd
import UIKit

/// 등록 때 저장한 방향별 흑백 실루엣 카드 (자산 로컬 프레임) + 텍스처
struct PhotoCardAsset {
    var side: String  // a00..a23 (앞=a00, 시계 방향 15°씩) 또는 구버전 front|right|back|left
    var center: SIMD3<Float>
    var right: SIMD3<Float>
    var up: SIMD3<Float>
    var forward: SIMD3<Float>  // 촬영 당시 카메라가 사물을 바라본 방향
    var widthM: Float
    var heightM: Float
    var texture: TextureResource
}

/// 카메라 방향에 따라 보였다 숨겼다 하는 카드
struct OverlayCard {
    weak var node: Entity?
    weak var root: Entity?
    var forwardLocal: SIMD3<Float>
    var side: String = ""
}

/// 카드 한 장: 촬영 당시 카메라 쪽(−forward)을 향하는 평면. 알파 PNG라 사물 윤곽대로만 보인다.
@MainActor
func makeCardEntity(_ c: PhotoCardAsset, opacity: Float = 0.8) -> Entity {
    let mesh = MeshResource.generatePlane(width: c.widthM, height: c.heightM)
    var m = UnlitMaterial()
    m.color = .init(tint: .white, texture: .init(c.texture))
    m.blending = .transparent(opacity: .init(floatLiteral: opacity))
    let e = ModelEntity(mesh: mesh, materials: [m])
    e.position = c.center
    // 평면의 +Z(법선)가 카메라 쪽(−forward)이 되도록: z = right × up = −forward
    e.orientation = simd_quatf(simd_float3x3(columns: (c.right, c.up, simd_cross(c.right, c.up))))
    return e
}

/// 외곽 박스의 치수선 + mm 표기. 반환한 labels는 매 프레임 카메라를 향하게 해야 한다.
@MainActor
func makeDimensionEntity(_ obb: OutlineJSON.OBB, color: UIColor, thickness: Float = 0.0003) -> (root: Entity, labels: [Entity]) {
    let root = Entity()
    guard obb.corners.count == 8 else { return (root, []) }
    let c = obb.corners.map { SIMD3<Float>(Float($0[0]), Float($0[1]), Float($0[2])) }
    // 코너 인덱스 = sx*4 + sy*2 + sz  (sx: u축, sy: 높이, sz: v축)
    let c0 = c[0], cU = c[4], cV = c[1], cH = c[2]
    let L = simd_distance(c0, cU), W = simd_distance(c0, cV), H = simd_distance(c0, cH)
    guard L > 0.01, W > 0.005, H > 0.01 else { return (root, []) }
    let u = (cU - c0) / L, v = (cV - c0) / W

    let off: Float = 0.05      // 치수선이 사물에서 떨어진 거리
    let gap: Float = 0.008     // 보조선이 사물에서 떨어지는 간격
    let over: Float = 0.012    // 보조선이 치수선을 넘는 길이
    let tick: Float = 0.014    // 사선 눈금 길이
    var segs: [(SIMD3<Float>, SIMD3<Float>)] = []
    var labels: [Entity] = []

    func label(_ text: String, at p: SIMD3<Float>) -> Entity {
        let mesh = MeshResource.generateText(text, extrusionDepth: 0.0003, font: .systemFont(ofSize: 0.016),
                                             containerFrame: .zero, alignment: .center, lineBreakMode: .byClipping)
        let model = ModelEntity(mesh: mesh, materials: [UnlitMaterial(color: color)])
        let b = mesh.bounds
        model.position = [-b.center.x, -b.center.y, 0]  // 글자 중심을 홀더 원점으로
        let holder = Entity()
        holder.position = p
        holder.addChild(model)
        return holder
    }

    /// a→b 모서리의 치수. offsetDir은 사물 바깥 방향(단위벡터)
    func dimension(_ a: SIMD3<Float>, _ b: SIMD3<Float>, offsetDir d: SIMD3<Float>, value: Float) {
        let a2 = a + d * off, b2 = b + d * off
        segs.append((a2, b2))                                              // 치수선
        segs.append((a + d * gap, a2 + d * over))                          // 보조선
        segs.append((b + d * gap, b2 + d * over))
        let tickDir = simd_normalize(simd_normalize(b - a) + d) * (tick / 2)  // 사선 눈금(제도 방식)
        segs.append((a2 - tickDir, a2 + tickDir))
        segs.append((b2 - tickDir, b2 + tickDir))
        labels.append(label("\(Int((value * 1000).rounded()))", at: (a2 + b2) / 2 + d * 0.016))
    }

    dimension(c0, cU, offsetDir: -v, value: L)                              // 길이
    dimension(c0, cV, offsetDir: -u, value: W)                              // 깊이
    dimension(c0, cH, offsetDir: simd_normalize(-u - v), value: H)          // 높이

    if let m = lineMesh(segs, thickness: thickness) {
        root.addChild(ModelEntity(mesh: m, materials: [UnlitMaterial(color: color)]))
    }
    for l in labels { root.addChild(l) }
    return (root, labels)
}
