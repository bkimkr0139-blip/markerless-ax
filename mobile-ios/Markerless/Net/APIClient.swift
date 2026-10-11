// APIClient.swift — 백엔드 REST 클라이언트 (FastAPI, docs/API.md)
// 외부 의존성 없음: URLSession + multipart 수동 조립

import Foundation

struct APIError: LocalizedError {
    var status: Int
    var message: String
    var errorDescription: String? { "[\(status)] \(message)" }
}

struct APIClient {
    var baseURL: URL?
    var apiKey: String = ""
    var timeout: TimeInterval = 60  // ICP 서버 연산 대기 (지시서 9절 성능 목표)

    /// 전용 세션: 연결이 복구될 때까지 대기(waitsForConnectivity), 전체 시간 상한 180초.
    /// 폰 네트워크(Wi-Fi↔LTE 전환 등)가 잠깐 끊겨도 요청이 바로 실패하지 않는다.
    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.waitsForConnectivity = true
        c.timeoutIntervalForRequest = 30
        c.timeoutIntervalForResource = 180
        c.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: c)
    }()

    // MARK: - Core

    private func request(_ path: String, method: String = "GET", body: Data? = nil,
                         contentType: String = "application/json", query: [URLQueryItem]? = nil) throws -> URLRequest {
        guard let base = baseURL else { throw APIError(status: 0, message: "서버 주소가 설정되지 않았습니다") }
        var url = base.appendingPathComponent(path)
        if let query, var comps = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            comps.queryItems = query  // '?'가 경로로 이스케이프되지 않게 URLComponents로 붙인다
            url = comps.url ?? url
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = timeout
        if !apiKey.isEmpty { req.setValue(apiKey, forHTTPHeaderField: "X-API-Key") }
        if let body {
            req.httpBody = body
            req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    /// 일시적 오류만 재시도: 네트워크 끊김/타임아웃, 터널 구간 502/503/504 (최대 3회, 0.8s·1.6s 대기)
    private static let retryableURLErrors: Set<URLError.Code> = [
        .timedOut, .networkConnectionLost, .cannotConnectToHost, .notConnectedToInternet,
        .dnsLookupFailed, .cannotFindHost,
    ]

    private func send(_ req: URLRequest) async throws -> Data {
        let label = "\(req.httpMethod ?? "GET") \(req.url?.path ?? "?")"
        let maxAttempts = 3
        var lastError: Error = APIError(status: 0, message: "알 수 없는 오류")
        for attempt in 1...maxAttempts {
            let t0 = Date()
            do {
                let (data, resp) = try await APIClient.session.data(for: req)
                let ms = Int(Date().timeIntervalSince(t0) * 1000)
                guard let http = resp as? HTTPURLResponse else {
                    throw APIError(status: 0, message: "응답 없음")
                }
                if [502, 503, 504].contains(http.statusCode), attempt < maxAttempts {
                    DebugLog.error("api", "\(label) → \(http.statusCode) 재시도 \(attempt)/\(maxAttempts - 1)")
                    try await Task.sleep(nanoseconds: UInt64(0.8 * Double(attempt) * 1_000_000_000))
                    continue
                }
                guard (200..<300).contains(http.statusCode) else {
                    var msg = (try? JSONDecoder().decode(ErrorDetail.self, from: data))?.detail
                        ?? String(data: data.prefix(300), encoding: .utf8) ?? "알 수 없는 오류"
                    if http.statusCode == 401 { msg = "API 키가 없거나 올바르지 않습니다 — 홈 탭의 'API 키'를 확인하세요" }
                    DebugLog.error("api", "\(label) → \(http.statusCode) (\(ms)ms)", extra: ["body": String(msg.prefix(600))])
                    throw APIError(status: http.statusCode, message: msg)
                }
                DebugLog.log("api", "\(label) → \(http.statusCode) (\(ms)ms, \(data.count)B\(attempt > 1 ? ", 시도 \(attempt)" : ""))")
                return data
            } catch let e as URLError where APIClient.retryableURLErrors.contains(e.code) {
                lastError = e
                DebugLog.error("api", "\(label) 네트워크 오류(\(e.code.rawValue)): \(e.localizedDescription) — 시도 \(attempt)/\(maxAttempts)")
                if attempt < maxAttempts {
                    try await Task.sleep(nanoseconds: UInt64(0.8 * Double(attempt) * 1_000_000_000))
                }
            } catch {
                if !(error is APIError) { DebugLog.error("api", "\(label) 실패: \(error.localizedDescription)") }
                throw error
            }
        }
        throw lastError
    }

    private struct ErrorDetail: Decodable {
        let detail: String
    }

    // MARK: - Health

    func health() async throws -> String {
        let data = try await send(try request("/healthz"))
        return String(data: data, encoding: .utf8) ?? "?"
    }

    // MARK: - 공간 (Spaces)

    @discardableResult
    func createSpace(projectId: String, spaceId: String, name: String?) async throws -> SpaceOut {
        let body = try JSONEncoder().encode(SpaceIn(spaceId: spaceId, name: name, level: nil))
        let req = try request("api/v1/projects/\(projectId)/spaces", method: "POST", body: body)
        return try JSONDecoder().decode(SpaceOut.self, from: try await send(req))
    }

    func syncState() async throws -> SyncStateJSON {
        try JSONDecoder().decode(SyncStateJSON.self, from: try await send(try request("api/v1/sync/state")))
    }

    func spaces(projectId: String) async throws -> [SpaceOut] {
        let req = try request("api/v1/projects/\(projectId)/spaces")
        return try JSONDecoder().decode([SpaceOut].self, from: try await send(req))
    }

    // MARK: - WorldMap (COORDINATE.md 4절 — T_space_session 해결)

    func uploadWorldMap(spaceNumericId: Int, data: Data) async throws {
        var req = try request("api/v1/spaces/\(spaceNumericId)/worldmap", method: "PUT", body: data,
                              contentType: "application/octet-stream")
        req.setValue(sha256Hex(data), forHTTPHeaderField: "X-Sha256")
        _ = try await send(req)
    }

    func downloadWorldMap(spaceNumericId: Int) async throws -> Data {
        var req = try request("api/v1/spaces/\(spaceNumericId)/worldmap")
        req.timeoutInterval = 5  // 없거나 느리면 빨리 포기 (AR 시작을 막지 않는다)
        return try await send(req)
    }

    // MARK: - 자산 (스캔 자가 생성)

    struct ScanAssetMeta: Codable {
        var assetId: String
        var projectId: String
        var spaceId: String
        var bimRevision: String
        var family: String
        var type: String
        var designPose: PoseJSON
        var tolerance: ToleranceJSON?
        var notes: String?
        var deviceId: String? = nil  // 등록한 단말 — 백오피스 단말기 화면·자산 목록에 표시
        var scannedAt: String? = nil  // 스캔 시각 (ISO8601, 단말 시계)
        var geo: GeoJSON? = nil  // 스캔 위치 (GPS)
    }

    func uploadScanAsset(meta: ScanAssetMeta, glb: Data) async throws -> AssetOut {
        let boundary = "Boundary-\(UUID().uuidString)"
        var body = Data()
        func append(_ s: String) { body.append(s.data(using: .utf8)!) }

        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n")
        body.append(try JSONEncoder().encode(meta))
        append("\r\n--\(boundary)\r\nContent-Disposition: form-data; name=\"model\"; filename=\"scan.glb\"\r\n")
        append("Content-Type: model/gltf-binary\r\n\r\n")
        body.append(glb)
        append("\r\n--\(boundary)--\r\n")

        var req = try request("api/v1/assets/scan", method: "POST", body: body,
                              contentType: "multipart/form-data; boundary=\(boundary)")
        req.timeoutInterval = 120  // 서버가 메시 파싱+샘플링한다
        return try JSONDecoder().decode(AssetOut.self, from: try await send(req))
    }

    func asset(assetId: String) async throws -> AssetOut {
        try JSONDecoder().decode(AssetOut.self, from: try await send(try request("api/v1/assets/\(assetId)")))
    }

    func assetsInSpace(spaceNumericId: Int) async throws -> [AssetOut] {
        try JSONDecoder().decode([AssetOut].self, from: try await send(try request("api/v1/spaces/\(spaceNumericId)/assets")))
    }

    // MARK: - 사물 분리 / 자산 삭제

    func segment(points: [SIMD3<Float>], poi: SIMD3<Float>, maxRadius: Float, refine: Bool = false) async throws -> SegmentOutJSON {
        let flat = points.flatMap { [$0.x, $0.y, $0.z] }
        let data = flat.withUnsafeBufferPointer { Data(buffer: $0) }
        let body = SegmentRequest(
            cloud: CloudPayload(count: points.count, base64: data.base64EncodedString()),
            poi: Vec3JSON(x: Double(poi.x), y: Double(poi.y), z: Double(poi.z)),
            maxRadius: Double(maxRadius), refine: refine)
        var req = try request("api/v1/scan/segment", method: "POST", body: try JSONEncoder().encode(body))
        req.timeoutInterval = 60
        return try JSONDecoder().decode(SegmentOutJSON.self, from: try await send(req))
    }

    /// 자산 외곽선(자산 로컬 프레임) — 검수 때 사물 위에 선으로 겹쳐 그린다
    func assetOutline(assetId: String) async throws -> OutlineJSON {
        try JSONDecoder().decode(OutlineJSON.self, from: try await send(try request("api/v1/assets/\(assetId)/outline")))
    }

    // MARK: - 사진 실루엣 카드

    func uploadPhotoCard(assetId: String, card: PhotoCard) async throws {
        let boundary = "Boundary-\(UUID().uuidString)"
        var body = Data()
        func append(_ s: String) { body.append(s.data(using: .utf8)!) }
        func v3(_ v: SIMD3<Float>) -> [Double] { [Double(v.x), Double(v.y), Double(v.z)] }
        let meta: [String: Any] = [
            "center": v3(card.center), "right": v3(card.right), "up": v3(card.up), "forward": v3(card.forward),
            "widthM": Double(card.widthM), "heightM": Double(card.heightM), "azimuthDeg": Double(card.azimuthDeg),
        ]
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n")
        body.append(try JSONSerialization.data(withJSONObject: meta))
        append("\r\n--\(boundary)\r\nContent-Disposition: form-data; name=\"image\"; filename=\"\(card.side).png\"\r\n")
        append("Content-Type: image/png\r\n\r\n")
        body.append(card.png)
        append("\r\n--\(boundary)--\r\n")
        var req = try request("api/v1/assets/\(assetId)/photos/\(card.side)", method: "PUT", body: body,
                              contentType: "multipart/form-data; boundary=\(boundary)")
        req.timeoutInterval = 60
        _ = try await send(req)
    }

    func assetPhotos(assetId: String) async throws -> PhotoListJSON {
        try JSONDecoder().decode(PhotoListJSON.self, from: try await send(try request("api/v1/assets/\(assetId)/photos")))
    }

    func assetPhotoPNG(assetId: String, side: String) async throws -> Data {
        try await send(try request("api/v1/assets/\(assetId)/photos/\(side).png"))
    }

    /// 자산 찾기: 비춘 사물과 닮은 등록 자산 후보 (판정·저장 없음)
    func match(_ reqBody: MatchRequest) async throws -> MatchResultJSON {
        var req = try request("api/v1/match", method: "POST", body: try JSONEncoder().encode(reqBody))
        req.timeoutInterval = 120
        return try JSONDecoder().decode(MatchResultJSON.self, from: try await send(req))
    }

    func deleteAsset(assetId: String) async throws {
        _ = try await send(try request("api/v1/assets/\(assetId)", method: "DELETE"))
    }

    func bulkDeleteAssets(ids: [String]) async throws -> BulkDeleteOutJSON {
        struct Body: Codable { var assetIds: [String] }
        let req = try request("api/v1/assets/bulk-delete", method: "POST",
                              body: try JSONEncoder().encode(Body(assetIds: ids)))
        return try JSONDecoder().decode(BulkDeleteOutJSON.self, from: try await send(req))
    }

    func assetModel(assetId: String) async throws -> Data {
        try await send(try request("api/v1/assets/\(assetId)/model.glb"))
    }

    func setTolerance(assetId: String, tol: ToleranceJSON) async throws {
        let body = try JSONEncoder().encode(tol)
        _ = try await send(try request("api/v1/assets/\(assetId)/tolerance", method: "PUT", body: body))
    }

    // MARK: - 측정 (개발지시서 13절)

    func measure(_ reqBody: MeasurementRequest) async throws -> MeasurementOut {
        let body = try JSONEncoder().encode(reqBody)
        var req = try request("api/v1/measurements", method: "POST", body: body)
        req.timeoutInterval = 120
        return try JSONDecoder().decode(MeasurementOut.self, from: try await send(req))
    }

    // MARK: - 공간 전체 스캔 (docs/INTERFACES.md §5) — 공간은 기존 API처럼 numericId 경로

    func createScanSession(spaceNumericId: Int, _ body: ScanSessionCreateJSON) async throws -> ScanSessionOutJSON {
        let req = try request("api/v1/spaces/\(spaceNumericId)/scan-sessions", method: "POST", body: try JSONEncoder().encode(body))
        return try JSONDecoder().decode(ScanSessionOutJSON.self, from: try await send(req))
    }

    func initFrameUpload(sessionId: String, _ body: FrameInitUploadJSON) async throws -> FrameInitUploadOutJSON {
        let req = try request("api/v1/scan-sessions/\(sessionId)/frames/init-upload", method: "POST", body: try JSONEncoder().encode(body))
        return try JSONDecoder().decode(FrameInitUploadOutJSON.self, from: try await send(req))
    }

    /// 청크 하나를 offset 위치에 이어 쓴다 (재개 가능). X-Sha256 = 파트 전체 해시 (마지막 청크에서 서버가 검증)
    func putFramePart(sessionId: String, frameIndex: Int, part: String, offset: Int, chunk: Data, partSha256: String) async throws {
        var req = try request("api/v1/scan-sessions/\(sessionId)/frames/\(frameIndex)/parts/\(part)", method: "PUT", body: chunk,
                              contentType: "application/octet-stream", query: [URLQueryItem(name: "offset", value: "\(offset)")])
        req.setValue(partSha256, forHTTPHeaderField: "X-Sha256")
        req.timeoutInterval = 60
        _ = try await send(req)
    }

    func completeFrameUpload(sessionId: String, frameIndex: Int) async throws {
        let req = try request("api/v1/scan-sessions/\(sessionId)/frames/complete-upload", method: "POST",
                              body: try JSONEncoder().encode(FrameCompleteJSON(frameIndex: frameIndex)))
        _ = try await send(req)
    }

    func uploadStructure(sessionId: String, _ s: SpaceStructureJSON) async throws {
        let req = try request("api/v1/scan-sessions/\(sessionId)/structure", method: "POST", body: try JSONEncoder().encode(s))
        _ = try await send(req)
    }

    func finalizeScanSession(sessionId: String, endedReason: String) async throws {
        let req = try request("api/v1/scan-sessions/\(sessionId)/finalize", method: "POST",
                              body: try JSONEncoder().encode(ScanFinalizeJSON(endedReason: endedReason)))
        _ = try await send(req)
    }

    func reconstructScanSession(sessionId: String, _ opts: ScanReconstructJSON = ScanReconstructJSON()) async throws {
        let req = try request("api/v1/scan-sessions/\(sessionId)/reconstruct", method: "POST", body: try JSONEncoder().encode(opts))
        _ = try await send(req)
    }

    func scanSessionStatus(sessionId: String) async throws -> ScanStatusJSON {
        try JSONDecoder().decode(ScanStatusJSON.self, from: try await send(try request("api/v1/scan-sessions/\(sessionId)/status")))
    }

    /// 404(아직 스캔 없음/미지원 서버)는 nil
    func spaceCoverage(spaceNumericId: Int) async throws -> SpaceCoverageOutJSON? {
        do {
            return try JSONDecoder().decode(SpaceCoverageOutJSON.self, from: try await send(try request("api/v1/spaces/\(spaceNumericId)/coverage")))
        } catch let e as APIError where e.status == 404 {
            return nil
        }
    }

    // MARK: - Utils

    private func sha256Hex(_ data: Data) -> String {
        // CryptoKit 없이 Crypto.sha256 (iOS 13+, 시스템 프레임워크)
        var hash = [UInt8](repeating: 0, count: 32)
        data.withUnsafeBytes { buf in
            _ = CC_SHA256(buf.baseAddress, CC_LONG(data.count), &hash)
        }
        return hash.map { String(format: "%02x", $0) }.joined()
    }
}

import CommonCrypto
