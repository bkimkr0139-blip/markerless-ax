// DTOs.swift — 백엔드 API JSON 스키마 (docs/API.md, backend/app/schemas.py와 동기화)
// 좌표 규약: docs/COORDINATE.md — Pose canonical JSON {positionM, rpyDeg}

import Foundation

// MARK: - Pose (COORDINATE.md 1절 canonical JSON)

struct Vec3JSON: Codable, Hashable {
    var x: Double
    var y: Double
    var z: Double
}

struct RPYJSON: Codable, Hashable {
    var roll: Double
    var pitch: Double
    var yaw: Double

    static let zero = RPYJSON(roll: 0, pitch: 0, yaw: 0)
}

struct QuatJSON: Codable, Hashable {
    var x: Double
    var y: Double
    var z: Double
    var w: Double
}

struct PoseJSON: Codable, Hashable {
    var positionM: Vec3JSON
    var rpyDeg: RPYJSON?
    /// 서버가 사물 앵커 정합 결과에 함께 주는 회전 (축 모호성 없음 — 오버레이는 있으면 이것을 쓴다)
    var quat: QuatJSON?

    init(positionM: Vec3JSON, rpyDeg: RPYJSON? = .zero, quat: QuatJSON? = nil) {
        self.positionM = positionM
        self.rpyDeg = rpyDeg
        self.quat = quat
    }
}

// MARK: - 공간/자산

struct SpaceIn: Codable {
    var spaceId: String
    var name: String?
    var level: String?
}

/// GET /api/v1/sync/state — 관리자가 공간·자산을 하나라도 바꾸면 revision이 달라진다
struct SyncStateJSON: Codable {
    var revision: String
    var spaces: Int
    var assets: Int
}

struct SpaceOut: Codable, Identifiable {
    var id: Int
    var projectId: String
    var code: String
    var name: String?
    var level: String?
    var hasWorldMap: Bool
    var worldmapSha256: String?

    var spaceId: String { code }
}

struct ToleranceJSON: Codable {
    var positionMm: Double
    var zToleranceMm: Double?
    var rotationDeg: Double?
}

struct AssetOut: Codable, Identifiable {
    var assetId: String
    var projectId: String
    var spaceId: Int
    var bimRevision: String
    var family: String?
    var type: String?
    var designPose: PoseJSON
    var tolerance: ToleranceJSON?
    var aabb: AABBJSON?
    var cadUri: String?
    var inspectView: InspectViewJSON?  // 검수 때 그릴 레이어 (통합관리자 설정, 없으면 기본값)
    var name: String?  // 표시 이름 (통합관리자가 수정 — 비면 assetId)
    var status: String?  // ACTIVE | ARCHIVED
    var scannedAt: String?  // 스캔(등록) 시각 ISO8601
    var geo: GeoJSON?  // 스캔 위치 (GPS 없으면 nil)
    var deviceId: String?

    var id: String { assetId }
}

/// 검수 표시 설정 — 백오피스(자산 상세 → 검수 표시 설정)에서 자산별로 정한다.
/// 기본: 외곽선·외곽 박스·치수·사진 실루엣. 표면·점군은 필요할 때만 켠다.
struct InspectViewJSON: Codable, Hashable {
    var surface = false
    var points = false
    var outline = true
    var box = true
    var dimensions = true
    var silhouette = true
    static let standard = InspectViewJSON()
    var summary: String {
        [(surface, "표면"), (points, "점군"), (outline, "외곽선"), (box, "박스"), (dimensions, "치수"), (silhouette, "실루엣")]
            .filter { $0.0 }.map { $0.1 }.joined(separator: "·")
    }
}

struct GeoJSON: Codable, Hashable {
    var lat: Double
    var lon: Double
    var altM: Double?
    var accuracyM: Double?
}

struct AABBJSON: Codable {
    struct MinMax: Codable, Hashable {
        var minM: [Double]?
        var maxM: [Double]?
    }
    var minM: [Double]
    var maxM: [Double]
}

// MARK: - 측정 (개발지시서 13절)

struct CloudPayload: Codable {
    var frame: String = "space"
    var count: Int
    var base64: String
}

// MARK: - 사물 분리 (POI 기반, POST /api/v1/scan/segment)

// MARK: - 사물 대 사물 검수 상세 (MeasurementOut.match)

struct MatchJSON: Codable {
    struct Rank: Codable, Hashable {
        var assetId: String
        var score: Double?
        var inlier: Double?
        var sizePenalty: Double?
        var combined: Double?
        var rmseMm: Double?
    }
    struct Alignment: Codable {
        var inlierRatio: Double
        var rmseM: Double
        var score: Double
        var modelCoverage: Double
        var yawDeg: Double
        var tauM: Double
        var bestInit: String?
        var segmented: Bool?
    }
    struct Seg: Codable {
        var segmented: Bool
        var nIn: Int
        var nRemoved: Int
        var quality: String
        var warnings: [String]?
    }
    struct SizeRatio: Codable {
        var footprint: Double
        var height: Double
    }
    var matchedBy: String            // auto | pinned
    var assetId: String
    var ranking: [Rank]
    var segmentation: Seg
    var alignment: Alignment
    var modelSizeM: [Double]
    var observedSizeM: [Double]
    var sizeRatio: SizeRatio
    var observedOutline: OutlineJSON?  // 관측된 사물의 외곽선 (세션 좌표)
}

// MARK: - 자산 찾기 (POST /api/v1/match) — 후보 순위만, 판정·저장 없음

struct MatchRequest: Codable {
    var spaceId: Int
    var cloud: CloudPayload
    var hintCenter: Vec3JSON?
    var worldMapRestored: Bool = false
}

struct MatchCandidate: Codable, Identifiable {
    var assetId: String
    var similarity: Double        // 0~100 (일치율 × 크기 일치 × 정합 점수)
    var accepted: Bool            // 크기·일치율·점수 기준 통과
    var inlier: Double
    var rmseMm: Double
    var score: Double
    var yawDeg: Double
    var modelSizeM: [Double]
    var sizeRatio: MatchJSON.SizeRatio
    var id: String { assetId }
}

struct MatchResultJSON: Codable {
    var best: String?             // 기준을 통과한 1위 자산 (없으면 nil)
    var accepted: Bool
    var candidates: [MatchCandidate]
    var segmentation: MatchJSON.Seg
    var observedSizeM: [Double]
    var observedOutline: OutlineJSON?
}

// MARK: - 사진 실루엣 카드 (GET /api/v1/assets/{id}/photos)

struct PhotoMetaJSON: Codable {
    var side: String
    var center: [Double]
    var right: [Double]
    var up: [Double]
    var forward: [Double]
    var widthM: Double
    var heightM: Double
    var azimuthDeg: Double?
}

struct PhotoListJSON: Codable {
    var photos: [PhotoMetaJSON]
}

struct SegmentRequest: Codable {
    var cloud: CloudPayload
    var poi: Vec3JSON
    var maxRadius: Double
    var upAxis: Int = 1  // ARKit Y-up
    var refine: Bool = false  // true: 사물 점 스무딩 + 바깥쪽 법선 (VIO 점용)
}

/// 등록 품질 평가 (서버): 방향별 면 확보·방위 커버리지·밀도·노이즈·상자형 정도
struct AssessmentJSON: Codable {
    var score: Int                 // 0~100
    var grade: String              // good | fair | poor
    var points: Int
    var sides: [String: Int]       // front|right|back|left → 그 면 중앙부 점 수
    var sidesCovered: Int          // 확보된 면 수 (0~4)
    var sideCoveredFlags: [String: Bool]?
    var azimuthCoverage: Double
    var densityPerM2: Double
    var noiseMm: Double?
    var boxness: Double
    var topCovered: Bool
    var tips: [String]             // 보완 안내
}

/// 서버가 정제한 사물 점(스무딩)과 바깥쪽 법선 — float32 xyz * count
struct RefinedJSON: Codable {
    var count: Int
    var points: String
    var normals: String

    private static func floats(_ b64: String) -> [SIMD3<Float>] {
        guard let d = Data(base64Encoded: b64) else { return [] }
        let n = d.count / 12
        return d.withUnsafeBytes { raw in
            (0..<n).map { i in
                SIMD3<Float>(raw.loadUnaligned(fromByteOffset: i * 12, as: Float.self),
                             raw.loadUnaligned(fromByteOffset: i * 12 + 4, as: Float.self),
                             raw.loadUnaligned(fromByteOffset: i * 12 + 8, as: Float.self))
            }
        }
    }
    var pointArray: [SIMD3<Float>] { RefinedJSON.floats(points) }
    var normalArray: [SIMD3<Float>] { RefinedJSON.floats(normals) }
}

/// 사물 외곽선(와이어프레임) — 점 조각 대신 선으로 표시 (BIM/CAD 유사)
struct OutlineJSON: Codable {
    struct OBB: Codable {
        var center: [Double]
        var size: [Double]      // (u 길이, 높이, v 길이)
        var yawDeg: Double      // u축이 수평면 첫 축(x)에서 둘째 축(z)으로 돈 각
        var corners: [[Double]] // 8개, 인덱스 = sx*4 + sy*2 + sz
    }
    struct ViewOutline: Codable {
        var w: Double
        var h: Double
        var segments: [[Double]] // [x1,y1,x2,y2] — 그 방향에서 본 2D 선분 (미터, 사물 중심 기준)
    }
    var lines: [[Double]]       // [x1,y1,z1,x2,y2,z2] 높이별 윤곽선
    var obb: OBB
    var levels: Int
    var views: [String: ViewOutline]?  // front | right | back | left (관측 사물 외곽선에는 없음)
}

struct SegmentOutJSON: Codable {
    var count: Int
    var mask: String          // uint8 * N base64, 요청 점 순서
    var center: Vec3JSON      // 사물 AABB 중심
    var aabbMin: Vec3JSON
    var aabbMax: Vec3JSON
    var nIn: Int
    var nRemoved: Int
    var quality: String       // good | fair | poor
    var warnings: [String]
    var supportHeightM: Double?
    var connRadiusM: Double
    var outline: OutlineJSON?   // 분리된 사물의 외곽선 (세션 좌표)
    var assessment: AssessmentJSON?  // 등록 품질 평가
    var refined: RefinedJSON?        // refine 요청 시 정제 점·법선

    /// 요청 점 순서의 불리언 마스크
    func maskArray() -> [Bool] {
        guard let d = Data(base64Encoded: mask) else { return [] }
        return d.map { $0 != 0 }
    }

    /// 사물 크기 (cm): 가로×높이×깊이
    var sizeCm: (Double, Double, Double) {
        ((aabbMax.x - aabbMin.x) * 100, (aabbMax.y - aabbMin.y) * 100, (aabbMax.z - aabbMin.z) * 100)
    }
}

struct BulkDeleteOutJSON: Codable {
    var deleted: [String]
    var notFound: [String]
}

struct MeasurementRequest: Codable {
    var assetId: String? = nil           // 자동 매칭이면 생략
    var autoMatch: Bool? = nil           // true: 서버가 공간의 등록 자산 중 가장 닮은 것을 고른다
    var spaceId: Int? = nil              // autoMatch 대상 공간(숫자 ID)
    var deviceId: String
    var observationId: String?
    var alignment: String? = nil          // "object": 사물 앵커 정합 (월드맵 없이 yaw 탐색 + ICP)
    var hintCenter: Vec3JSON? = nil       // 세션 좌표의 사물 중심 추정 (조준점)
    var worldMapRestored: Bool? = nil     // true면 공간 프레임 기준 편차까지 판정
    var cloud: CloudPayload?
    var trackingNormal: Bool?
    var depthValidRatio: Double?
}

struct DeviationJSON: Codable, Hashable {
    var dxMm: Double
    var dyMm: Double
    var dzMm: Double
    var distMm: Double
    var rollDeg: Double
    var pitchDeg: Double
    var yawDeg: Double
    var maxRotDeg: Double
}

struct MarginsJSON: Codable {
    struct Axis: Codable {
        var certainlyIn: Bool?
        var certainlyOut: Bool?
    }
    var position: Axis?
    var z: Axis?
    var rotation: Axis?

    /// 월드맵 미복원 시 서버가 주는 형상 일치도 (위치 편차 대신 표시)
    struct ShapeFit: Codable {
        var inlierRatio: Double
        var rmseMm: Double
    }
    var shapeFit: ShapeFit?
}

struct ReasonJSON: Codable {
    var code: String
    var message: String

    init(from decoder: Decoder) throws {
        // 서버(backend/app/schemas.py ResultOut.reasons)는 list[str] — 객체 형태도 허용
        if let s = try? decoder.singleValueContainer().decode(String.self) {
            code = s; message = s
        } else {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            code = try c.decode(String.self, forKey: .code)
            message = try c.decode(String.self, forKey: .message)
        }
    }

    enum CodingKeys: String, CodingKey { case code, message }
}

struct ResultJSON: Codable {
    var resultId: String
    var observationId: String
    var assetId: String
    var decision: String       // PASS | FAIL | REVIEW | RETAKE
    var confidence: String     // HIGH | MEDIUM | LOW | UNKNOWN
    var deviation: DeviationJSON
    var margins: MarginsJSON?
    var reasons: [ReasonJSON]?
}

struct ObservationJSON: Codable {
    var observationId: String
    var assetId: String
    var deviceId: String?
    var bimRevision: String?
    var designPose: PoseJSON?
    var actualPose: PoseJSON?
    var deviation: DeviationJSON?
    var uncertainty: UncertaintyJSON?
    var confidence: String?
    var decision: String?
    var createdAt: String?
}

struct UncertaintyJSON: Codable {
    var positionMm: Double?
    var rotationDeg: Double?
}

struct MeasurementOut: Codable {
    var observation: ObservationJSON
    var result: ResultJSON
    var match: MatchJSON?
}

// MARK: - 판정 색상/문구 (뷰 공용)

extension ResultJSON {
    var decisionColorHex: String {
        switch decision {
        case "PASS": return "#16A34A"
        case "FAIL": return "#DC2626"
        case "REVIEW": return "#D97706"
        default: return "#CA8A04" // RETAKE
        }
    }

    var decisionKorean: String {
        switch decision {
        case "PASS": return "합격 (PASS)"
        case "FAIL": return "불합격 (FAIL)"
        case "REVIEW": return "사람 확인 (REVIEW)"
        default: return "재측정 (RETAKE)"
        }
    }
}

// MARK: - 공간 전체 스캔 (docs/INTERFACES.md §4·§5) — JSON은 camelCase

/// 서버가 정확한 스키마를 확정하지 않은 필드(quality·coverage 등)를 어떤 형태로 와도 받아 두는 값
enum FlexValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: FlexValue]), array([FlexValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let d = try? c.decode(Double.self) { self = .number(d) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([FlexValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: FlexValue].self) { self = .object(o) }
        else { self = .null }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let d): try c.encode(d)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    var number: Double? { if case .number(let d) = self { return d }; return nil }

    /// 화면 표시용 짧은 문자열 (객체면 overall/score/grade 같은 대표 키를 찾는다)
    var display: String {
        switch self {
        case .string(let s): return s
        case .number(let d): return d <= 1.0 && d >= 0 ? "\(Int((d * 100).rounded()))%" : String(format: "%.1f", d)
        case .bool(let b): return b ? "예" : "아니오"
        case .object(let o):
            for k in ["overall", "score", "grade", "label", "value"] { if let v = o[k] { return v.display } }
            return "{…}"
        case .array(let a): return "\(a.count)개"
        case .null: return "-"
        }
    }
}

struct DeviceProfileJSON: Codable {
    var model: String  // 하드웨어 식별자 (예: iPhone14,2)
    var hasLiDAR: Bool
    var osVersion: String
    var appBuild: String?
}

struct ScanSessionCreateJSON: Codable {
    var mode: String  // SPACE | FACILITY | OBJECT
    var deviceId: String
    var deviceProfile: DeviceProfileJSON
    var capabilities: [String: Bool]
    var worldMapRevision: String?
    var relocalized: Bool
}

struct ScanSessionOutJSON: Codable {
    var scanSessionId: String
    var status: String?
}

/// INTERFACES.md §4 프레임 메타
struct FrameMetaJSON: Codable {
    var frameIndex: Int
    var segmentIndex: Int
    var timestampNs: Int64
    var cameraTransform: [Float]  // 16, column-major, camera→session
    var intrinsics: [Float]  // 9, column-major 3x3, RGB 이미지 픽셀 기준
    var imageWidth: Int
    var imageHeight: Int
    var depthWidth: Int
    var depthHeight: Int
    var trackingState: String  // normal | limited | notAvailable
    var exposureDurationS: Double?
    var iso: Double?
    var whiteBalanceK: Double?
    var lightEstimateLux: Double?
    var motionBlurScore: Double?
    var worldMappingStatus: String?
    // ── 품질 메타(선택 키 — 서버는 meta JSON을 그대로 저장, 계약 §4 호환)
    var sharpness: Double?  // 320px 휘도 Laplacian 분산 (클수록 선명)
    var exposureOffset: Double?  // ARCamera.exposureOffset (EV)
    var ambientIntensity: Double?  // ARLightEstimate.ambientIntensity (lumen)
    var ambientColorTemperature: Double?  // ARLightEstimate.ambientColorTemperature (K)
    var trackingReason: String?  // limited일 때 사유 (excessiveMotion | insufficientFeatures | initializing | relocalizing)
    var depthFramesAveraged: Int?  // 정지 시 시간 평균에 쓴 깊이 프레임 수 (없으면 단일 프레임)
    var highResStill: Bool?  // true면 RGB가 고화질 스틸 (imageWidth/Height/intrinsics는 그 격자 기준)
}

/// 보조 센서(절대 진실 아님): 서버로 보내지 않고 디스크 기록에만 둔다 (세션 단위 요약은 capabilities/로그)
struct AuxSampleJSON: Codable {
    var relAltitudeM: Double?
    var pressureKPa: Double?
    var headingDeg: Double?
    var headingAccuracyDeg: Double?
    var lat: Double?
    var lon: Double?
    var gpsAccuracyM: Double?
}

/// 디스크 <idx>.json — 서버 메타 + 보조 센서
struct FrameDiskRecord: Codable {
    var meta: FrameMetaJSON
    var aux: AuxSampleJSON?
}

struct FramePartInfoJSON: Codable {
    var bytes: Int
    var sha256: String
}

struct FrameInitUploadJSON: Codable {
    var meta: FrameMetaJSON
    var parts: [String: FramePartInfoJSON]  // rgb | depth | conf
    var idempotencyKey: String
}

struct FrameInitUploadOutJSON: Codable {
    var state: String  // DONE | PENDING
    var received: [String: Int]?
}

struct FrameCompleteJSON: Codable { var frameIndex: Int }
struct ScanFinalizeJSON: Codable { var endedReason: String }
struct ScanReconstructJSON: Codable {
    var voxelM: Double?
    var textureMaxPx: Int?
    var force: Bool?
}

struct ScanStatusJSON: Codable {
    var status: String
    var stage: String?
    var progress: Double?
    var message: String?
    var quality: FlexValue?
    var coverage: FlexValue?
    var revisionId: String?
}

/// GET /spaces/{id}/coverage — 필드가 없어도 디코드된다
struct SpaceCoverageOutJSON: Codable {
    var overall: Double?
    var floor: Double?
    var ceiling: Double?
    var unobservedAreaM2: Double?
    var quality: FlexValue?
}

/// space-structure.json (RoomPlan 변환 결과) — 단위 m, transform16은 column-major, RoomPlan 세션 프레임 기준
struct SpaceStructureJSON: Codable {
    struct Surface: Codable {
        var id: String
        var transform16: [Float]
        var widthM: Float
        var heightM: Float
        var thicknessM: Float?
    }
    struct Obj: Codable {
        var id: String
        var category: String
        var transform16: [Float]
        var dimensionsM: [Float]  // x,y,z
    }
    var frame: String = "roomplan-session"  // 별도 RGB-D 패스의 session 프레임과 다르다 → 서버가 정합해야 함
    var walls: [Surface] = []
    var doors: [Surface] = []
    var windows: [Surface] = []
    var openings: [Surface] = []
    var objects: [Obj] = []
    var floorY: Float?
    var ceilingY: Float?
}
