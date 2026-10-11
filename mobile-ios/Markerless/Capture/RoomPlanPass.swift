// RoomPlanPass.swift — (선택) RoomPlan 구조 스캔 패스. RoomPlan이 없거나 기기가 지원하지 않으면 UI에서 버튼을 숨긴다.
//
// RoomCaptureSession은 자체 ARSession으로 카메라를 쓴다 → RGB-D 키프레임 패스와 '동시에' 돌리지 않는다.
// 호출 쪽(SpaceScanController.beginStructurePass)이 먼저 ScanSessionController.pause()로 카메라를 비우고,
// 이 패스가 끝나면 endStructurePass가 카메라를 되돌려 준다 (순차 패스). 따라서 두 패스의 session 좌표계는 서로 다르다:
// structure.json의 frame="roomplan-session"이 그 표시이며, 서버가 RGB-D 점군과 정합(또는 REVIEW_REQUIRED 처리)해야 한다.
//
// space-structure.json (SpaceStructureJSON) — 단위 m, transform16 = column-major 4x4(모델→RoomPlan 세션), 오른손 Y-up
//   {
//     "frame": "roomplan-session",
//     "walls":    [{"id": uuid, "transform16": [16], "widthM": x, "heightM": y, "thicknessM": z?}],   // dimensions = (폭, 높이, 두께)
//     "doors":    [ …같은 형식… ],  "windows": [ … ],  "openings": [ … ],
//     "objects":  [{"id": uuid, "category": "table", "transform16": [16], "dimensionsM": [x,y,z]}],
//     "floorY": 최저 벽 하단 y?, "ceilingY": 최고 벽 상단 y?   // 벽 중심 y ∓ 높이/2 에서 유도한 근사
//   }

import SwiftUI
import simd
#if canImport(RoomPlan)
import RoomPlan
#endif

#if canImport(RoomPlan)

enum RoomPlanConverter {
    private static func m16(_ m: simd_float4x4) -> [Float] {
        let c = m.columns
        return [c.0.x, c.0.y, c.0.z, c.0.w, c.1.x, c.1.y, c.1.z, c.1.w, c.2.x, c.2.y, c.2.z, c.2.w, c.3.x, c.3.y, c.3.z, c.3.w]
    }

    private static func surface(_ s: CapturedRoom.Surface) -> SpaceStructureJSON.Surface {
        SpaceStructureJSON.Surface(id: s.identifier.uuidString, transform16: m16(s.transform),
                                   widthM: s.dimensions.x, heightM: s.dimensions.y,
                                   thicknessM: s.dimensions.z > 0.001 ? s.dimensions.z : nil)
    }

    static func convert(_ room: CapturedRoom) -> SpaceStructureJSON {
        var out = SpaceStructureJSON()
        out.walls = room.walls.map(surface)
        out.doors = room.doors.map(surface)
        out.windows = room.windows.map(surface)
        out.openings = room.openings.map(surface)
        out.objects = room.objects.map {
            SpaceStructureJSON.Obj(id: $0.identifier.uuidString, category: String(describing: $0.category),
                                   transform16: m16($0.transform), dimensionsM: [$0.dimensions.x, $0.dimensions.y, $0.dimensions.z])
        }
        if !room.walls.isEmpty {
            out.floorY = room.walls.map { $0.transform.columns.3.y - $0.dimensions.y / 2 }.min()
            out.ceilingY = room.walls.map { $0.transform.columns.3.y + $0.dimensions.y / 2 }.max()
        }
        return out
    }
}

private struct RoomCaptureHost: UIViewRepresentable {
    let model: RoomPassModel

    func makeUIView(context: Context) -> RoomCaptureView {
        let v = RoomCaptureView(frame: .zero)
        v.delegate = context.coordinator
        model.view = v
        v.captureSession.run(configuration: RoomCaptureSession.Configuration())
        return v
    }

    func updateUIView(_ uiView: RoomCaptureView, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    @objc(MarkerlessRoomPassCoordinator)
    final class Coordinator: NSObject, RoomCaptureViewDelegate {
        let model: RoomPassModel
        init(model: RoomPassModel) { self.model = model; super.init() }
        required init?(coder: NSCoder) { fatalError("not coded") }  // RoomCaptureViewDelegate가 NSCoding을 요구한다
        func encode(with coder: NSCoder) {}

        func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool { true }

        func captureView(didPresent processedResult: CapturedRoom, error: Error?) {
            Task { @MainActor in
                if let error { self.model.error = error.localizedDescription }
                self.model.result = RoomPlanConverter.convert(processedResult)
                self.model.finished = true
            }
        }
    }
}

@MainActor
final class RoomPassModel: ObservableObject {
    var view: RoomCaptureView?
    @Published var finished = false
    @Published var result: SpaceStructureJSON?
    @Published var error: String?
    @Published var stopping = false
    func stop() { stopping = true; view?.captureSession.stop() }
    func cancel() { view?.captureSession.stop() }
}

/// 전체 화면 시트로 띄우는 구조 스캔. onDone(nil)=취소.
struct RoomPlanPassView: View {
    var onDone: (SpaceStructureJSON?) -> Void
    @StateObject private var model = RoomPassModel()

    var body: some View {
        ZStack(alignment: .bottom) {
            RoomCaptureHost(model: model).ignoresSafeArea()
            VStack(spacing: 8) {
                Text(model.stopping && !model.finished ? "구조 계산 중…" : "벽·문·창을 따라 천천히 한 바퀴 돌며 비추세요")
                    .font(.footnote.weight(.semibold)).padding(.horizontal, 12).padding(.vertical, 6)
                    .background(.black.opacity(0.65), in: Capsule()).foregroundStyle(.white)
                if let e = model.error { Text(e).font(.caption).foregroundStyle(.orange) }
                HStack(spacing: 28) {
                    Button { model.cancel(); onDone(nil) } label: { DockIcon(symbol: "xmark.circle", caption: "취소") }
                    if model.finished {
                        Button { onDone(model.result) } label: { DockIcon(symbol: "checkmark.circle.fill", caption: "저장", tint: .green, active: true) }
                    } else {
                        Button { model.stop() } label: { DockIcon(symbol: "stop.circle", caption: "구조 완료") }.disabled(model.stopping)
                    }
                }
            }
            .padding(.bottom, 22)
        }
        .background(Color.black)
    }
}

#else

/// RoomPlan이 없는 SDK 빌드: 호출부가 SpaceScanDevice.roomPlanSupported로 버튼을 숨긴다.
struct RoomPlanPassView: View {
    var onDone: (SpaceStructureJSON?) -> Void
    var body: some View { Text("RoomPlan 미지원").onAppear { onDone(nil) } }
}

#endif
