// ARScanView.swift — ARKit 세션 컨트롤러 + SwiftUI 래퍼
//
// 좌표 규약 (docs/COORDINATE.md):
// - 세션 월드 = ARKit Y-up 오른손. 월드맵 복원 성공 시 공간 프레임과 일치 (T_space_session ≈ Identity)
// - 클라우드/GLB는 모두 세션 월드 좌표로 생성 → frame="space"로 전송
// - GLB 오버레이는 designPose.position 배치만으로 정합 (glTF/ARKit 동일 규약, §2)

import ARKit
import RealityKit
import SwiftUI
import simd

@MainActor
final class ScanSessionController: NSObject, ObservableObject {
    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)

    @Published var trackingNormal = false
    @Published var meshTriangleCount = 0
    @Published var depthValidRatio: Double = 0
    @Published var statusText = "카메라 시작 대기"

    // 공간 전체 스캔(SpaceScanController)용 확장점 — 기존 사물 스캔·검수는 설정하지 않으므로 동작이 그대로다.
    /// 매 ARFrame마다 메인 액터에서 호출된다 (키프레임 수집용)
    var onFrame: ((ARFrame) -> Void)?
    /// 세션 중단(전화·앱 전환 등) 시작(true)/끝(false)
    var onInterruption: ((Bool) -> Void)?
    /// 중단 뒤 ARKit이 이전 지도로 재위치 인식을 시도할지 (공간 스캔: true — 좌표계 유지 시도)
    var attemptRelocalization = false
    /// 공간 스캔 '고화질 사진' 옵션: iOS 16+ 고해상도 캡처 권장 비디오 포맷을 쓴다 (기본 false — 기존 동작 그대로)
    var useHighResFormat = false

    /// 이 기기에서 고해상도 스틸 캡처를 쓸 수 있는가
    static var highResStillSupported: Bool {
        if #available(iOS 16.0, *) { return ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing != nil }
        return false
    }

    /// 고해상도 스틸 1장 요청. 미지원/실패면 nil을 돌려준다 (호출자는 조용히 일반 캡처로 대체). completion은 메인 액터.
    func captureHighResFrame(_ completion: @escaping @MainActor (ARFrame?) -> Void) {
        if #available(iOS 16.0, *), Self.highResStillSupported {
            arView.session.captureHighResolutionFrame { frame, _ in
                Task { @MainActor in completion(frame) }
            }
        } else {
            completion(nil)
        }
    }

    /// 현재 화면 스캔 모드: LiDAR 메시(true) / 카메라+IMU VIO 특징점(false)
    @Published var usesLiDARMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
    @Published var featurePointCount = 0
    /// 월드맵으로 시작한 세션이 실제로 재위치 인식(relocalization)에 성공했는가.
    /// 지도를 불러온 것만으로는 부족하다 — 성공 전 좌표는 어긋나 있어 위치 편차 판정에 쓰면 안 된다.
    @Published var relocalized = false
    @Published var awaitingRelocalization = false
    private var sawNonNormalSinceStart = false

    private var lastLoggedStatus = ""
    private var meshAnchorIDs = Set<UUID>()
    private var lastDepthUpdate = Date.distantPast

    /// LiDAR 깊이 융합 (13 Pro 등) — 메시보다 촘촘한 안정 점을 만든다. LiDAR 없으면 사용하지 않는다.
    let fusion = DepthFusion()
    private var lastFusionTime: TimeInterval = 0
    var hasLiDAR: Bool { usesLiDARMesh && ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) }
    /// 스캔 준비로 보는 최소 점 수 (LiDAR 깊이 점은 촘촘해서 기준이 훨씬 높다)
    var minScanPoints: Int { hasLiDAR ? 3000 : 150 }
    /// 검수 조준 사물 주변(35cm) 최소 점 수
    var minAimPoints: Int { hasLiDAR ? 400 : 60 }
    var scanPointLabel: String { hasLiDAR ? "깊이 점" : "특징점" }

    // MARK: - 대상(POI)을 먼저 지정하는 스캔: 3D 마커 + 둘레 커버리지 + 어시스트

    private(set) var target: SIMD3<Float>?
    var coverage = ScanCoverage()
    private var lastCoverTime: TimeInterval = 0
    private var lastHaptic = Date.distantPast
    private var sawRelocalizing = false
    private var completeNotified = false
    private var targetAnchor: AnchorEntity?

    /// 스캔할 사물의 중심을 정한다. 이후 깊이 누적은 이 주변(focusRadius)만, 둘레 지도는 이 중심 기준.
    func setTarget(_ c: SIMD3<Float>, focusRadius: Float = 1.3) {
        let first = target == nil
        target = c
        if first, let t = cameraPosition { coverage.reset(); coverage.setOrigin(camPos: t, center: c); completeNotified = false }
        if hasLiDAR { fusion.setFocus(center: c, radius: focusRadius) }
        drawTargetMarker()
        DebugLog.log("ar", "스캔 대상 지정 (\(c.x), \(c.y), \(c.z))", extra: ["focus": "\(focusRadius)", "first": "\(first)"])
    }

    /// 중심을 옮긴다(자동 보정·미세조정). 둘레 지도 누적은 유지한다.
    func moveTarget(_ c: SIMD3<Float>, focusRadius: Float = 1.3) {
        guard target != nil else { return }
        target = c
        if hasLiDAR { fusion.setFocus(center: c, radius: focusRadius) }
        drawTargetMarker()
    }

    func clearTarget() {
        target = nil
        coverage.reset()
        completeNotified = false
        if hasLiDAR { fusion.setFocus(center: nil, radius: 0) }
        if let a = targetAnchor { arView.scene.removeAnchor(a); targetAnchor = nil }
    }

    /// 화면 기준 축(오른쪽·위·앞) — 중심 미세조정이 보이는 방향대로 움직이게 한다
    func cameraAxes() -> (right: SIMD3<Float>, up: SIMD3<Float>, fwd: SIMD3<Float>)? {
        guard let t = arView.session.currentFrame?.camera.transform else { return nil }
        let c = t.columns
        return (simd_normalize(SIMD3(c.0.x, c.0.y, c.0.z)), simd_normalize(SIMD3(c.1.x, c.1.y, c.1.z)),
                simd_normalize(-SIMD3(c.2.x, c.2.y, c.2.z)))
    }

    /// 지금 가장 도움 되는 카메라 워크 한 줄 (대상이 있어야 한다)
    func assistNow() -> ScanCoverage.Assist? {
        guard let c = target, let t = arView.session.currentFrame?.camera.transform else { return nil }
        let col = t.columns
        let pos = SIMD3<Float>(col.3.x, col.3.y, col.3.z)
        let fwd = simd_normalize(-SIMD3<Float>(col.2.x, col.2.y, col.2.z))
        return coverage.assist(camPos: pos, camFwd: fwd, center: c, tracking: trackingNormal, tooFast: motionTooFast)
    }

    /// 상단 썸네일용: 대상 주변 누적 점(대상 중심 기준 상대 좌표)과 카메라 위치
    struct ThumbData {
        var points: [SIMD3<Float>]
        var cam: SIMD3<Float>
        var camSector: Int
        var coverage: ScanCoverage
    }

    func thumbnailData(radius: Float = 0.9) -> ThumbData? {
        guard let c = target, let cam = cameraPosition else { return nil }
        let pts = hasLiDAR ? fusion.points(center: c, radius: radius, maxPoints: 1400)
                           : Array(featurePoints(center: c, radius: radius).prefix(1400))
        let look = arView.session.currentFrame.map { simd_normalize(-SIMD3<Float>($0.camera.transform.columns.2.x, $0.camera.transform.columns.2.y, $0.camera.transform.columns.2.z)) } ?? SIMD3(0, 0, -1)
        let sec = coverage.pose(camPos: cam, camFwd: look, center: c).sector
        return ThumbData(points: pts.map { $0 - c }, cam: cam - c, camSector: sec, coverage: coverage)
    }

    /// 대상 중심 마커: 3축 십자 + 수평 링 + 수직 기둥 — 어느 방향에서 봐도 중심 위치가 보인다
    private func drawTargetMarker() {
        if let a = targetAnchor { arView.scene.removeAnchor(a); targetAnchor = nil }
        guard let c = target else { return }
        var segs: [(SIMD3<Float>, SIMD3<Float>)] = []
        let h: Float = 0.05
        segs.append((c - [h, 0, 0], c + [h, 0, 0]))
        segs.append((c - [0, h, 0], c + [0, h, 0]))
        segs.append((c - [0, 0, h], c + [0, 0, h]))
        let R: Float = 0.18, n = 28
        for i in 0..<n {
            let a0 = Float(i) / Float(n) * 2 * .pi, a1 = Float(i + 1) / Float(n) * 2 * .pi
            segs.append((c + [R * cos(a0), 0, R * sin(a0)], c + [R * cos(a1), 0, R * sin(a1)]))
        }
        segs.append((c + [0, h, 0], c + [0, 0.28, 0]))
        guard let m = lineMesh(segs, thickness: 0.0014) else { return }
        let anchor = AnchorEntity(world: .zero)
        anchor.addChild(ModelEntity(mesh: m, materials: [UnlitMaterial(color: .systemTeal)]))
        arView.scene.addAnchor(anchor)
        targetAnchor = anchor
    }

    /// VIO 특징점 누적 (identifier → 월드 좌표). 같은 id는 최신 위치로 갱신
    /// 점 품질: 여러 프레임에서 관측된 점만 쓰고(minObs), 위치는 관측 평균 — 한 프레임짜리 노이즈 점을 거른다
    private var featurePoints: [UInt64: SIMD3<Float>] = [:]  // id → 평균 위치
    private var featureObs: [UInt64: Int] = [:]  // id → 관측 프레임 수
    /// 안정 점 기준 관측 프레임 수. 등록은 3(품질 우선), 검수는 2 — 폰을 고정한 채 비추면 점이 늘지 않아 더 빨리 쌓이게 한다
    var minObs = 3
    private var stableCount = 0
    private let featurePointCap = 80_000
    /// 카메라가 너무 빠르게 움직이는 동안은 점을 기록하지 않는다 (모션 블러·VIO 불안정으로 점이 번진다)
    @Published var motionTooFast = false
    private var lastPoseT: TimeInterval = 0
    private var lastPos: SIMD3<Float>?
    private var lastFwd: SIMD3<Float>?

    // MARK: - 세션 생명주기

    private var hasStarted = false
    private var triedMinimalConfig = false

    /// reset=false(기본): 탭을 오가도 추적과 누적 특징점을 유지한 채 재개. reset=true: 처음부터 다시.
    func start(restoringWorldMap map: ARWorldMap?, reset: Bool = false) {
        let config = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            config.sceneReconstruction = .mesh
        }
        if reset || !hasStarted {
            featurePoints.removeAll(); featureObs.removeAll(); stableCount = 0
            camTrail.removeAll(); lastPos = nil; lastFwd = nil
            keyframes.removeAll(); keyframeCount = 0; lastKFPos = nil; lastKFFwd = nil
            fusion.reset()
            clearTarget()
        }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            config.frameSemantics.insert(.sceneDepth)
        }
        config.environmentTexturing = .automatic
        if useHighResFormat, #available(iOS 16.0, *),
           let f = ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing {
            config.videoFormat = f
        }
        if let map {  // 재방문: 공간 프레임 복원 (COORDINATE.md 4절)
            config.initialWorldMap = map
            statusText = "월드맵 복원 시도 중…"
        }
        if let o = Self.active, o !== self { o.arView.session.pause() }  // 다른 화면의 세션은 먼저 멈춘다
        Self.active = self
        arView.session.delegate = self
        let resetting = reset || !hasStarted
        if map != nil {
            awaitingRelocalization = true; relocalized = false; sawNonNormalSinceStart = false
        } else if resetting {
            awaitingRelocalization = false; relocalized = false
        }
        arView.session.run(config, options: resetting ? [.resetTracking, .removeExistingAnchors] : [])
        hasStarted = true
        watchFrames(after: Date())
        DebugLog.log("ar", "세션 \(resetting ? "시작" : "재개") lidarMesh=\(config.sceneReconstruction != [])",
                     extra: ["worldMapRestore": "\(map != nil)", "featurePoints": "\(featurePoints.count)"])
    }

    /// 카메라는 앱에 하나뿐이다 — 스캔·검수 두 화면의 세션이 겹쳐 돌면 안 되므로 지금 켜진 컨트롤러를 기억한다.
    private static weak var active: ScanSessionController?
    private var lastFrameAt = Date.distantPast

    /// 화면 전환 때 새 화면의 onAppear가 이전 화면의 onDisappear보다 먼저 올 수 있다.
    /// 그때 늦게 온 pause()가 새 화면의 세션을 멈춰 카메라가 마지막 장면(가로 원본)으로 굳는 것을 막는다.
    func pause() {
        if let a = Self.active, a !== self { return }  // 이미 다른 화면이 카메라를 넘겨받았다
        Self.active = nil
        arView.session.pause()
    }

    /// 카메라 프레임이 끊겼으면(다른 세션이 가져갔거나 멈춤) 추적을 유지한 채 세션을 다시 돌린다.
    private func watchFrames(after started: Date) {
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard let self, Self.active === self, self.lastFrameAt < started else { return }
            DebugLog.log("ar", "카메라 프레임 없음 — 세션 재개")
            let cfg = ARWorldTrackingConfiguration()
            if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { cfg.sceneReconstruction = .mesh }
            if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { cfg.frameSemantics.insert(.sceneDepth) }
            self.arView.session.run(cfg, options: [])
        }
    }

    /// 다음 사물 스캔 준비: 이전 사물의 누적 특징점·키프레임·궤적·미리보기만 비운다.
    /// 추적(세션)은 유지한다 — 리셋하면 공간 좌표계가 바뀌어 이미 저장한 자산의 설계 위치·월드맵과 어긋난다.
    func resetScanData() {
        featurePoints.removeAll(); featureObs.removeAll(); stableCount = 0
        featurePointCount = 0
        camTrail.removeAll(); lastPos = nil; lastFwd = nil
        keyframes.removeAll(); keyframeCount = 0; lastKFPos = nil; lastKFFwd = nil
        fusion.reset()
        clearTarget()
        clearPreview()
        DebugLog.log("ar", "새 사물 스캔 준비: 누적 데이터 초기화(추적 유지)")
    }

    func currentWorldMap() async throws -> ARWorldMap {
        // SDK가 async 변형을 자동 생성하지 않아 completion API를 래핑
        try await withCheckedThrowingContinuation { cont in
            arView.session.getCurrentWorldMap { map, error in
                if let map {
                    cont.resume(returning: map)
                } else {
                    cont.resume(throwing: error ?? NSError(
                        domain: "Markerless", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: "월드맵 획득 실패"]))
                }
            }
        }
    }

    // MARK: - 화면 중앙 레이캐스트 (사물 중심 지정 — 자가 에셋 §5)

    func centerHitPosition() -> SIMD3<Float>? {
        // ARKit 레이캐스트는 평면만 대상 — 재구성 메시와 직접 교차해 사물 표면을 얻는다
        guard let frame = arView.session.currentFrame else { return nil }
        let camT = frame.camera.transform
        let origin = SIMD3<Float>(camT.columns.3.x, camT.columns.3.y, camT.columns.3.z)
        let dir = simd_normalize(-SIMD3<Float>(camT.columns.2.x, camT.columns.2.y, camT.columns.2.z))
        if hasLiDAR, let d = frame.sceneDepth,
           let hit = DepthFusion.centerSurface(depth: d.depthMap, confidence: d.confidenceMap, intrinsics: frame.camera.intrinsics,
                                               imageSize: frame.camera.imageResolution, transform: camT) {
            return hit  // LiDAR 깊이맵 중앙 = 조준한 사물 표면 (메시 전체 교차보다 싸고 정확)
        }
        let tris = collectTriangles()
        if tris.triangleCount == 0 { return centerHitFromFeaturePoints(origin: origin, dir: dir) }
        var bestDist = Float.greatestFiniteMagnitude
        var hit: SIMD3<Float>?
        for t in 0..<tris.triangleCount {
            let a = tris.positions[Int(tris.indices[t*3])]
            let b = tris.positions[Int(tris.indices[t*3+1])]
            let c = tris.positions[Int(tris.indices[t*3+2])]
            if let d = rayTriangleDist(origin, dir, a, b, c), d < bestDist {
                bestDist = d
                hit = origin + dir * d
            }
        }
        return hit
    }

    // MARK: - LiDAR 없는 기기: 특징점 기반 (카메라+IMU VIO)

    /// 조준선 주변(원뿔) 특징점의 중앙 깊이를 표면으로 본다. 점이 부족하면 평면 추정 레이캐스트.
    private func centerHitFromFeaturePoints(origin: SIMD3<Float>, dir: SIMD3<Float>) -> SIMD3<Float>? {
        var depths: [Float] = []
        for (id, p) in featurePoints where (featureObs[id] ?? 0) >= minObs {
            let v = p - origin
            let t = simd_dot(v, dir)
            guard t > 0.15, t < 8 else { continue }
            let perp = simd_length(v - dir * t)
            if perp < 0.04 + 0.05 * t { depths.append(t) }
        }
        if depths.count >= 3 {
            // 카메라에서 가장 가까운 군집(점 ≥3, 간격 6cm 이내로 이어진 것)의 중앙 깊이 = 사물 앞면.
            // 전체 중앙값은 사물 뒤의 배경·탁자 점에 끌려가 POI가 사물 밖으로 나간다.
            depths.sort()
            var cluster: [Float] = [depths[0]]
            for d in depths.dropFirst() {
                if d - cluster.last! <= 0.06 { cluster.append(d) }
                else if cluster.count >= 3 { break }
                else { cluster = [d] }
            }
            if cluster.count >= 3 { return origin + dir * cluster[cluster.count / 2] }
            return origin + dir * depths[depths.count / 2]
        }
        let pt = CGPoint(x: arView.bounds.midX, y: arView.bounds.midY)
        if let q = arView.raycast(from: pt, allowing: .estimatedPlane, alignment: .any).first {
            let c = q.worldTransform.columns.3
            return SIMD3<Float>(c.x, c.y, c.z)
        }
        return nil
    }

    /// center 반경 안의 안정 특징점 수 (검수 화면의 "조준 사물 주변 점" 실시간 표시용)
    func nearbyStableCount(center: SIMD3<Float>, radius: Float) -> Int {
        if hasLiDAR { return fusion.count(center: center, radius: radius) }
        var n = 0
        for (id, p) in featurePoints where (featureObs[id] ?? 0) >= minObs && simd_distance(p, center) <= radius { n += 1 }
        return n
    }

    /// center 반경 안에 누적된 특징점 (월드 좌표)
    func featurePoints(center: SIMD3<Float>, radius: Float) -> [SIMD3<Float>] {
        featurePoints.compactMap { id, p in
            (featureObs[id] ?? 0) >= minObs && simd_distance(p, center) <= radius ? p : nil
        }
    }

    /// 현재 카메라 위치 (surfel 법선 정렬용)
    var cameraPosition: SIMD3<Float>? {
        guard let t = arView.session.currentFrame?.camera.transform else { return nil }
        return SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
    }

    // MARK: - 사물 분리 입력 (POI 주변 점) — LiDAR는 메시 정점+삼각형, VIO는 특징점

    struct ScanContext {
        var points: [SIMD3<Float>]
        /// LiDAR 메시: points를 가리키는 삼각형 인덱스(3개씩). VIO는 빈 배열
        var triangles: [UInt32]
        var isMesh: Bool { !triangles.isEmpty }
    }

    /// center 반경 안의 점. 정점이 maxPoints를 넘으면 반경을 줄여 맞춘다(서버 상한 120k).
    func contextSample(center: SIMD3<Float>, radius: Float, maxPoints: Int = 100_000) -> ScanContext {
        if hasLiDAR {
            // 깊이 융합 점이 충분하면 그것을 쓴다 (메시보다 촘촘·정확). 부족하면 아래 메시 경로로 대체.
            let pts = fusion.points(center: center, radius: radius, maxPoints: maxPoints)
            if pts.count >= 800 { return ScanContext(points: pts, triangles: []) }
            DebugLog.log("scan", "깊이 점 부족(\(pts.count)) → 메시로 대체", extra: ["fused": "\(fusion.stableCount)"])
        }
        let tris = collectTriangles()
        guard tris.triangleCount > 0 else {
            return ScanContext(points: featurePoints(center: center, radius: radius), triangles: [])
        }
        var r = radius
        var dists = tris.positions.map { simd_distance($0, center) }
        if dists.filter({ $0 <= r }).count > maxPoints {
            r = dists.sorted()[maxPoints - 1]  // 가장 가까운 maxPoints개까지만
        }
        var remap = [Int32](repeating: -1, count: tris.positions.count)
        var pts: [SIMD3<Float>] = []
        for (i, d) in dists.enumerated() where d <= r {
            remap[i] = Int32(pts.count)
            pts.append(tris.positions[i])
        }
        dists.removeAll()
        var idx: [UInt32] = []
        for t in 0..<tris.triangleCount {
            let a = remap[Int(tris.indices[t * 3])], b = remap[Int(tris.indices[t * 3 + 1])], c = remap[Int(tris.indices[t * 3 + 2])]
            if a >= 0, b >= 0, c >= 0 { idx.append(contentsOf: [UInt32(a), UInt32(b), UInt32(c)]) }
        }
        return ScanContext(points: pts, triangles: idx)
    }

    // MARK: - 분리 결과 미리보기 (외곽선 와이어프레임 — 점 조각이 아니라 선으로)

    private var previewAnchor: AnchorEntity?

    func clearPreview() {
        if let a = previewAnchor { arView.scene.removeAnchor(a); previewAnchor = nil }
    }

    /// 분리된 사물의 윤곽선(초록) + 외곽 박스를 세션 좌표 그대로 그린다
    func showOutlinePreview(_ outline: OutlineJSON) {
        clearPreview()
        let anchor = AnchorEntity(world: .zero)
        anchor.addChild(makeOutlineEntity(outline, color: .systemGreen, contour: true, box: false))  // 외곽선 = 초록
        anchor.addChild(makeOutlineEntity(outline, color: boxLineColor, contour: false, box: true))  // 외곽 박스 = 보라
        let dim = makeDimensionEntity(outline.obb, color: boxLineColor)  // 제도식 치수선·mm 표기(박스와 같은 색)
        anchor.addChild(dim.root)
        billboards += dim.labels
        arView.scene.addAnchor(anchor)
        previewAnchor = anchor
    }

    // MARK: - 키프레임 (등록 때 방향별 사진 실루엣의 원본)

    private(set) var keyframes: [Keyframe] = []
    @Published var keyframeCount = 0
    private var lastKFPos: SIMD3<Float>?
    private var lastKFFwd: SIMD3<Float>?
    private let keyframeCap = 140  // 960px JPEG ≈ 100KB → 최대 약 14MB

    /// 카메라가 충분히 움직였을 때만(12cm 또는 8°) 저장한다 — 가만히 있으면 쌓이지 않고, 한 바퀴 돌면 사방이 담긴다
    private func captureKeyframeIfNeeded(_ frame: ARFrame, pos: SIMD3<Float>, fwd: SIMD3<Float>) {
        if let lp = lastKFPos, let lf = lastKFFwd {
            let moved = simd_distance(pos, lp) > 0.12
            let turned = acos(max(-1, min(1, simd_dot(fwd, lf)))) > 0.14
            guard moved || turned else { return }
        }
        lastKFPos = pos; lastKFFwd = fwd
        let box = PixelBufferBox(buffer: frame.capturedImage)
        let tf = frame.camera.transform
        let K = frame.camera.intrinsics
        Task.detached(priority: .utility) {
            guard let (data, w, h, s) = Keyframe.encode(box.buffer) else { return }
            let kf = Keyframe(jpeg: data, transform: tf, fx: K[0][0] * s, fy: K[1][1] * s,
                              cx: K[2][0] * s, cy: K[2][1] * s, width: w, height: h)
            await MainActor.run {
                self.keyframes.append(kf)
                if self.keyframes.count > self.keyframeCap { self.keyframes.removeFirst(self.keyframes.count - self.keyframeCap) }
                self.keyframeCount = self.keyframes.count
            }
        }
    }

    // MARK: - 4방향 스캔 커버리지 (카메라 궤적)

    private struct CamSample { var pos: SIMD3<Float>; var fwd: SIMD3<Float> }
    private var camTrail: [CamSample] = []
    private var lastTrailTime = Date.distantPast

    /// center 둘레를 n방향(15°×24)으로 나눠, 카메라가 사물을 향해 그 방향에서 비춘 구간을 표시한다 (한 바퀴 돌았는지 안내).
    /// 방향 0은 세계 −Z 쪽에서 보는 위치부터 시계 방향.
    func sectorCoverage(center: SIMD3<Float>, count n: Int = 24) -> [Bool] {
        var out = [Bool](repeating: false, count: n)
        for s in camTrail {
            var rel = s.pos - center
            rel.y = 0
            let d = simd_length(rel)
            guard d > 0.2, d < 2.8 else { continue }
            let toObj = simd_normalize(center - s.pos)
            guard simd_dot(s.fwd, toObj) > 0.75 else { continue }  // 사물을 향해 비출 때만 센다
            var ang = atan2(rel.x, -rel.z)  // 0 = −Z 쪽, 시계 방향 +
            if ang < 0 { ang += 2 * .pi }
            out[min(n - 1, Int(ang / (2 * .pi) * Float(n)))] = true
        }
        return out
    }

    private func recordCamera(_ transform: simd_float4x4) {
        let now = Date()
        guard now.timeIntervalSince(lastTrailTime) > 0.25 else { return }  // 4Hz
        lastTrailTime = now
        let c = transform.columns
        camTrail.append(CamSample(pos: SIMD3(c.3.x, c.3.y, c.3.z),
                                  fwd: simd_normalize(-SIMD3(c.2.x, c.2.y, c.2.z))))
        if camTrail.count > 900 { camTrail.removeFirst(camTrail.count - 900) }
    }

    /// 사물 외곽 박스(OBB) 기준 4방향(앞·오른쪽·뒤·왼쪽)에서 사물을 바라본 시간(샘플 수).
    /// 서버 외형 뷰와 같은 정의: front=관찰자가 −v 쪽, right=+u, back=+v, left=−u (u,v는 OBB 수평 축).
    func viewCoverage(obbCenter: SIMD3<Float>, yawDeg: Float) -> [String: Int] {
        let th = yawDeg * .pi / 180
        let u = SIMD3<Float>(cos(th), 0, sin(th))       // 수평면 축 (x,z)
        let v = SIMD3<Float>(-sin(th), 0, cos(th))
        let viewers: [String: SIMD3<Float>] = ["front": -v, "right": u, "back": v, "left": -u]
        var counts = ["front": 0, "right": 0, "back": 0, "left": 0]
        for s in camTrail {
            var rel = s.pos - obbCenter
            rel.y = 0
            let dist = simd_length(rel)
            guard dist > 0.25, dist < 3.5 else { continue }
            let toCenter = simd_normalize(obbCenter - s.pos)
            guard simd_dot(s.fwd, toCenter) > 0.6 else { continue }  // 사물을 향해 보고 있을 때만
            let dir = rel / dist
            for (name, vd) in viewers where simd_dot(dir, vd) > 0.7071 { counts[name]! += 1 }  // ±45° 섹터
        }
        return counts
    }

    // MARK: - 메시 → 삼각형 수집 (월드 좌표)

    struct Triangles {
        var positions: [SIMD3<Float>] = []
        var indices: [UInt32] = []

        var triangleCount: Int { indices.count / 3 }

        mutating func add(vertices: [SIMD3<Float>], tris: [Int32], transform: simd_float4x4) {
            let base = UInt32(positions.count)
            positions.reserveCapacity(positions.count + vertices.count)
            for v in vertices {
                let w = transform * SIMD4<Float>(v.x, v.y, v.z, 1)
                positions.append(SIMD3<Float>(w.x, w.y, w.z))
            }
            indices.reserveCapacity(indices.count + tris.count)
            for t in tris { indices.append(base + UInt32(t)) }
        }
    }

    func collectTriangles() -> Triangles {
        var out = Triangles()
        guard let frame = arView.session.currentFrame else { return out }
        for anchor in frame.anchors {
            guard let mesh = anchor as? ARMeshAnchor else { continue }
            // ARGeometrySource/Element에 서브스크립트가 없어 MTLBuffer에서 직접 읽는다
            out.add(vertices: mesh.geometry.vertices.simd3Array(),
                    tris: mesh.geometry.faces.int32Array(),
                    transform: mesh.transform)
        }
        return out
    }

    // MARK: - 측정 클라우드 추출 (공간 프레임, 지시서 13절 CloudIn)

    /// center 주변 radius(m) 구 안의 표면을 면적 비례 샘플링해 maxPoints로 축소.
    /// 노이즈는 ARKit 메시 자체 것 — 서버 ICP/트리밍이 처리한다.
    func sampleCloud(center: SIMD3<Float>, radius: Float, maxPoints: Int = 3000) -> [SIMD3<Float>] {
        let tris = collectTriangles()
        if tris.triangleCount == 0 {
            // LiDAR 없음: 누적 특징점을 그대로 표면 샘플로 사용 (희소 → 서버 ICP가 처리)
            let pts = featurePoints(center: center, radius: radius)
            guard pts.count > maxPoints else { return pts }
            let step = Float(pts.count) / Float(maxPoints)
            return (0..<maxPoints).map { pts[Int(Float($0) * step)] }
        }

        // 구 안에 무게중심이 든 삼각형만
        struct Cand { var a: SIMD3<Float>; var b: SIMD3<Float>; var c: SIMD3<Float>; var area: Float }
        var cands: [Cand] = []
        for t in 0..<tris.triangleCount {
            let a = tris.positions[Int(tris.indices[t*3])]
            let b = tris.positions[Int(tris.indices[t*3+1])]
            let c = tris.positions[Int(tris.indices[t*3+2])]
            let mid = (a + b + c) / 3
            if distance(mid, center) <= radius {
                let area = 0.5 * simd_length(simd_cross(b - a, c - a))
                if area > 1e-8 { cands.append(Cand(a: a, b: b, c: c, area: area)) }
            }
        }
        guard !cands.isEmpty else { return [] }

        let totalArea = cands.reduce(0) { $0 + $1.area }
        var pts: [SIMD3<Float>] = []
        // 면적 비례 기대 개수로 샘플 후 상한 컷
        let budget = min(maxPoints * 4, max(1000, Int(totalArea * 5000)))
        var rng = SystemRandomNumberGenerator()
        for cand in cands {
            let n = max(1, Int(Float(cand.area / totalArea) * Float(budget)))
            for _ in 0..<n {
                var r1 = sqrt(Float.random(in: 0...1, using: &rng))
                let r2 = Float.random(in: 0...1, using: &rng)
                let p = (1 - r1) * cand.a + (r1 * (1 - r2)) * cand.b + (r1 * r2) * cand.c
                pts.append(p)
            }
        }
        if pts.count > maxPoints {
            // 균등 간격 서브샘플
            var picked: [SIMD3<Float>] = []
            picked.reserveCapacity(maxPoints)
            let step = Float(pts.count) / Float(maxPoints)
            for i in 0..<maxPoints { picked.append(pts[Int(Float(i) * step)]) }
            return picked
        }
        return pts
    }

    // MARK: - 오버레이 (InspectionView용)

    private var overlayAnchor: AnchorEntity?

    func clearOverlay() {
        if let anchor = overlayAnchor {
            arView.scene.removeAnchor(anchor)
            overlayAnchor = nil
        }
    }

    /// 설계 모델(파랑 반투명) 배치 — designPose.position에 두면 정합 (§2/§5)
    func showDesign(_ mesh: GLBMesh, outline: OutlineJSON? = nil, cards: [PhotoCardAsset] = [],
                    at pose: PoseJSON, color: UIColor, view: InspectViewJSON = .standard) throws {
        clearOverlay()
        let entity = try makeAssetEntity(mesh: mesh, outline: outline, view: view, color: color, surfaceOpacity: 0.45)
        // 앵커를 월드 원점에 두고 엔티티에 포즈를 준다 (앵커와 엔티티 양쪽에 위치를 주면 2배 이동한다)
        entity.position = SIMD3<Float>(Float(pose.positionM.x), Float(pose.positionM.y), Float(pose.positionM.z))
        entity.orientation = orientation(of: pose)
        for c in cards where view.silhouette {  // 설계 위치에서도 사진 실루엣 — 보는 방향에 가장 가까운 한 장
            let node = makeCardEntity(c)
            entity.addChild(node)
            overlayCards.append(OverlayCard(node: node, root: entity, forwardLocal: c.forward, side: c.side))
        }
        let anchor = AnchorEntity(world: .zero)
        anchor.addChild(entity)
        arView.scene.addAnchor(anchor)
        overlayAnchor = anchor
    }

    /// 측정 결과 실물 포즈 표시 (초록=PASS, 빨강=FAIL)
    func showActual(_ mesh: GLBMesh, outline: OutlineJSON? = nil, cards: [PhotoCardAsset] = [],
                    at pose: PoseJSON, color: UIColor, view: InspectViewJSON = .standard) throws {
        let entity = try makeAssetEntity(mesh: mesh, outline: outline, view: view, color: color, surfaceOpacity: 0.8)
        entity.position = SIMD3<Float>(Float(pose.positionM.x), Float(pose.positionM.y), Float(pose.positionM.z))
        entity.orientation = orientation(of: pose)
        for c in cards where view.silhouette {  // 사진 실루엣 카드: 보는 방향에 가장 가까운 한 장만 보인다
            let node = makeCardEntity(c)
            entity.addChild(node)
            overlayCards.append(OverlayCard(node: node, root: entity, forwardLocal: c.forward, side: c.side))
        }
        let anchor = AnchorEntity(world: .zero)
        anchor.addChild(entity)
        arView.scene.addAnchor(anchor)
        // 이전 actual 앵커는 남겨 두어 비교 — 새 측정 시 clearOverlay가 design만 지우지 않으므로 별도 관리
        actualAnchor = anchor
    }

    private var actualAnchor: AnchorEntity?

    /// 관리자가 고른 레이어(표면·점군·외곽선·외곽 박스·치수)만 담은 자산 엔티티. 사진 실루엣은 호출한 쪽이 붙인다.
    /// 선택한 것이 하나도 그릴 수 없으면(외곽선 데이터 없음 등) 표면으로 대체해 빈 화면을 막는다.
    private func makeAssetEntity(mesh: GLBMesh, outline: OutlineJSON?, view: InspectViewJSON, color: UIColor,
                                 surfaceOpacity: Float) throws -> Entity {
        let root = Entity()
        var drew = false
        if view.surface { root.addChild(try makeEntity(from: mesh, color: color, opacity: surfaceOpacity)); drew = true }
        if view.points { root.addChild(makePointsEntity(mesh, color: color)); drew = true }
        if let outline {
            if view.outline || view.box {
                // 외곽선(높이별 윤곽)은 판정색, 외곽 박스·치수는 보라색 — 서로 구분되게
                if view.outline { root.addChild(makeOutlineEntity(outline, color: color, contour: true, box: false)); drew = true }
                if view.box { root.addChild(makeOutlineEntity(outline, color: boxLineColor, contour: false, box: true)); drew = true }
            }
            if view.dimensions {  // 치수선·mm 표기 (글자는 매 프레임 카메라를 향한다)
                let dim = makeDimensionEntity(outline.obb, color: boxLineColor)  // 치수는 박스와 같은 보라색
                root.addChild(dim.root)
                billboards += dim.labels
                drew = true
            }
        }
        if !drew && !view.silhouette { root.addChild(try makeEntity(from: mesh, color: color, opacity: surfaceOpacity)) }
        return root
    }

    /// 매 프레임 카메라를 향해야 하는 글자(치수 표기)와 방향별 사진 카드
    private var billboards: [Entity] = []
    private var overlayCards: [OverlayCard] = []
    private var lastShownCard = ""

    /// 글자는 카메라를 바라보게, 사진 카드는 현재 시점과 가장 가까운 방향 한 장만 켠다
    private func updateOverlayNodes(camPos: SIMD3<Float>, camFwd: SIMD3<Float>) {
        billboards.removeAll { $0.scene == nil }
        for node in billboards {
            let p = node.position(relativeTo: nil)
            node.look(at: p + (p - camPos), from: p, relativeTo: nil)  // 글자 앞면(+Z)이 카메라를 향하도록 반대편을 바라본다
        }
        overlayCards.removeAll { $0.node?.scene == nil }
        guard !overlayCards.isEmpty else { return }
        var best: [ObjectIdentifier: (idx: Int, score: Float)] = [:]
        for (i, c) in overlayCards.enumerated() {
            guard let root = c.root else { continue }
            let fwdWorld = root.orientation(relativeTo: nil).act(c.forwardLocal)
            let score = simd_dot(camFwd, fwdWorld)
            let key = ObjectIdentifier(root)
            if best[key] == nil || score > best[key]!.score { best[key] = (i, score) }
        }
        var shown = ""
        for (i, c) in overlayCards.enumerated() {
            guard let root = c.root, let b = best[ObjectIdentifier(root)] else { continue }
            // 둘레 24컷(15° 간격)이면 가장 가까운 한 장이 항상 ±8° 안이다 — 36° 밖이면 숨긴다. 구버전 4컷은 56°.
            let on = (i == b.idx && b.score > (overlayCards.count >= 12 ? 0.9 : 0.55))
            c.node?.isEnabled = on
            if on { shown = "\(c.side)(\(String(format: "%.2f", b.score)))" }
        }
        if shown != lastShownCard {  // 표시되는 카드가 바뀔 때만 기록 (실기기에서 카드가 보이는지 진단용)
            lastShownCard = shown
            DebugLog.log("ar", shown.isEmpty ? "사진 카드 숨김 (보는 방향이 촬영 방향과 멀다)" : "사진 카드 표시 \(shown)")
        }
    }

    /// quat이 있으면 그대로(사물 앵커 정합 결과), 없으면 rpy 규약 변환
    private func orientation(of pose: PoseJSON) -> simd_quatf {
        if let q = pose.quat {
            return simd_quatf(ix: Float(q.x), iy: Float(q.y), iz: Float(q.z), r: Float(q.w))
        }
        return simd_quatf(rpyDeg: pose.rpyDeg ?? .zero)
    }

    /// 지금 사물 주변에서 보이는 점 (월드 좌표) — 추적 보정용. LiDAR는 융합 점, 아니면 특징점.
    func livePoints(center: SIMD3<Float>, radius: Float, maxPoints: Int = 2500) -> [SIMD3<Float>] {
        hasLiDAR ? fusion.points(center: center, radius: radius, maxPoints: maxPoints)
                 : Array(featurePoints(center: center, radius: radius).prefix(maxPoints))
    }

    /// 떠 있는 '맞춘 결과' 오버레이를 새 포즈로 옮긴다 (다시 만들지 않고 변환만 바꾼다)
    func updateActualPose(position: SIMD3<Float>, orientation: simd_quatf) {
        guard let e = actualAnchor?.children.first else { return }
        e.position = position
        e.orientation = orientation
    }

    func clearActual() {
        if let a = actualAnchor {
            arView.scene.removeAnchor(a)
            actualAnchor = nil
        }
    }

    // MARK: - 관측된 사물 윤곽 (노랑) — 맞춘 모델 선화와 겹쳐 크기·각도 일치를 눈으로 확인

    private var observedAnchor: AnchorEntity?

    func clearObserved() {
        if let a = observedAnchor { arView.scene.removeAnchor(a); observedAnchor = nil }
    }

    func showObservedOutline(_ o: OutlineJSON) {
        clearObserved()
        let anchor = AnchorEntity(world: .zero)
        anchor.addChild(makeOutlineEntity(o, color: .systemYellow, contourThickness: 0.0004))
        arView.scene.addAnchor(anchor)
        observedAnchor = anchor
    }

    func clearAllOverlays() {
        clearOverlay()
        clearActual()
        clearObserved()
    }

    private func makeEntity(from mesh: GLBMesh, color: UIColor, opacity: Float) throws -> ModelEntity {
        // 플랫 법선: 삼각형별 면법선을 정점 3개에 복제
        var normals = [SIMD3<Float>](repeating: .zero, count: mesh.positions.count)
        for t in stride(from: 0, to: mesh.indices.count, by: 3) {
            let ia = Int(mesh.indices[t]), ib = Int(mesh.indices[t+1]), ic = Int(mesh.indices[t+2])
            let a = mesh.positions[ia], b = mesh.positions[ib], c = mesh.positions[ic]
            let n = simd_normalize(simd_cross(b - a, c - a))
            normals[ia] = n; normals[ib] = n; normals[ic] = n
        }
        var descriptor = MeshDescriptor(name: "overlay")
        descriptor.positions = MeshBuffer(mesh.positions)
        descriptor.normals = MeshBuffer(normals)
        descriptor.primitives = .triangles(mesh.indices)
        let meshRes = try MeshResource.generate(from: [descriptor])
        var material = SimpleMaterial(color: color.withAlphaComponent(CGFloat(opacity)), isMetallic: false)
        if #available(iOS 18.0, *) {
            material.faceCulling = .none  // 안쪽 면도 보이게 (반투명, iOS 18+ 전용 API)
        }
        return ModelEntity(mesh: meshRes, materials: [material])
    }
}

// MARK: - ARSessionDelegate

extension ScanSessionController: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let newMeshIDs = frame.anchors.compactMap { ($0 as? ARMeshAnchor)?.identifier }
        let rawPoints = frame.rawFeaturePoints?.points ?? []
        let rawIDs = frame.rawFeaturePoints?.identifiers ?? []
        let depthBox = frame.sceneDepth.map { PixelBufferBox(buffer: $0.depthMap) }
        let confBox = frame.sceneDepth?.confidenceMap.map { PixelBufferBox(buffer: $0) }
        let camK = frame.camera.intrinsics, camSize = frame.camera.imageResolution, camT = frame.camera.transform
        Task { @MainActor in
            self.lastFrameAt = Date()
            // 카메라 속도: 선속도 0.5m/s, 각속도 1.0rad/s(≈57°/s)를 넘으면 이 프레임의 점은 버린다
            let tr = frame.camera.transform.columns
            let pos = SIMD3<Float>(tr.3.x, tr.3.y, tr.3.z)
            let fwd = simd_normalize(-SIMD3<Float>(tr.2.x, tr.2.y, tr.2.z))
            var moving = false
            if let lp = self.lastPos, let lf = self.lastFwd, frame.timestamp > self.lastPoseT {
                let dt = Float(frame.timestamp - self.lastPoseT)
                let lin = simd_distance(pos, lp) / dt
                let ang = acos(max(-1, min(1, simd_dot(fwd, lf)))) / dt
                moving = lin > 0.5 || ang > 1.0
            }
            self.lastPos = pos; self.lastFwd = fwd; self.lastPoseT = frame.timestamp
            self.motionTooFast = moving
            self.updateOverlayNodes(camPos: pos, camFwd: fwd)
            // 사진 카드는 이제 3D 점의 실루엣으로 만들어 키프레임(JPEG)이 필요 없다 — CPU·메모리 절약

            if frame.camera.trackingState == .normal && !moving {
                for (id, p) in zip(rawIDs, rawPoints) {
                    if let old = self.featurePoints[id] {
                        let n = self.featureObs[id] ?? 1
                        let nn = n + 1
                        // 처음 10회는 누적 평균, 이후는 지수 평균(ARKit이 점을 보정해도 따라간다)
                        self.featurePoints[id] = nn <= 10 ? (old * Float(n) + p) / Float(nn) : old * 0.9 + p * 0.1
                        self.featureObs[id] = nn
                        if n == self.minObs - 1 { self.stableCount += 1 }
                    } else {
                        self.featurePoints[id] = p
                        self.featureObs[id] = 1
                    }
                }
                if self.featurePoints.count > self.featurePointCap {
                    // 상한 초과 시 임의 절반 제거 (오래된 순서 정보를 따로 두지 않는 단순 정책)
                    for key in self.featurePoints.keys.prefix(self.featurePoints.count / 2) {
                        self.featurePoints.removeValue(forKey: key)
                        self.featureObs.removeValue(forKey: key)
                    }
                    self.stableCount = self.featureObs.values.filter { $0 >= self.minObs }.count
                }
            }
            if let t = self.target, frame.camera.trackingState == .normal, !moving, frame.timestamp - self.lastCoverTime > 0.1 {
                self.lastCoverTime = frame.timestamp
                if self.coverage.add(camPos: pos, camFwd: fwd, center: t) {  // 새 칸이 채워질 때마다 가볍게 진동
                    if Date().timeIntervalSince(self.lastHaptic) > 0.35 {
                        self.lastHaptic = Date()
                        UIImpactFeedbackGenerator(style: .light).impactOccurred()
                    }
                    if !self.completeNotified, self.coverage.complete {
                        self.completeNotified = true
                        UINotificationFeedbackGenerator().notificationOccurred(.success)  // 한 바퀴 완료
                    }
                }
            }
            if self.hasLiDAR {
                // 깊이 융합: 10Hz로 누적 (정상 추적 + 느린 움직임일 때만 — 번진 깊이가 섞이지 않게)
                if frame.camera.trackingState == .normal, !moving, frame.timestamp - self.lastFusionTime > 0.1, let d = depthBox {
                    self.lastFusionTime = frame.timestamp
                    self.fusion.integrate(depth: d, confidence: confBox, intrinsics: camK, imageSize: camSize, transform: camT)
                }
                self.featurePointCount = self.fusion.stableCount
            } else {
                self.featurePointCount = self.stableCount  // 안정 점(여러 프레임 관측)만 센다
            }

            self.trackingNormal = frame.camera.trackingState == .normal
            // 위치 재인식(relocalizing)이 끝나면 ARKit이 좌표계를 옮길 수 있다 — 그 전에 쌓은 깊이·특징점은 옛 좌표라
            // 새 점과 겹치면 사물이 두 곳에 번져 보이고 맞춘 포즈가 옆으로 빗나간다. 쌓은 점을 비우고 다시 모은다.
            if case .limited(.relocalizing) = frame.camera.trackingState { self.sawRelocalizing = true }
            else if self.trackingNormal, self.sawRelocalizing {
                self.sawRelocalizing = false
                if self.hasLiDAR { self.fusion.reset() }
                self.featurePoints.removeAll(); self.featureObs.removeAll(); self.stableCount = 0
                DebugLog.log("ar", "위치 재인식 완료 → 이전 좌표로 쌓은 점을 비우고 다시 모읍니다")
            }
            if self.trackingNormal { self.recordCamera(frame.camera.transform) }
            if self.awaitingRelocalization {
                if !self.trackingNormal { self.sawNonNormalSinceStart = true }
                else if self.sawNonNormalSinceStart {  // 재위치 인식 중(non-normal) → normal 로 돌아옴 = 성공
                    self.awaitingRelocalization = false
                    self.relocalized = true
                    DebugLog.log("ar", "월드맵 재위치 인식 성공")
                }
            }
            if self.trackingNormal == false {
                switch frame.camera.trackingState {
                case .limited(.initializing): self.statusText = "공간 인식 초기화 중… 천천히 둘러보세요"
                case .limited(.excessiveMotion): self.statusText = "움직임이 너무 빠릅니다"
                case .limited(.insufficientFeatures): self.statusText = "특징이 부족합니다 — 텍스처가 있는 곳을 봐주세요"
                case .limited(.relocalizing): self.statusText = "위치 재인식 중… 스캔 당시 장면을 비춰주세요"
                default: self.statusText = "추적 불안정"
                }
            } else {
                self.statusText = self.motionTooFast ? "천천히 움직여 주세요 — 빠르면 점이 기록되지 않습니다" : "추적 정상"
            }
            if self.statusText != self.lastLoggedStatus {  // 상태 변화 때만 로그
                self.lastLoggedStatus = self.statusText
                DebugLog.log("ar", "추적: \(self.statusText)", extra: ["featurePoints": "\(self.featurePoints.count)"])
            }

            for id in newMeshIDs where !self.meshAnchorIDs.contains(id) {
                self.meshAnchorIDs.insert(id)
            }
            let triCount = frame.anchors.reduce(0) { $0 + (($1 as? ARMeshAnchor)?.geometry.faces.count ?? 0) }
            self.meshTriangleCount = triCount

            // depth 유효비는 0.5초마다 갱신 (전체 순회 비용 절감)
            if Date().timeIntervalSince(self.lastDepthUpdate) > 0.5, let depth = frame.sceneDepth {
                self.lastDepthUpdate = Date()
                let map = depth.depthMap
                CVPixelBufferLockBaseAddress(map, .readOnly)
                defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
                if let base = CVPixelBufferGetBaseAddress(map) {
                    let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map)
                    let bpr = CVPixelBufferGetBytesPerRow(map)
                    let ptr = base.assumingMemoryBound(to: Float32.self)
                    var valid = 0, total = 0
                    for y in stride(from: 0, to: h, by: 16) {
                        for x in stride(from: 0, to: w, by: 16) {
                            let d = ptr[y * bpr / 4 + x]
                            if d.isFinite, d > 0 { valid += 1 }
                            total += 1
                        }
                    }
                    self.depthValidRatio = total > 0 ? Double(valid) / Double(total) : 0
                }
            }
            self.onFrame?(frame)
        }
    }

    nonisolated func sessionWasInterrupted(_ session: ARSession) {
        Task { @MainActor in self.onInterruption?(true) }
    }

    nonisolated func sessionInterruptionEnded(_ session: ARSession) {
        Task { @MainActor in self.onInterruption?(false) }
    }

    nonisolated func sessionShouldAttemptRelocalization(_ session: ARSession) -> Bool {
        MainActor.assumeIsolated { self.attemptRelocalization }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        let code = (error as? ARError)?.code
        Task { @MainActor in
            self.statusText = "AR 오류: \(error.localizedDescription)"
            DebugLog.error("ar", "세션 오류: \(error.localizedDescription)", extra: ["code": "\(code?.rawValue ?? -1)"])
            // 지원하지 않는 구성(부가 옵션 조합)이면 최소 구성으로 한 번 다시 시작한다
            if code == .unsupportedConfiguration, !self.triedMinimalConfig {
                self.triedMinimalConfig = true
                session.run(ARWorldTrackingConfiguration(), options: [.resetTracking, .removeExistingAnchors])
                self.awaitingRelocalization = false; self.relocalized = false
                DebugLog.log("ar", "최소 구성으로 세션 재시작")
            }
        }
    }
}

// MARK: - SwiftUI 래퍼

struct ARScanView: UIViewRepresentable {
    let controller: ScanSessionController

    func makeUIView(context: Context) -> ARView { controller.arView }
    func updateUIView(_ uiView: ARView, context: Context) {}
}

// MARK: - RPY → 쿼터니언 (intrinsic Z-Y-X, COORDINATE.md 1절)

extension simd_quatf {
    init(rpyDeg rpy: RPYJSON) {
        let d2r = Float.pi / 180
        let yaw = Float(rpy.yaw) * d2r, pitch = Float(rpy.pitch) * d2r, roll = Float(rpy.roll) * d2r
        // ARKit Y-up: RPY의 Z축(yaw)은 월드 Y축에 대응 (intrinsic Z-Y-X)
        let q = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
            * simd_quatf(angle: pitch, axis: SIMD3(1, 0, 0))
            * simd_quatf(angle: roll, axis: SIMD3(0, 0, 1))
        self.init(ix: q.vector[0], iy: q.vector[1], iz: q.vector[2], r: q.vector[3])
    }
}

// MARK: - ARKit 메시 버퍼 접근 (서브스크립트 미제공 → MTLBuffer 직독)

extension ARGeometrySource {
    /// float3 정점 버퍼를 SIMD3<Float> 배열로 읽기 (stride/offset 존중)
    func simd3Array() -> [SIMD3<Float>] {
        let base = buffer.contents().advanced(by: offset)
        return (0..<count).map { i in
            SIMD3<Float>(base.loadUnaligned(fromByteOffset: i * stride, as: Float.self),
                         base.loadUnaligned(fromByteOffset: i * stride + 4, as: Float.self),
                         base.loadUnaligned(fromByteOffset: i * stride + 8, as: Float.self))
        }
    }
}

extension ARGeometryElement {
    /// 인덱스 버퍼(u16/u32)를 Int32 평탄 배열로 읽기
    func int32Array() -> [Int32] {
        let base = buffer.contents()
        var out = [Int32]()
        out.reserveCapacity(count)
        if bytesPerIndex == 2 {
            let p = base.assumingMemoryBound(to: UInt16.self)
            for i in 0..<count { out.append(Int32(p[i])) }
        } else {
            let p = base.assumingMemoryBound(to: Int32.self)
            for i in 0..<count { out.append(p[i]) }
        }
        return out
    }
}

// MARK: - 월드맵 직렬화 (NSSecureCoding 경유)

extension ARWorldMap {
    func serializedData() throws -> Data {
        try NSKeyedArchiver.archivedData(withRootObject: self, requiringSecureCoding: true)
    }

    static func deserialize(from data: Data) throws -> ARWorldMap {
        guard let map = try NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: data) else {
            throw NSError(domain: "Markerless", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "월드맵 역직렬화 실패"])
        }
        return map
    }
}

// MARK: - 외곽선(와이어프레임) 렌더링 — 가는 사각 기둥으로 선을 그린다

/// 선분들을 하나의 메시로 (선마다 8정점·8삼각형). RealityKit에는 선 프리미티브가 없어 얇은 기둥을 쓴다.
func lineMesh(_ segs: [(SIMD3<Float>, SIMD3<Float>)], thickness t: Float) -> MeshResource? {
    var pos: [SIMD3<Float>] = []
    var idx: [UInt32] = []
    pos.reserveCapacity(segs.count * 8)
    idx.reserveCapacity(segs.count * 24)
    let quads: [[UInt32]] = [[0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    for (a, b) in segs {
        let d = b - a
        let len = simd_length(d)
        guard len > 1e-5 else { continue }
        let dn = d / len
        let ref: SIMD3<Float> = abs(dn.y) < 0.9 ? SIMD3(0, 1, 0) : SIMD3(1, 0, 0)
        let n1 = simd_normalize(simd_cross(dn, ref)) * t
        let n2 = simd_normalize(simd_cross(dn, n1)) * t
        let offs = [-n1 - n2, n1 - n2, n1 + n2, -n1 + n2]
        let base = UInt32(pos.count)
        for o in offs { pos.append(a + o) }
        for o in offs { pos.append(b + o) }
        for q in quads {
            idx.append(contentsOf: [base + q[0], base + q[1], base + q[2], base + q[0], base + q[2], base + q[3]])
        }
    }
    guard !pos.isEmpty else { return nil }
    var d = MeshDescriptor(name: "outlineLines")
    d.positions = MeshBuffer(pos)
    d.primitives = .triangles(idx)
    return try? MeshResource.generate(from: [d])
}

extension OutlineJSON {
    /// 높이별 윤곽선 선분
    var contourSegments: [(SIMD3<Float>, SIMD3<Float>)] {
        lines.compactMap { l in
            guard l.count == 6 else { return nil }
            return (SIMD3(Float(l[0]), Float(l[1]), Float(l[2])), SIMD3(Float(l[3]), Float(l[4]), Float(l[5])))
        }
    }

    /// 외곽 박스 12모서리 (코너 인덱스 = sx*4 + sy*2 + sz → 한 비트만 다른 쌍을 잇는다)
    var boxSegments: [(SIMD3<Float>, SIMD3<Float>)] {
        guard obb.corners.count == 8 else { return [] }
        let c = obb.corners.map { SIMD3<Float>(Float($0[0]), Float($0[1]), Float($0[2])) }
        var out: [(SIMD3<Float>, SIMD3<Float>)] = []
        for i in 0..<8 { for bit in [1, 2, 4] where i & bit == 0 { out.append((c[i], c[i | bit])) } }
        return out
    }
}

/// 외곽선 엔티티(자산 로컬 좌표): 윤곽선은 지정 색, 외곽 박스는 흰색 — 포즈는 호출자가 설정
@MainActor
/// 외곽 박스·치수선 색 (외곽선은 판정색: 설계 파랑 / 합격 초록 / 불합격 빨강 / 재확인 주황)
let boxLineColor = UIColor(red: 0.78, green: 0.47, blue: 1.0, alpha: 1)

func makeOutlineEntity(_ o: OutlineJSON, color: UIColor, contourThickness: Float = 0.0004,
                       contour: Bool = true, box: Bool = true) -> Entity {
    let root = Entity()
    if contour, let m = lineMesh(o.contourSegments, thickness: contourThickness) {
        root.addChild(ModelEntity(mesh: m, materials: [UnlitMaterial(color: color)]))
    }
    if box, let m = lineMesh(o.boxSegments, thickness: contourThickness * 1.5) {
        root.addChild(ModelEntity(mesh: m, materials: [UnlitMaterial(color: color.withAlphaComponent(0.9))]))
    }
    return root
}

/// 점군: 모델 정점(삼각형당 첫 점)을 최대 3000개, 2mm 십자로 그린다
func makePointsEntity(_ mesh: GLBMesh, color: UIColor) -> Entity {
    let root = Entity()
    let step = max(1, mesh.positions.count / 3 / 3000)
    var segs: [(SIMD3<Float>, SIMD3<Float>)] = []
    var i = 0
    while i < mesh.positions.count {
        let p = mesh.positions[i]
        let h: Float = 0.001
        segs.append((p - [h, 0, 0], p + [h, 0, 0]))
        segs.append((p - [0, h, 0], p + [0, h, 0]))
        segs.append((p - [0, 0, h], p + [0, 0, h]))
        i += 3 * step
    }
    if let m = lineMesh(segs, thickness: 0.0003) {
        root.addChild(ModelEntity(mesh: m, materials: [UnlitMaterial(color: color)]))
    }
    return root
}

// MARK: - 광선-삼각형 교차 (Möller–Trumbore)

private func rayTriangleDist(_ o: SIMD3<Float>, _ d: SIMD3<Float>,
                             _ a: SIMD3<Float>, _ b: SIMD3<Float>, _ c: SIMD3<Float>) -> Float? {
    let e1 = b - a
    let e2 = c - a
    let p = simd_cross(d, e2)
    let det = simd_dot(e1, p)
    if abs(det) < 1e-8 { return nil }
    let inv = 1 / det
    let tv = o - a
    let u = simd_dot(tv, p) * inv
    if u < 0 || u > 1 { return nil }
    let q = simd_cross(tv, e1)
    let v = simd_dot(d, q) * inv
    if v < 0 || u + v > 1 { return nil }
    let dist = simd_dot(e2, q) * inv
    return dist > 0.01 ? dist : nil  // 카메라 바로 앞 1cm 이내 무시
}
