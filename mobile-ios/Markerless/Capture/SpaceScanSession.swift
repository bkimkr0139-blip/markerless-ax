// SpaceScanSession.swift — 공간 전체 스캔(RGB-D 키프레임 수집) 컨트롤러 + 디스크 저장소
//
// ── 카메라 소유권 설계 (중요) ─────────────────────────────────────────────────────────
// 앱에는 카메라가 하나뿐이고, ScanSessionController가 `static weak var active` 로 "지금 카메라를 쥔 컨트롤러"를
// 한 개만 허용한다 (start()가 이전 active를 pause, 늦게 온 pause()는 무시). 이 컨트롤러는 그 패턴을 그대로 따른다:
//   - SpaceScanController가 자기 ScanSessionController 한 개(`ar`, lazy)를 소유한다. 새 ARSession을 따로 만들지 않는다.
//   - 사물 스캔(ScanAssetView)·검수(InspectionView)도 각자 컨트롤러를 갖지만 동시에 화면에 있는 것은 하나뿐이고,
//     화면이 바뀔 때 start()/pause()의 active 규칙이 세션 하나만 돌게 보장한다.
//   - ScanHubView는 모드 ③ ↔ ①② 전환 시 한쪽 뷰만 남긴다(switch). 동시에 두 ARView가 살아 있지 않다.
//   - RoomPlan 구조 패스는 RoomCaptureSession이 자체 ARSession을 쓰므로, 시작 전에 `ar.pause()`로 카메라를 비우고
//     끝난 뒤 `ar.start()`로 되돌린다 (순차 패스). 두 세션의 좌표계는 서로 다르다 → structure.json에 frame을 명시.
// 프레임은 ScanSessionController.onFrame 훅으로 받는다 (기존 사물 스캔 로직은 건드리지 않음).
//
// ── 디스크 레이아웃 ───────────────────────────────────────────────────────────────────
// Documents/SpaceScans/<sessionId>/session.json                     (SpaceSessionRecord)
//                                 /frames/<idx>.jpg|.depth|.conf|.json
//                                 /structure.json                   (RoomPlan 변환 결과, 선택)
// Documents/SpaceScans/queue.json                                    (업로드 대기 sessionId 목록)
// 메모리에는 프레임별 메타 스텁(인덱스·시각·시선)만 둔다. 업로드는 [완료] 후 FrameUploader가 한다
// (촬영 중 [방금 구간 제외]로 지운 프레임이 이미 올라가 있는 경합을 피하려는 설계).

import ARKit
import Combine
import CoreImage
import CoreLocation
import CoreMotion
import SwiftUI
import simd
#if canImport(RoomPlan)
import RoomPlan
#endif

// MARK: - 상태

enum SpaceScanState: String {
    case idle = "IDLE"
    case scanning = "SCANNING", paused = "PAUSED", recovery = "RECOVERY", processing = "PROCESSING"
    case ready = "READY", needMoreCapture = "NEED_MORE_CAPTURE", failed = "FAILED"

    var label: String {
        switch self {
        case .idle: return "대기"
        case .scanning: return "촬영 중"
        case .paused: return "일시정지"
        case .recovery: return "복구 중"
        case .processing: return "처리 중"
        case .ready: return "완료"
        case .needMoreCapture: return "추가 촬영 필요"
        case .failed: return "실패"
        }
    }

    var symbol: String {
        switch self {
        case .idle: return "viewfinder"
        case .scanning: return "record.circle"
        case .paused: return "pause.circle"
        case .recovery: return "exclamationmark.arrow.triangle.2.circlepath"
        case .processing: return "gearshape.2"
        case .ready: return "checkmark.seal.fill"
        case .needMoreCapture: return "plus.viewfinder"
        case .failed: return "xmark.octagon.fill"
        }
    }

    var color: Color {
        switch self {
        case .scanning: return .red
        case .paused: return .yellow
        case .recovery: return .orange
        case .processing: return .blue
        case .ready: return .green
        case .needMoreCapture: return .orange
        case .failed: return .red
        case .idle: return .gray
        }
    }

    /// 서버 세션 상태 문자열 → 화면 상태
    static func fromServer(_ s: String) -> SpaceScanState {
        switch s.uppercased() {
        case "READY": return .ready
        case "NEED_MORE_CAPTURE": return .needMoreCapture
        case "FAILED": return .failed
        case "RECOVERY": return .recovery
        case "PAUSED": return .paused
        case "SCANNING": return .scanning
        default: return .processing
        }
    }
}

// MARK: - 세션 기록 (디스크)

struct SpaceSessionRecord: Codable, Identifiable {
    var id: String  // 로컬 UUID — idempotencyKey = "<id>-<frameIndex>"
    var spaceNumericId: Int
    var spaceCode: String
    var create: ScanSessionCreateJSON
    var serverSessionId: String?
    var frameIndices: [Int] = []
    var uploaded: [Int] = []
    var failed: [Int] = []
    var epochs: [String: Int] = [:]  // 프레임별 재시도 세대 (해시 불일치 시 새 idempotencyKey)
    var captureFinished = false
    var endedReason: String?
    var hasStructure = false
    var structureUploaded = false
    var finalized = false
    var reconstructQueued = false
    var serverStatus: String?
    var message: String?
    var createdAt = Date()
    var segmentCount = 1
    var excludedCount = 0
}

enum SpaceScanStore {
    static var root: URL {
        let d = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("SpaceScans")
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }
    static func dir(_ id: String) -> URL { root.appendingPathComponent(id) }
    static func framesDir(_ id: String) -> URL {
        let d = dir(id).appendingPathComponent("frames")
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }
    static func frameURL(_ id: String, _ idx: Int, _ ext: String) -> URL { framesDir(id).appendingPathComponent("\(idx).\(ext)") }

    static func save(_ r: SpaceSessionRecord) {
        try? FileManager.default.createDirectory(at: dir(r.id), withIntermediateDirectories: true)
        let enc = JSONEncoder(); enc.dateEncodingStrategy = .iso8601
        if let d = try? enc.encode(r) { try? d.write(to: dir(r.id).appendingPathComponent("session.json"), options: .atomic) }
    }

    static func load(_ id: String) -> SpaceSessionRecord? {
        guard let d = try? Data(contentsOf: dir(id).appendingPathComponent("session.json")) else { return nil }
        let dec = JSONDecoder(); dec.dateDecodingStrategy = .iso8601
        return try? dec.decode(SpaceSessionRecord.self, from: d)
    }

    // 업로드 대기열
    private static var queueURL: URL { root.appendingPathComponent("queue.json") }
    static func queue() -> [String] {
        (try? JSONDecoder().decode([String].self, from: Data(contentsOf: queueURL))) ?? []
    }
    static func setQueue(_ ids: [String]) {
        if let d = try? JSONEncoder().encode(ids) { try? d.write(to: queueURL, options: .atomic) }
    }

    /// 앱이 촬영 도중 종료된 세션(캡처 미완료)을 찾아 디스크의 프레임 파일 기준으로 마감한다. 반환: 마감된 id들
    @discardableResult
    static func recoverUnfinished() -> [String] {
        var out: [String] = []
        let ids = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        for id in ids {
            guard var r = load(id), !r.captureFinished else { continue }
            let files = (try? FileManager.default.contentsOfDirectory(atPath: framesDir(id).path)) ?? []
            let idx = files.compactMap { $0.hasSuffix(".json") ? Int($0.dropLast(5)) : nil }.sorted()
            r.frameIndices = idx.filter { FileManager.default.fileExists(atPath: frameURL(id, $0, "conf").path) }
            r.captureFinished = true; r.endedReason = "APP_RESTART_RECOVERED"
            save(r)
            out.append(id)
        }
        return out
    }

    static func diskFreeBytes() -> Int64 {
        let v = try? root.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return v?.volumeAvailableCapacityForImportantUsage ?? Int64.max
    }
}

// MARK: - 프레임 디스크 기록 (백그라운드)

enum SpaceFrameWriter {
    private static let ci = CIContext()
    /// 서버 rgb 파트 한도는 12MB (backend twin.py MAX_PART_BYTES) — 여유를 두고 6MB 아래로 유지
    static let maxJPEGBytes = 6_000_000

    struct Out { var jpegBytes: Int; var depthBytes: Int }

    struct DepthPlanes {
        var depth: [Float]  // m, 무효 = 0
        var conf: [UInt8]  // 0/1/2
        var width: Int
        var height: Int
    }

    /// depth(float32 m) + conf(uint8) CVPixelBuffer → 행 패딩 제거 배열. 비유한/비양수 깊이 → 0.
    nonisolated static func extractPlanes(depth: CVPixelBuffer, conf: CVPixelBuffer) -> DepthPlanes {
        let dw = CVPixelBufferGetWidth(depth), dh = CVPixelBufferGetHeight(depth)
        var d = [Float](repeating: 0, count: dw * dh)
        CVPixelBufferLockBaseAddress(depth, .readOnly)
        if let b = CVPixelBufferGetBaseAddress(depth) {
            let bpr = CVPixelBufferGetBytesPerRow(depth)
            for y in 0..<dh {
                let row = (b + y * bpr).assumingMemoryBound(to: Float32.self)
                for x in 0..<dw { let v = row[x]; d[y * dw + x] = (v.isFinite && v > 0) ? v : 0 }
            }
        }
        CVPixelBufferUnlockBaseAddress(depth, .readOnly)
        var c = [UInt8](repeating: 0, count: dw * dh)
        CVPixelBufferLockBaseAddress(conf, .readOnly)
        if let b = CVPixelBufferGetBaseAddress(conf), CVPixelBufferGetWidth(conf) == dw, CVPixelBufferGetHeight(conf) == dh {
            let bpr = CVPixelBufferGetBytesPerRow(conf)
            for y in 0..<dh {
                let row = (b + y * bpr).assumingMemoryBound(to: UInt8.self)
                for x in 0..<dw { c[y * dw + x] = min(2, row[x]) }
            }
        }
        CVPixelBufferUnlockBaseAddress(conf, .readOnly)
        return DepthPlanes(depth: d, conf: c, width: dw, height: dh)
    }

    /// 휘도(Y) 평면을 ~320px 너비로 박스 샘플링(블록당 최대 3x3 점) 후 Laplacian 분산. 메인 액터에서 부르는 경우 창당 수 ms 수준.
    nonisolated static func sharpness(of image: CVPixelBuffer, targetWidth: Int = 320) -> Float {
        guard CVPixelBufferGetPlaneCount(image) > 0 else { return 0 }
        CVPixelBufferLockBaseAddress(image, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(image, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(image, 0) else { return 0 }
        let w = CVPixelBufferGetWidthOfPlane(image, 0), h = CVPixelBufferGetHeightOfPlane(image, 0)
        let bpr = CVPixelBufferGetBytesPerRowOfPlane(image, 0)
        let step = max(1, w / targetWidth)
        let ow = w / step, oh = h / step
        guard ow >= 3, oh >= 3 else { return 0 }
        let p = base.assumingMemoryBound(to: UInt8.self)
        let sub = max(1, step / 3)
        var luma = [UInt8](repeating: 0, count: ow * oh)
        for oy in 0..<oh {
            for ox in 0..<ow {
                var acc = 0, n = 0
                var yy = oy * step
                while yy < oy * step + step { 
                    var xx = ox * step
                    while xx < ox * step + step { acc += Int(p[yy * bpr + xx]); n += 1; xx += sub }
                    yy += sub
                }
                luma[oy * ow + ox] = UInt8(acc / max(1, n))
            }
        }
        return SharpnessMath.laplacianVariance(luma: luma, width: ow, height: oh)
    }

    /// RGB(JPEG, 긴 변 ≤ maxEdge, 6MB 이하) + depth(float32 m, 무효=0) + conf(uint8)를 쓰고 마지막에 json(메타)을 쓴다.
    /// json이 있으면 그 프레임은 완전하다 (복구 기준).
    /// kResolution = rawIntrinsics K가 정의된 픽셀 격자(= 그 ARFrame의 camera.imageResolution). 이미지 버퍼와 다르면 먼저 맞춘다.
    nonisolated static func write(sessionId: String, meta base: FrameMetaJSON, aux: AuxSampleJSON?,
                                  image: CVPixelBuffer, planes: DepthPlanes,
                                  rawIntrinsics K: simd_float3x3, kResolution: CGSize, maxEdge: Int, quality: CGFloat) -> Out? {
        let w = CVPixelBufferGetWidth(image), h = CVPixelBufferGetHeight(image)
        var s = Float(min(1.0, Double(maxEdge) / Double(max(w, h))))
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        func encode(_ scale: Float, _ q: CGFloat) -> Data? {
            let ciImg = CIImage(cvPixelBuffer: image).transformed(by: CGAffineTransform(scaleX: CGFloat(scale), y: CGFloat(scale)))
            return ci.jpegRepresentation(of: ciImg, colorSpace: cs,
                options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): q])
        }
        guard var jpeg = encode(s, quality) else { return nil }
        // 크기 가드: 품질을 낮추고, 그래도 크면 해상도를 25% 줄인다
        for q in [quality - 0.08, quality - 0.16] where jpeg.count > maxJPEGBytes {
            if let j = encode(s, max(0.5, q)) { jpeg = j }
        }
        if jpeg.count > maxJPEGBytes {
            s *= 0.75
            if let j = encode(s, max(0.5, quality - 0.16)) { jpeg = j }
        }

        let dw = planes.width, dh = planes.height
        let depthData = planes.depth.withUnsafeBytes { Data($0) }
        let confData = Data(planes.conf)

        var m = base
        let outW = Int((Float(w) * s).rounded()), outH = Int((Float(h) * s).rounded())
        m.imageWidth = outW; m.imageHeight = outH
        m.depthWidth = dw; m.depthHeight = dh
        // 열 우선 3x3: col0=(fx,0,0) col1=(0,fy,0) col2=(cx,cy,1) — 저장한 RGB의 픽셀 격자 기준.
        // 규약: 픽셀 i의 중심 = i+0.5, cx/cy는 좌상단 모서리 기준 → 해상도 비율로 선형 스케일 (StillCapture.scaleIntrinsics 참고)
        m.intrinsics = StillCapture.scaleIntrinsics(fx: K[0][0], fy: K[1][1], cx: K[2][0], cy: K[2][1],
                                                    fromW: Float(kResolution.width), fromH: Float(kResolution.height),
                                                    toW: Float(outW), toH: Float(outH))

        let id = sessionId, i = m.frameIndex
        do {
            try jpeg.write(to: SpaceScanStore.frameURL(id, i, "jpg"), options: .atomic)
            try depthData.write(to: SpaceScanStore.frameURL(id, i, "depth"), options: .atomic)
            try confData.write(to: SpaceScanStore.frameURL(id, i, "conf"), options: .atomic)
            let rec = try JSONEncoder().encode(FrameDiskRecord(meta: m, aux: aux))
            try rec.write(to: SpaceScanStore.frameURL(id, i, "json"), options: .atomic)
        } catch { return nil }
        return Out(jpegBytes: jpeg.count, depthBytes: depthData.count + confData.count)
    }

    static func delete(sessionId: String, index: Int) {
        for e in ["json", "jpg", "depth", "conf"] { try? FileManager.default.removeItem(at: SpaceScanStore.frameURL(sessionId, index, e)) }
    }
}

// MARK: - 보조 센서 (절대 진실 아님 — 참고 메타데이터)

@MainActor
final class AuxSensors: NSObject, CLLocationManagerDelegate {
    private let altimeter = CMAltimeter()
    private let loc = CLLocationManager()
    private var relAlt: Double?
    private var pressure: Double?
    private var heading: CLHeading?
    private(set) var running = false

    nonisolated static var hasBarometer: Bool { CMAltimeter.isRelativeAltitudeAvailable() }
    nonisolated static var hasHeading: Bool { CLLocationManager.headingAvailable() }

    func start() {
        guard !running else { return }
        running = true
        if Self.hasBarometer {
            altimeter.startRelativeAltitudeUpdates(to: .main) { [weak self] d, _ in
                guard let d else { return }
                self?.relAlt = d.relativeAltitude.doubleValue
                self?.pressure = d.pressure.doubleValue
            }
        }
        if Self.hasHeading { loc.delegate = self; loc.startUpdatingHeading() }
    }

    func stop() {
        guard running else { return }
        running = false
        altimeter.stopRelativeAltitudeUpdates()
        loc.stopUpdatingHeading()
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading) {
        Task { @MainActor in self.heading = newHeading }
    }

    func sample() -> AuxSampleJSON {
        let g = LocationProvider.shared.current
        return AuxSampleJSON(
            relAltitudeM: relAlt, pressureKPa: pressure,
            headingDeg: heading.map { $0.trueHeading >= 0 ? $0.trueHeading : $0.magneticHeading },
            headingAccuracyDeg: heading.map { $0.headingAccuracy },
            lat: g?.coordinate.latitude, lon: g?.coordinate.longitude, gpsAccuracyM: g?.horizontalAccuracy)
    }
}

// MARK: - 기기 능력

enum SpaceScanDevice {
    static var modelIdentifier: String {
        var u = utsname(); uname(&u)
        return withUnsafePointer(to: &u.machine) { $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) } }
    }

    static var roomPlanSupported: Bool {
        #if canImport(RoomPlan)
        return RoomCaptureSession.isSupported
        #else
        return false
        #endif
    }

    /// LiDAR 메시 + 장면 깊이 둘 다 되어야 RGB-D 공간 스캔이 가능하다 (센서 정확도는 가정하지 않는다 — 서버가 신뢰도로 걸러냄)
    static var canCaptureRGBD: Bool {
        ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
            && ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    static func capabilities() -> [String: Bool] {
        [
            "sceneReconstructionMesh": ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
            "sceneDepth": ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
            "smoothedSceneDepth": ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth),
            "roomPlan": roomPlanSupported,
            "barometer": AuxSensors.hasBarometer,
            "heading": AuxSensors.hasHeading,
            "rawSceneDepthUsed": true,
        ]
    }

    static func profile() -> DeviceProfileJSON {
        DeviceProfileJSON(model: modelIdentifier, hasLiDAR: canCaptureRGBD,
                          osVersion: UIDevice.current.systemVersion, appBuild: BuildInfo.number)
    }
}

// MARK: - 컨트롤러

@MainActor
final class SpaceScanController: ObservableObject {
    /// 이 컨트롤러가 쓰는 유일한 ScanSessionController(= ARSession 하나). 화면에 들어올 때 처음 만든다.
    lazy var ar: ScanSessionController = {
        let c = ScanSessionController()
        c.attemptRelocalization = true
        c.onFrame = { [weak self] f in self?.handle(f) }
        c.onInterruption = { [weak self] began in self?.interruption(began) }
        return c
    }()

    @Published private(set) var state: SpaceScanState = .idle
    @Published private(set) var segmentIndex = 0
    @Published private(set) var keyframeCount = 0
    @Published private(set) var storedMB: Double = 0
    @Published private(set) var coverage = SpaceCoverageGrid()
    @Published private(set) var hint = "시작을 누르고 천천히 방 안을 둘러보세요"
    @Published private(set) var recoveryText = ""
    @Published private(set) var notice: String?  // 열/메모리/디스크 적응 안내
    @Published private(set) var heading: Float = 0  // 현재 시선 방위(rad) — HUD 링용
    @Published private(set) var lastExcluded = 0
    @Published var mode = "SPACE"  // SPACE | FACILITY
    @Published private(set) var recordId: String?
    @Published private(set) var serverMessage: String?
    @Published private(set) var progress: FrameUploader.Progress?
    // ── 품질 HUD (촬영 품질 알약)
    @Published private(set) var qualitySharpRatio: Double?  // 선택 창 중 선명하게 통과한 비율
    @Published private(set) var qualityMeanConf: Double?  // 키프레임 평균 깊이 신뢰도 (0...2)
    @Published private(set) var qualityDenoisedPct: Double?  // 정지 시간평균 적용 키프레임 %
    @Published private(set) var highResCount = 0
    /// 고화질 사진 옵션 (기본 OFF). 촬영 시작 전(대기 중)에만 바꾼다.
    @Published private(set) var highRes = false
    var highResAvailable: Bool { ScanSessionController.highResStillSupported }

    private var record: SpaceSessionRecord?
    private var gate = KeyframeGate()
    private let aux = AuxSensors()
    private var cancellables = Set<AnyCancellable>()

    private struct Stub { var index: Int; var t: TimeInterval; var fwd: SIMD3<Float> }
    private var stubs: [Stub] = []
    private var nextIndex = 0
    private var inFlight = 0
    private var bytes: Int64 = 0
    private var lastCapture: TimeInterval = 0
    private var hasHadNormal = false
    private var lossStart: TimeInterval?
    private var interrupted = false
    private var prevT: simd_float4x4?
    private var prevTs: TimeInterval = 0
    private var lowMemory = false
    private var lastHintAt: TimeInterval = 0
    private let maxFrames = 2500

    // 품질 로직 상태
    private struct Candidate { var frame: ARFrame; var blur: Float; var sharp: Float }
    private var picker = BestFramePicker<Candidate>()
    private var windowMaxSharp: Float = 0
    private var sharpRef: Float?
    private var depthRing = DepthRing()
    private var windowsTotal = 0, windowsSharp = 0
    private var keyframesTotal = 0, keyframesDenoised = 0
    private var confSum = 0.0
    private var blurHintUntil: TimeInterval = 0
    /// 정지 판정 (각속도 rad/s, 선속도 m/s)
    private let stillAng: Float = 0.05, stillLin: Float = 0.03

    private final class Once { var done = false }

    init() {
        NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.lowMemory = true
                    self?.notice = "메모리 부족 — 해상도와 촬영 간격을 낮춥니다"
                }
            }.store(in: &cancellables)
        FrameUploader.shared.$progress
            .sink { [weak self] all in
                guard let self, let id = self.recordId, let p = all[id] else { return }
                self.progress = p
                self.serverMessage = p.message
                if self.state == .processing || self.state == .ready || self.state == .needMoreCapture || self.state == .failed,
                   let s = p.serverStatus {
                    self.state = SpaceScanState.fromServer(s)
                }
            }.store(in: &cancellables)
    }

    var canCapture: Bool { SpaceScanDevice.canCaptureRGBD }
    var isActive: Bool { state == .scanning || state == .recovery }
    var hasSession: Bool { record != nil && !(record?.captureFinished ?? true) }

    // MARK: 생명주기

    func screenAppeared() {
        ar.start(restoringWorldMap: nil)  // 첫 진입=새 세션, 복귀=이어서 재개 (사물 스캔과 같은 규약)
        LocationProvider.shared.start()
    }

    func screenDisappeared() {
        if isActive { pause(auto: true) }
        ar.pause()
        UIApplication.shared.isIdleTimerDisabled = false
    }

    private func ensureRecord(app: AppState) {
        if record != nil, !(record!.captureFinished) { record?.create.mode = mode; return }
        let id = UUID().uuidString
        let create = ScanSessionCreateJSON(
            mode: mode, deviceId: app.deviceId, deviceProfile: SpaceScanDevice.profile(),
            capabilities: SpaceScanDevice.capabilities(), worldMapRevision: nil, relocalized: ar.relocalized)
        record = SpaceSessionRecord(id: id, spaceNumericId: app.spaceNumericId, spaceCode: app.spaceId, create: create)
        recordId = id
        stubs.removeAll(); nextIndex = 0; bytes = 0; keyframeCount = 0; storedMB = 0
        coverage = SpaceCoverageGrid(); gate = KeyframeGate(); segmentIndex = 0
        resetQuality()
        state = .idle
        SpaceScanStore.save(record!)
    }

    func begin(app: AppState) {
        guard app.isSpaceSelected, canCapture else { return }
        ensureRecord(app: app)
        aux.start()
        hasHadNormal = false; lossStart = nil; interrupted = false; prevT = nil
        gate.startSegment(); segmentIndex = gate.segmentIndex
        state = .scanning
        UIApplication.shared.isIdleTimerDisabled = true
        DebugLog.log("space", "공간 스캔 시작 mode=\(mode)", extra: ["session": record?.id ?? "-"])
    }

    private func resetQuality() {
        picker.reset(); depthRing.reset(); windowMaxSharp = 0; sharpRef = nil
        windowsTotal = 0; windowsSharp = 0; keyframesTotal = 0; keyframesDenoised = 0; confSum = 0; highResCount = 0
        qualitySharpRatio = nil; qualityMeanConf = nil; qualityDenoisedPct = nil
    }

    /// 고화질 사진 토글 — 대기 상태에서만. 카메라 포맷이 바뀌므로 세션을 이어서(리셋 없이) 다시 돌린다.
    func setHighRes(_ on: Bool) {
        guard state == .idle, on == false || highResAvailable else { return }
        highRes = on
        ar.useHighResFormat = on
        ar.start(restoringWorldMap: nil)
    }

    func pause(auto: Bool = false) {
        guard isActive else { return }
        picker.reset(); depthRing.reset(); windowMaxSharp = 0
        state = .paused
        notice = auto ? "화면을 벗어나 일시정지했습니다" : nil
        UIApplication.shared.isIdleTimerDisabled = false
    }

    func resume() {
        guard state == .paused else { return }
        if thermal.rawValue >= ProcessInfo.ThermalState.serious.rawValue {
            notice = "기기가 아직 뜨겁습니다 — 식을 때까지 잠시 기다린 뒤 [이어서]를 눌러 주세요"
            return
        }
        depthRing.reset(); picker.reset()
        gate.startSegment()  // 멈춘 동안 이동했을 수 있다 → 새 구간으로 이어 촬영
        segmentIndex = gate.segmentIndex
        prevT = nil; lossStart = nil; notice = nil
        state = .scanning
        UIApplication.shared.isIdleTimerDisabled = true
    }

    /// 사람 등 움직이는 물체는 기기에서 감지할 수 없다. 지나간 사람이 찍혔으면 직전 N초 프레임을 지운다.
    func excludeRecent(seconds: TimeInterval = 10) {
        guard let id = record?.id, let last = stubs.last?.t else { return }
        let cut = last - seconds
        let drop = stubs.filter { $0.t >= cut }
        guard !drop.isEmpty else { return }
        for s in drop { SpaceFrameWriter.delete(sessionId: id, index: s.index) }
        stubs.removeAll { $0.t >= cut }
        var cov = SpaceCoverageGrid()
        for s in stubs { cov.add(forward: s.fwd) }  // 남은 프레임으로 커버리지를 다시 계산
        coverage = cov
        keyframeCount = stubs.count
        lastExcluded = drop.count
        record?.excludedCount += drop.count
        gate.startSegment(); segmentIndex = gate.segmentIndex
        persist()
        DebugLog.log("space", "최근 \(Int(seconds))초 \(drop.count)프레임 제외")
    }

    /// 촬영 종료 → 업로드 대기열 등록. 쓰기 중인 프레임이 끝나길 잠시 기다린다.
    func finish() {
        guard record != nil, state == .scanning || state == .paused || state == .recovery || state == .idle else { return }
        state = .processing
        aux.stop()
        UIApplication.shared.isIdleTimerDisabled = false
        Task { @MainActor in
            var waited = 0
            while self.inFlight > 0, waited < 100 { try? await Task.sleep(nanoseconds: 100_000_000); waited += 1 }
            guard var r = self.record else { return }
            r.frameIndices = self.stubs.map(\.index).sorted()
            r.captureFinished = true
            r.endedReason = "USER_FINISHED"
            r.segmentCount = self.gate.segmentIndex + 1
            self.record = r
            SpaceScanStore.save(r)
            FrameUploader.shared.enqueue(r.id)
            DebugLog.log("space", "촬영 종료 → 업로드 대기열", extra: ["frames": "\(r.frameIndices.count)", "segments": "\(r.segmentCount)"])
        }
    }

    /// NEED_MORE_CAPTURE / READY / FAILED 뒤 새 촬영으로 돌아간다 (같은 공간에 새 세션 — 병합은 서버 몫)
    func startOver() {
        record = nil; recordId = nil; progress = nil; serverMessage = nil
        stubs.removeAll(); nextIndex = 0; keyframeCount = 0; storedMB = 0
        coverage = SpaceCoverageGrid(); gate = KeyframeGate(); segmentIndex = 0
        resetQuality()
        state = .idle; notice = nil
        hint = "시작을 누르고 천천히 방 안을 둘러보세요"
    }

    func retryUpload() {
        guard let id = record?.id else { return }
        state = .processing
        FrameUploader.shared.retry(id)
    }

    // MARK: RoomPlan 구조 패스 연동

    /// RoomPlan이 카메라를 쓰는 동안 RGB-D 세션을 비운다 (순차 패스)
    func beginStructurePass(app: AppState) {
        if isActive { pause() }
        ensureRecord(app: app)
        ar.pause()
    }

    func endStructurePass(_ s: SpaceStructureJSON?) {
        if let s, let id = record?.id, let data = try? JSONEncoder().encode(s) {
            try? data.write(to: SpaceScanStore.dir(id).appendingPathComponent("structure.json"), options: .atomic)
            record?.hasStructure = true
            persist()
            notice = "구조 스캔 저장됨 — 촬영 완료 시 함께 업로드됩니다"
        }
        ar.start(restoringWorldMap: nil)  // RGB-D 패스의 카메라를 되돌려 받는다
        if state == .paused { gate.startSegment(); segmentIndex = gate.segmentIndex }  // 좌표 연속성 보장 못 함 → 새 구간
    }

    // MARK: 프레임 처리

    private func interruption(_ began: Bool) {
        interrupted = began
        guard isActive else { return }
        if began {
            state = .recovery
            recoveryText = "카메라가 중단되었습니다 — 앱으로 돌아와 직전에 찍은 장면을 다시 비춰 주세요"
            gate.startSegment(); segmentIndex = gate.segmentIndex
        }
    }

    private var thermal: ProcessInfo.ThermalState { ProcessInfo.processInfo.thermalState }

    private func handle(_ frame: ARFrame) {
        let t = frame.camera.transform
        let c = t.columns
        let fwd = simd_normalize(-SIMD3<Float>(c.2.x, c.2.y, c.2.z))
        // HUD용 시선 방위
        var az = atan2(fwd.x, -fwd.z); if az < 0 { az += 2 * .pi }
        if state == .scanning || state == .recovery || state == .paused || state == .idle { heading = az }

        // 속도 → 블러 근사
        var lin: Float = 0, ang: Float = 0
        if let pt = prevT, frame.timestamp > prevTs {
            let dt = Float(frame.timestamp - prevTs)
            lin = simd_distance(KeyframeGate.pos(t), KeyframeGate.pos(pt)) / dt
            ang = KeyframeGate.angleDeg(t, pt) * .pi / 180 / dt
        }
        prevT = t; prevTs = frame.timestamp
        guard state == .scanning || state == .recovery else { picker.reset(); depthRing.reset(); return }

        // 추적 상태 분류: 속도 초과는 '일시적'(프레임만 건너뜀), 재위치·특징 부족·중단은 '심각'(복구 안내 + 새 구간)
        let tracking = frame.camera.trackingState
        let normal = tracking == .normal
        var serious = false
        var reasonText = ""
        switch tracking {
        case .normal: hasHadNormal = true
        case .notAvailable: serious = hasHadNormal; reasonText = "카메라 추적이 끊겼습니다"
        case .limited(let r):
            switch r {
            case .relocalizing: serious = true; reasonText = "위치를 다시 찾는 중 — 방금 찍은 벽·가구 쪽을 비춰 주세요"
            case .insufficientFeatures: serious = hasHadNormal; reasonText = "특징이 부족합니다 — 무늬가 있는 곳(문, 모서리, 가구)을 비춰 주세요"
            case .initializing: serious = hasHadNormal; reasonText = "추적 초기화 중 — 천천히 둘러보세요"
            case .excessiveMotion: break
            @unknown default: break
            }
        }
        if interrupted { serious = true; reasonText = "카메라 중단 상태 — 앱을 다시 열고 직전 장면을 비춰 주세요" }

        let now = frame.timestamp
        if serious {
            if lossStart == nil { lossStart = now }
            if state == .scanning, now - (lossStart ?? now) > 0.8 {
                state = .recovery
                UINotificationFeedbackGenerator().notificationOccurred(.warning)
            }
            if state == .recovery {
                let secs = Int(now - (lossStart ?? now))
                recoveryText = reasonText + (secs > 20 ? "\n복구가 안 되면 [완료]로 여기까지 올리고 새로 이어 촬영하세요" : "")
            }
        } else {
            lossStart = nil
            if state == .recovery, normal, !interrupted {
                state = .scanning
                recoveryText = ""
            }
        }

        let jumped = gate.observe(transform: t, trackingNormal: !serious)
        if jumped {
            segmentIndex = gate.segmentIndex
            DebugLog.log("space", "추적 점프/재위치 → 구간 \(segmentIndex)")
        }
        guard state == .scanning, normal else { picker.reset(); depthRing.reset(); windowMaxSharp = 0; return }

        // 열: serious 이상이면 자동 일시정지 (이미 큐에 들어간 프레임 저장·업로드는 계속된다)
        if thermal.rawValue >= ProcessInfo.ThermalState.serious.rawValue {
            pause()
            notice = "기기가 매우 뜨거워 자동 일시정지했습니다 — 저장된 프레임은 안전합니다. 식은 뒤 [이어서]를 누르세요"
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            return
        }

        // 정지 판정 → 깊이 시간평균용 링 버퍼 (움직이면 비운다: 서로 다른 시점의 깊이를 섞지 않는다)
        let stationary = ang < stillAng && lin < stillLin
        if stationary, let sd = frame.sceneDepth, let sc = sd.confidenceMap {
            let pl = SpaceFrameWriter.extractPlanes(depth: sd.depthMap, conf: sc)
            depthRing.push(depth: pl.depth, conf: pl.conf)
        } else {
            depthRing.reset()
        }

        // 힌트 갱신 (2Hz) — 흔들림 안내가 떠 있는 동안은 덮어쓰지 않는다
        if now - lastHintAt > 0.5, now >= blurHintUntil { lastHintAt = now; hint = coverage.hint(headingRad: az) }

        // 적응: 열/메모리
        let hot = thermal.rawValue >= ProcessInfo.ThermalState.fair.rawValue
        if hot { notice = "기기 온도 상승 — 해상도·간격을 낮췄습니다" }
        let minInterval: TimeInterval = hot || lowMemory ? 0.6 : 0.25
        let maxEdge = (hot || lowMemory) ? 1280 : 1920
        gate.minMoveM = hot || lowMemory ? 0.25 : 0.15

        // 선택 창이 열려 있지 않을 때만 간격/동시 저장/최대 장수 제한을 건다
        if !picker.isOpen {
            guard now - lastCapture >= minInterval, inFlight < 2, nextIndex < maxFrames else {
                if nextIndex >= maxFrames { notice = "최대 프레임(\(maxFrames)장)에 도달했습니다 — 완료를 눌러 주세요" }
                return
            }
        }
        guard ang < 1.0, lin < 0.5 else { picker.reset(); windowMaxSharp = 0; return }  // 기존 사물 스캔과 같은 속도 상한
        guard let depth = frame.sceneDepth, depth.confidenceMap != nil, record != nil else { return }
        guard picker.isOpen || gate.motionOpen(transform: t) else { return }

        // 후보 평가: 속도 기반 블러 + 선명도(Laplacian 분산)
        let speedBlur = KeyframeGate.blurScore(angularRadPerS: ang, linearMPerS: lin,
                                               exposureS: frame.camera.exposureDuration > 0 ? Float(frame.camera.exposureDuration) : nil,
                                               fxPx: frame.camera.intrinsics[0][0])
        let sharp = SpaceFrameWriter.sharpness(of: frame.capturedImage)
        windowMaxSharp = max(windowMaxSharp, sharp)
        let blur = SharpnessMath.combine(speedBlur: speedBlur, sharpBlur: SharpnessMath.sharpBlur(sharpness: sharp, reference: sharpRef))
        guard let win = picker.offer(Candidate(frame: frame, blur: blur, sharp: sharp), score: blur, at: now) else { return }

        // 창 종료 — 가장 선명한 후보를 확정하거나, 전부 흐리면 거절
        sharpRef = SharpnessMath.updateReference(sharpRef, windowBest: windowMaxSharp)
        windowMaxSharp = 0
        windowsTotal += 1
        guard win.score <= gate.maxBlur else {
            hint = "흔들림 — 천천히"; blurHintUntil = now + 1.5
            qualitySharpRatio = Double(windowsSharp) / Double(windowsTotal)
            return
        }
        windowsSharp += 1
        qualitySharpRatio = Double(windowsSharp) / Double(windowsTotal)

        if nextIndex % 25 == 0, SpaceScanStore.diskFreeBytes() < 400_000_000 {
            notice = "저장 공간이 부족합니다 (400MB 미만) — 촬영을 일시정지합니다"
            pause()
            return
        }
        commitKeyframe(win.item, now: now, maxEdge: maxEdge, wantStill: highRes && !hot && !lowMemory && depthRing.count >= 2)
    }

    private func commitKeyframe(_ cand: Candidate, now: TimeInterval, maxEdge: Int, wantStill: Bool) {
        guard let rec = record, let sd = cand.frame.sceneDepth, let sc = sd.confidenceMap else { return }
        let frame = cand.frame
        let t = frame.camera.transform
        gate.commit(transform: t)
        lastCapture = now
        let idx = nextIndex; nextIndex += 1
        let fwd = simd_normalize(-SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        stubs.append(Stub(index: idx, t: frame.timestamp, fwd: fwd))
        if coverage.add(forward: fwd) { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
        keyframeCount = stubs.count
        inFlight += 1

        // 깊이: 정지 중이었으면 링의 연속 프레임을 평균(백그라운드), 아니면 단일 프레임
        let ringD = depthRing.depths, ringC = depthRing.confs
        let dBox = PixelBufferBox(buffer: sd.depthMap), cBox = PixelBufferBox(buffer: sc)
        let sid = rec.id, seg = gate.segmentIndex

        let go: (ARFrame?) -> Void = { [weak self] still in
            guard let self else { return }
            // 스틸 사용 조건: 포즈가 일반 프레임과 1cm/0.5° 이내 (깊이는 일반 프레임 것을 쓰므로). 아니면 일반 이미지로 대체.
            var useStill = false
            if let st = still, case .normal = st.camera.trackingState,
               StillCapture.poseCompatible(st.camera.transform, t), CVPixelBufferGetWidth(st.capturedImage) > CVPixelBufferGetWidth(frame.capturedImage) {
                useStill = true
            }
            let src = useStill ? still! : frame
            let c = src.camera.transform.columns
            let le = frame.lightEstimate
            var reason: String?
            if case .limited(let r) = frame.camera.trackingState {
                switch r {
                case .excessiveMotion: reason = "excessiveMotion"
                case .insufficientFeatures: reason = "insufficientFeatures"
                case .initializing: reason = "initializing"
                case .relocalizing: reason = "relocalizing"
                @unknown default: reason = "unknown"
                }
            }
            let averagedN = ringD.count >= 2 ? ringD.count : 1
            let meta = FrameMetaJSON(
                frameIndex: idx, segmentIndex: seg,
                timestampNs: Int64(src.timestamp * 1_000_000_000),
                cameraTransform: [c.0.x, c.0.y, c.0.z, c.0.w, c.1.x, c.1.y, c.1.z, c.1.w,
                                  c.2.x, c.2.y, c.2.z, c.2.w, c.3.x, c.3.y, c.3.z, c.3.w],
                intrinsics: [], imageWidth: 0, imageHeight: 0, depthWidth: 0, depthHeight: 0,
                trackingState: "normal",
                exposureDurationS: frame.camera.exposureDuration > 0 ? frame.camera.exposureDuration : nil,
                iso: nil,  // ARKit은 ISO를 제공하지 않는다
                whiteBalanceK: le.map { Double($0.ambientColorTemperature) },
                lightEstimateLux: le.map { Double($0.ambientIntensity) },
                motionBlurScore: Double(cand.blur),
                worldMappingStatus: Self.mappingName(frame.worldMappingStatus),
                sharpness: Double(cand.sharp),
                exposureOffset: Double(frame.camera.exposureOffset),
                ambientIntensity: le.map { Double($0.ambientIntensity) },
                ambientColorTemperature: le.map { Double($0.ambientColorTemperature) },
                trackingReason: reason,
                depthFramesAveraged: averagedN,
                highResStill: useStill ? true : nil)
            let auxSample = self.aux.sample()
            let img = PixelBufferBox(buffer: src.capturedImage)
            let K = src.camera.intrinsics, kRes = src.camera.imageResolution
            let edge = useStill ? 4032 : maxEdge, q: CGFloat = useStill ? 0.88 : 0.85
            Task.detached(priority: .utility) {
                let planes: SpaceFrameWriter.DepthPlanes
                if ringD.count >= 2 {
                    let r = DepthDenoise.average(depths: ringD, confs: ringC)
                    planes = .init(depth: r.depth, conf: r.conf, width: CVPixelBufferGetWidth(dBox.buffer), height: CVPixelBufferGetHeight(dBox.buffer))
                } else {
                    planes = SpaceFrameWriter.extractPlanes(depth: dBox.buffer, conf: cBox.buffer)
                }
                let mc = DepthDenoise.meanConfidence(depth: planes.depth, conf: planes.conf)
                let out = SpaceFrameWriter.write(sessionId: sid, meta: meta, aux: auxSample, image: img.buffer, planes: planes,
                                                 rawIntrinsics: K, kResolution: kRes, maxEdge: edge, quality: q)
                await MainActor.run {
                    self.inFlight -= 1
                    if let out {
                        self.bytes += Int64(out.jpegBytes + out.depthBytes)
                        self.storedMB = Double(self.bytes) / 1_048_576
                        self.keyframesTotal += 1
                        if averagedN >= 2 { self.keyframesDenoised += 1 }
                        if useStill { self.highResCount += 1 }
                        self.confSum += Double(mc)
                        self.qualityMeanConf = self.confSum / Double(self.keyframesTotal)
                        self.qualityDenoisedPct = Double(self.keyframesDenoised) / Double(self.keyframesTotal) * 100
                    } else {
                        self.stubs.removeAll { $0.index == idx }
                        self.keyframeCount = self.stubs.count
                        self.notice = "프레임 저장 실패 — 저장 공간을 확인하세요"
                    }
                    if idx % 20 == 0 { self.persist() }
                }
            }
        }

        if wantStill {
            // 고화질 스틸 요청. 미지원/실패/2초 지연이면 조용히 일반 캡처로 대체한다.
            let once = Once()
            ar.captureHighResFrame { still in
                guard !once.done else { return }
                once.done = true; go(still)
            }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !once.done else { return }
                once.done = true; go(nil)
            }
        } else {
            go(nil)
        }
    }

    private func persist() {
        guard var r = record else { return }
        r.frameIndices = stubs.map(\.index).sorted()
        r.segmentCount = gate.segmentIndex + 1
        record = r
        SpaceScanStore.save(r)
    }

    private static func mappingName(_ s: ARFrame.WorldMappingStatus) -> String {
        switch s {
        case .notAvailable: return "notAvailable"
        case .limited: return "limited"
        case .extending: return "extending"
        case .mapped: return "mapped"
        @unknown default: return "unknown"
        }
    }
}
