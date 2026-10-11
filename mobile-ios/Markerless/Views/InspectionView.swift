// InspectionView.swift — ③ 검수: 사물을 비추면 등록 자산을 자동으로 찾아 맞추고 판정
//
// 흐름:
//  1. (기본 켜짐) 공간 월드맵으로 세션 시작 → 재위치 인식이 성공하면 공간 프레임 복원 (12초 안에 안 되면 자동 해제)
//  2. 십자선을 사물 중심에 맞추고 [맞추기·검수]
//  3. 서버가 검수 점에서도 사물만 분리(등록과 동일) → 크기가 비슷한 등록 자산을 후보로 골라 정합(4자유도) →
//     가장 잘 맞는 자산을 자동 선택 (자산을 직접 고정할 수도 있음)
//  4. 맞춘 모델 선화(판정 색)와 관측된 사물 윤곽(노랑)을 겹쳐 그려 크기·각도 일치를 확인, 판정 표시

import ARKit
import Combine
import ImageIO
import RealityKit
import SwiftUI

struct InspectionView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var ar = ScanSessionController()

    /// 사용자가 고정한 자산 (nil이면 자동 매칭)
    @State private var pinnedId: String?
    // 오버레이용으로 불러온 자산 (고정했거나 자동 매칭된 것)
    @State private var asset: AssetOut?
    @State private var assetMesh: GLBMesh?
    @State private var assetOutline: OutlineJSON?
    @State private var lastObservedOutline: OutlineJSON?  // 판정 때 비춘 사물 윤곽(보인 면만)
    // 추적 보정: 맞춘 뒤에도 지금 보이는 깊이 점으로 포즈를 계속 다듬는다 (좌표계가 조금 움직여도 사물 위에 붙어 있게)
    @State private var refineModel: (pts: [SIMD3<Float>], nrm: [SIMD3<Float>])?
    @State private var refineTask: Task<Void, Never>?
    @AppStorage("hud.hidden") private var hudHidden = false  // 안내·상태 숨김 (스캔 화면과 공유)
    @State private var assetCards: [PhotoCardAsset] = []  // 등록 때 만든 방향별 흑백 실루엣 카드

    @State private var result: ResultJSON?
    @State private var match: MatchJSON?
    @State private var actualPose: PoseJSON?
    @State private var viewOverride: InspectViewJSON?  // 현장에서 칩으로 임시 변경한 표시 레이어 (nil = 관리자 설정 그대로)
    @State private var busy = false
    @State private var message: String?
    @State private var resultHidden = false

    // 자산 찾기(자동 매칭) → 후보 선택 → 검수의 2단계
    @State private var candidates: MatchResultJSON?
    @State private var lastCloud: (points: [SIMD3<Float>], hint: SIMD3<Float>)?  // 찾을 때 쓴 스캔 — 선택하면 이것으로 바로 검수
    @State private var aimHint: SIMD3<Float>?  // 조준점 (실시간)
    @State private var aimPoints = 0  // 조준 사물 주변 안정 점 수 (실시간)

    // 월드맵 (기본 켜짐: 재위치 인식이 되면 설계 위치 대비 편차까지 판정하고 매칭도 안정)
    @State private var worldMap: ARWorldMap?
    @State private var useWorldMap = true
    @State private var didAutoMap = false
    @State private var suppressToggle = false
    @State private var mapWatch: Task<Void, Never>?

    // MARK: - 단계 안내

    /// 조준 사물 주변에 필요한 최소 점 수 (서버가 사물을 분리·정합하기에 충분한 양)
    private var minAimPoints: Int { ar.minAimPoints }
    private var aimReady: Bool { aimHint != nil && aimPoints >= minAimPoints }

    private var steps: [GuideStep] {
        .make(["사물 비추기", "자산 찾기", "검수", "결과 확인"],
              done: [aimReady || pinnedId != nil || result != nil, pinnedId != nil || result != nil, result != nil, result != nil])
    }

    private var aimStatus: String {
        if aimHint == nil { return "조준점에서 표면을 못 찾음 — 사물을 십자선 가운데 두세요" }
        return "조준 사물 주변 \(ar.scanPointLabel) \(aimPoints)/\(minAimPoints)"
    }

    private var nextAction: String {
        if !app.isSpaceSelected { return "먼저 홈 탭에서 공간을 선택하세요" }
        if let r = result {
            let who = match.map { " · 자산 '\(app.assetName($0.assetId))'" } ?? ""
            return "결과: \(r.decisionKorean)\(who) — 다시 하려면 사물을 다시 조준하세요"
        }
        if candidates != nil { return "아래 후보에서 검수할 자산을 고르세요 — 추천(유사도 높은 순)이 맨 위입니다" }
        if !aimReady { return "사물 둘레를 좌우로 천천히 이동하며 비추세요 — \(aimStatus)" }
        if let p = pinnedId { return "[맞추기·검수]를 누르면 '\(app.assetName(p))'에 맞춰 검수합니다" }
        return "[자산 찾기]를 누르면 닮은 등록 자산을 찾아 후보를 보여 줍니다"
    }

    var body: some View {
        ZStack {
            ARScanView(controller: ar).ignoresSafeArea()
            Circle().strokeBorder(Color.blue, lineWidth: 2).frame(width: 36, height: 36)

            VStack(spacing: 8) {
                // 안내·상태는 눈(👁) 버튼 하나로 숨기고 보이게 — 검수 중 화면을 가리지 않도록 (스캔 화면과 같은 설정을 공유)
                HStack(alignment: .top, spacing: 0) {
                    if !hudHidden {
                        StepGuide(heading: "③ 검수 — 비춘 사물에 맞는 등록 자산을 찾아 크기·각도를 맞춰 검수합니다",
                                  steps: steps, nextAction: nextAction,
                                  warning: app.isSpaceSelected ? nil : "공간이 선택되지 않았습니다")
                    } else {
                        Spacer(minLength: 0)
                    }
                    HudEyeButton(hidden: $hudHidden).padding(.trailing, 10)
                }

                if !hudHidden {
                    HStack(spacing: 8) {
                        Text(ar.statusText).foregroundStyle(ar.trackingNormal ? .green : .orange)
                        Text(pinnedId.map { "고정 \(app.assetName($0))" } ?? "자동 매칭").foregroundStyle(.secondary)
                        Text(aimStatus).foregroundStyle(aimReady ? .green : .orange)
                    }
                    .font(.caption2)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(.thinMaterial, in: Capsule())
                } else if !ar.trackingNormal {
                    Text(ar.statusText).font(.caption2).foregroundStyle(.orange)
                        .padding(.horizontal, 10).padding(.vertical, 4).background(.thinMaterial, in: Capsule())
                }

                if let message {
                    Text(message).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
                        .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
                        .padding(.horizontal, 10)
                }

                Spacer()

                if !hudHidden, asset != nil || result != nil { layerBar }

                if let c = candidates { candidatePanel(c) }

                if let r = result {
                    if resultHidden {
                        ResultPill(result: r) { withAnimation(.snappy) { resultHidden = false } }
                            .padding(.bottom, 2)
                    } else {
                        VStack(spacing: 6) {
                            if let m = match { matchCaption(m) }
                            ResultBanner(result: r) { withAnimation(.snappy) { resultHidden = true } }
                        }
                        .padding(.bottom, 4)
                    }
                }

                controls
            }
            .padding(.top, 6)
            .padding(.bottom, 14)
        }
        .onAppear {
            ar.minObs = 2  // 검수는 폰을 고정한 채 비추는 일이 많아 2프레임 관측부터 인정 (등록은 3)
            DebugLog.log("inspect", "화면 진입 space=\(app.spaceId)#\(app.spaceNumericId) lidar=\(ar.usesLiDARMesh) map=\(useWorldMap)")
            if useWorldMap && !didAutoMap && app.isSpaceSelected {
                didAutoMap = true
                Task { await applyWorldMap(true) }  // 기본 켜짐: 지도로 세션 시작 (실패하면 일반 세션)
            } else {
                ar.start(restoringWorldMap: nil)  // 첫 진입=새 세션, 탭 복귀=이어서 재개
            }
        }
        .onDisappear { ar.pause() }
        .onReceive(Timer.publish(every: 0.6, on: .main, in: .common).autoconnect()) { _ in refreshAim() }
        .onChange(of: app.dataRevision) { _, _ in Task { await syncCurrentAsset() } }  // 관리자 수정 즉시 반영
        .onChange(of: app.spaceNumericId) { _, _ in  // 관리자가 공간을 지워 선택이 풀리면 불러온 자산·후보도 정리
            if !app.isSpaceSelected { clearLoadedAsset(); pinnedId = nil; result = nil; match = nil; candidates = nil }
        }
        .onChange(of: ar.relocalized) { _, ok in
            guard ok else { return }
            mapWatch?.cancel()
            DebugLog.log("inspect", "공간 복원 완료")
            // 재위치 인식 성공 → 설계 위치에 파란 모델 표시 (고정/매칭된 자산이 있으면)
            if let a = asset, let mesh = assetMesh, result == nil {
                try? ar.showDesign(mesh, outline: assetOutline, cards: assetCards, at: a.designPose, color: .systemBlue, view: viewProfile)
            }
        }
    }

    // MARK: - 표시 레이어 (현장 선택)

    /// 검수 때 그릴 항목. 처음엔 관리자(백오피스 → 자산 → 검수 표시 설정)가 정한 대로이고, 여기서 눌러 임시로 바꿀 수 있다.
    private var layerBar: some View {
        let p = viewProfile
        // 칩 색 = 화면에 그려지는 선 색 (외곽선은 판정색이라 파랑 계열 대표색, 박스·치수는 보라)
        let purple = Color(red: 0.78, green: 0.47, blue: 1.0)
        let items: [(String, WritableKeyPath<InspectViewJSON, Bool>, Color)] = [
            ("외곽선", \.outline, .blue), ("박스", \.box, purple), ("치수", \.dimensions, purple),
            ("실루엣", \.silhouette, .gray), ("표면", \.surface, .gray), ("점군", \.points, .gray),
        ]
        let icons = ["외곽선": "pentagon", "박스": "cube", "치수": "ruler", "실루엣": "person.crop.rectangle", "표면": "square.stack.3d.up.fill", "점군": "circle.grid.3x3.fill"]
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(items, id: \.0) { name, kp, tint in
                    Button { haptic(); toggleLayer(kp) } label: {
                        HStack(spacing: 5) {
                            Image(systemName: icons[name] ?? "circle").font(.system(size: 11, weight: .bold))
                                .foregroundStyle(p[keyPath: kp] ? Color.white : tint)
                            Text(name).font(.caption2.weight(.semibold)).lineLimit(1)
                        }
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(p[keyPath: kp] ? tint.opacity(0.55) : Color.black.opacity(0.35), in: Capsule())
                        .overlay(Capsule().strokeBorder(tint.opacity(p[keyPath: kp] ? 0.95 : 0.3), lineWidth: 1))
                        .foregroundStyle(.white)
                    }
                }
                if viewOverride != nil {
                    Button { viewOverride = nil; refreshOverlay() } label: {
                        Label("서버 설정", systemImage: "arrow.uturn.backward").font(.caption2.weight(.semibold))
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(Color.orange.opacity(0.85), in: Capsule()).foregroundStyle(.white)
                    }
                }
            }
            .padding(.horizontal, 10)
        }
        .frame(height: 32)
    }

    private func toggleLayer(_ kp: WritableKeyPath<InspectViewJSON, Bool>) {
        var v = viewProfile
        v[keyPath: kp].toggle()
        viewOverride = v
        // 실루엣을 처음 켜면 카드를 내려받아야 한다 (꺼진 설정이면 로드하지 않았다)
        if v.silhouette, assetCards.isEmpty, let id = asset?.assetId {
            Task { assetCards = await loadCards(id); refreshOverlay() }
        } else {
            refreshOverlay()
        }
        DebugLog.log("inspect", "표시 레이어 변경", extra: ["view": v.summary, "source": "현장"])
    }

    /// 선택이 바뀌면 지금 떠 있는 오버레이를 새 설정으로 다시 그린다
    private func refreshOverlay() {
        guard let mesh = assetMesh else { return }
        if let pose = actualPose, let r = result {
            ar.clearActual(); ar.clearObserved(); ar.clearOverlay()
            if viewProfile.outline, let o = lastObservedOutline { ar.showObservedOutline(o) }
            let color: UIColor = r.decision == "PASS" ? .systemGreen : (r.decision == "FAIL" ? .systemRed : .systemOrange)
            try? ar.showActual(mesh, outline: assetOutline, cards: assetCards, at: pose, color: color, view: viewProfile)
        } else if worldMap != nil && ar.relocalized, let a = asset {
            ar.clearOverlay()
            try? ar.showDesign(mesh, outline: assetOutline, cards: assetCards, at: a.designPose, color: .systemBlue, view: viewProfile)
        }
    }

    // MARK: - 매칭 요약 / 컨트롤

    /// 결과 카드 위 한 줄: 어떤 자산에 어떻게 맞았는지 (크기·각도 근거)
    private func matchCaption(_ m: MatchJSON) -> some View {
        let a = m.alignment
        let ranks = m.ranking.filter { $0.combined != nil }.prefix(3)
            .map { "\($0.assetId) \(String(format: "%.2f", $0.combined ?? 0))" }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 2) {
            Text("\(m.matchedBy == "auto" ? "🔎 자동 매칭" : "📌 고정") · \(app.assetName(m.assetId))").font(.caption.bold())
            Text(String(format: "일치 %.0f%% · 잔차 %.0fmm · 방위 %.0f° · 크기비(길이/높이) %.2f/%.2f",
                        a.inlierRatio * 100, a.rmseM * 1000, a.yawDeg, m.sizeRatio.footprint, m.sizeRatio.height))
                .font(.caption2)
            if m.matchedBy == "auto", m.ranking.count > 1 {
                Text("후보 점수(낮을수록 좋음): \(ranks)").font(.caption2).foregroundStyle(.secondary)
            }
            Text("🟡 노랑=관측한 사물 윤곽 / 선화=맞춘 등록 모델").font(.caption2).foregroundStyle(.secondary)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 16)
    }

    private var controls: some View {
        let disabled = busy || !ar.trackingNormal || !app.isSpaceSelected || !aimReady
        let pinned = pinnedId != nil
        return VStack(spacing: 8) {
            if useWorldMap {  // 월드맵 상태는 한 줄로 (자세한 설명은 맵 버튼을 길게 누르지 않아도 색으로 알 수 있다)
                HStack(spacing: 5) {
                    Image(systemName: ar.relocalized ? "checkmark.seal.fill" : "location.viewfinder")
                        .foregroundStyle(ar.relocalized ? Color.green : Color.orange)  // 상태 색은 아이콘에만
                    Text(ar.relocalized ? "공간 복원 완료 · 설계 위치 대비 편차도 판정" : "공간 복원 중 · 등록했던 장소를 비춰 주세요")
                        .foregroundStyle(Color.white)
                }
                .font(.caption2.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.75)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Capsule().fill(Color.black.opacity(0.78)))
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5))
            }
            HStack(alignment: .top, spacing: 6) {
                NavigationLink {
                    AssetManagerView(
                        onPick: { id in Task { await pin(id) } },
                        onDeleted: { ids in
                            if let p = pinnedId, ids.contains(p) { pinnedId = nil }
                            if let a = asset, ids.contains(a.assetId) { clearLoadedAsset(); result = nil; match = nil }
                        })
                } label: {
                    DockIcon(symbol: pinned ? "pin.fill" : "pin", caption: pinned ? "변경" : "고정", tint: .yellow, active: pinned)
                }
                .simultaneousGesture(TapGesture().onEnded { haptic() })

                if pinned {
                    Button {
                        haptic(); pinnedId = nil; result = nil; match = nil; candidates = nil; ar.clearAllOverlays()
                    } label: { DockIcon(symbol: "arrow.counterclockwise", caption: "다시") }
                } else {
                    Color.clear.frame(width: 58, height: 1)
                }

                Button {
                    haptic(.medium)
                    Task { if pinned { await measure() } else { await findAssets() } }
                } label: {
                    VStack(spacing: 4) {
                        Group {  // 원 배경 없이 아이콘만 — 비활성이면 아이콘만 흐리게 (글자 박스는 그대로)
                            if busy { ProgressView().tint(.white).scaleEffect(1.2) }
                            else { Image(systemName: pinned ? "scope" : "magnifyingglass").font(.system(size: 32, weight: .bold)) }
                        }
                        .frame(width: 68, height: 44).foregroundStyle(Color.white)
                        .shadow(color: .black.opacity(0.7), radius: 3, y: 1)
                        .opacity(disabled ? 0.5 : 1)
                        DockCaption(text: busy ? (pinned ? "맞추는 중" : "찾는 중") : (pinned ? "맞추기" : "자산 찾기"))
                    }
                    .frame(width: 78).foregroundStyle(.white)
                }
                .disabled(disabled)

                if result != nil {
                    Button {
                        haptic(); result = nil; match = nil; actualPose = nil
                        ar.clearActual(); ar.clearObserved()
                    } label: { DockIcon(symbol: "eraser", caption: "지우기") }
                } else {
                    Color.clear.frame(width: 58, height: 1)
                }

                Button {
                    haptic(); useWorldMap.toggle()
                } label: {
                    DockIcon(symbol: useWorldMap ? "map.fill" : "map", caption: "공간",
                             tint: ar.relocalized ? .green : .orange, active: useWorldMap)
                }
            }
            .onChange(of: useWorldMap) { _, on in
                if suppressToggle { suppressToggle = false; return }  // 코드가 바꾼 것(자동 해제)이면 무시
                Task { await applyWorldMap(on) }
            }
        }
        .padding(.horizontal, 10).padding(.top, 4).padding(.bottom, 2)
    }

    // MARK: - 추적 보정

    /// 모델 표면 점(점마다 첫 정점)을 1500개로 줄이고 법선을 미리 구해 둔다 (앱을 쓰는 동안 한 번)
    private func prepareRefineModel(_ mesh: GLBMesh) {
        refineModel = nil
        Task.detached(priority: .utility) {
            var pts: [SIMD3<Float>] = []
            var i = 0
            while i < mesh.positions.count { pts.append(mesh.positions[i]); i += 3 }
            let step = max(1, pts.count / 1500)
            let sub = stride(from: 0, to: pts.count, by: step).map { pts[$0] }
            let nrm = PoseRefiner.estimateNormals(sub, radius: 0.035)
            await MainActor.run { refineModel = (sub, nrm) }
        }
    }

    /// 오버레이가 떠 있는 동안 ~1.2초마다 지금 보이는 깊이 점으로 포즈를 다듬는다.
    /// 많이 움직이거나(>10cm·>15°) 일치율이 떨어지면 버리고, 서버가 준 포즈에서 15cm 넘게 벗어나지 않는다.
    private func startRefineLoop() {
        refineTask?.cancel()
        guard let base = actualPose, let q0 = base.quat else { return }
        let baseQ = simd_quatf(ix: Float(q0.x), iy: Float(q0.y), iz: Float(q0.z), r: Float(q0.w))
        let basePos = SIMD3<Float>(Float(base.positionM.x), Float(base.positionM.y), Float(base.positionM.z))
        var cur = RefinePose(position: basePos, yawRad: 0, base: baseQ)
        refineTask = Task {
            var tries = 0
            while !Task.isCancelled, result != nil {
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                guard !Task.isCancelled, result != nil, !busy, ar.trackingNormal, let model = refineModel else { continue }
                let diag: Float = asset?.aabb.map { a in
                    Float(zip(a.minM, a.maxM).map { ($1 - $0) * ($1 - $0) }.reduce(0, +)).squareRoot()
                } ?? 0.7
                let ext = diag * 0.6 + 0.12
                let live = ar.livePoints(center: cur.position, radius: ext)
                tries += 1
                let start = cur
                let res = await Task.detached(priority: .userInitiated) {
                    PoseRefiner.refine(model: model.pts, live: live, start: start, modelNormals: model.nrm)
                }.value
                guard !Task.isCancelled, result != nil else { break }
                let far = simd_distance(res.pose.position, basePos) > 0.15
                if tries <= 3 || tries % 10 == 0 || (res.accepted && (res.shiftM > 0.01 || abs(res.yawDeg) > 1)) {
                    DebugLog.log("inspect", "추적 보정 \(res.accepted && !far ? "적용" : "건너뜀") 이동 \(String(format: "%.1f", res.shiftM * 100))cm 회전 \(String(format: "%.1f", res.yawDeg))°",
                                 extra: ["inlier": String(format: "%.2f", res.inlier), "rmseMm": String(format: "%.1f", res.rmse * 1000),
                                         "why": far ? "서버 포즈에서 15cm 초과" : res.reason, "live": "\(live.count)"])
                }
                guard res.accepted, !far else { continue }
                cur = res.pose
                if res.shiftM > 0.002 || abs(res.yawDeg) > 0.3 {
                    let q = cur.orientation
                    actualPose?.positionM = Vec3JSON(x: Double(cur.position.x), y: Double(cur.position.y), z: Double(cur.position.z))
                    actualPose?.quat = QuatJSON(x: Double(q.imag.x), y: Double(q.imag.y), z: Double(q.imag.z), w: Double(q.real))
                    ar.updateActualPose(position: cur.position, orientation: q)
                }
            }
        }
    }

    // MARK: - 월드맵

    /// 켜면 월드맵을 내려받아 그 지도로 세션을 재시작(재위치 인식), 끄거나 실패하면 일반 세션.
    /// 12초 안에 재위치 인식이 안 되면(등록한 장소가 아님) 자동으로 끄고 사물 기준 검수로 전환한다.
    private func applyWorldMap(_ on: Bool) async {
        mapWatch?.cancel()
        if on {
            do {
                let data = try await app.api.downloadWorldMap(spaceNumericId: app.spaceNumericId)
                worldMap = try ARWorldMap.deserialize(from: data)
                ar.start(restoringWorldMap: worldMap, reset: true)
                message = nil
                mapWatch = Task {
                    try? await Task.sleep(nanoseconds: 12_000_000_000)
                    guard !Task.isCancelled, useWorldMap, !ar.relocalized else { return }
                    DebugLog.error("inspect", "공간 복원 시간 초과 — 사물 기준으로 전환")
                    fallbackToObjectOnly(note: "공간 복원이 안 돼 사물 기준으로 검수합니다 (등록한 장소에서는 복원됩니다)")
                }
            } catch {
                DebugLog.error("inspect", "월드맵 사용 불가: \(error.localizedDescription)")
                fallbackToObjectOnly(note: "이 공간에는 저장된 월드맵이 없어 사물 기준으로 검수합니다")
            }
        } else {
            worldMap = nil
            ar.start(restoringWorldMap: nil, reset: true)
            message = nil
        }
        ar.clearAllOverlays()
        result = nil; match = nil; actualPose = nil
    }

    private func fallbackToObjectOnly(note: String) {
        suppressToggle = true  // 아래 토글 변경이 applyWorldMap을 다시 부르지 않게
        useWorldMap = false
        worldMap = nil
        ar.start(restoringWorldMap: nil, reset: true)
        message = note
    }

    // MARK: - 자산 불러오기 (오버레이용)

    private func clearLoadedAsset() {
        asset = nil; assetMesh = nil; assetOutline = nil; assetCards = []
        ar.clearAllOverlays()
    }

    /// 관리자가 자산을 수정·보관·삭제했을 때: 지금 보고 있는 자산을 다시 읽어 화면(외곽선·박스·치수·실루엣·표시 설정)에 반영한다
    private func syncCurrentAsset() async {
        guard let cur = asset else { return }
        do {
            let fresh = try await app.api.asset(assetId: cur.assetId)
            if fresh.status == "ARCHIVED" {
                clearLoadedAsset(); if pinnedId == cur.assetId { pinnedId = nil }; result = nil; match = nil
                message = "'\(app.assetName(cur.assetId))' 자산이 관리자에 의해 보관 처리되어 목록에서 빠졌습니다"
                return
            }
            asset = fresh
            if fresh.cadUri != cur.cadUri {  // 모델이 다시 등록됨 → 전부 다시 받는다
                await loadAssetData(cur.assetId)
            } else if (viewOverride ?? fresh.inspectView ?? .standard).silhouette, assetCards.isEmpty {
                assetCards = await loadCards(cur.assetId)
            }
            refreshOverlay()
            DebugLog.log("inspect", "관리자 수정 반영", extra: ["asset": cur.assetId, "view": (viewOverride ?? fresh.inspectView ?? .standard).summary])
        } catch let e as APIError where e.status == 404 {
            clearLoadedAsset(); if pinnedId == cur.assetId { pinnedId = nil }; result = nil; match = nil
            message = "'\(app.assetName(cur.assetId))' 자산이 관리자에 의해 삭제되었습니다"
        } catch { /* 일시적인 오류는 다음 변경 때 다시 */ }
    }

    /// 자산 고정: 선택한 자산에 맞춘다
    private func pin(_ id: String) async {
        pinnedId = id
        result = nil; match = nil; actualPose = nil; message = nil
        ar.clearAllOverlays()
        await loadAssetData(id)
        if worldMap != nil && ar.relocalized, let a = asset, let mesh = assetMesh {
            try? ar.showDesign(mesh, outline: assetOutline, cards: assetCards, at: a.designPose, color: .systemBlue, view: viewProfile)
        }
    }

    /// 관리자가 정한 이 자산의 검수 표시 레이어 (없으면 기본: 외곽 박스·치수·사진 실루엣)
    /// 현장에서 칩으로 바꾼 값이 있으면 그것을, 없으면 관리자 설정을 쓴다
    private var viewProfile: InspectViewJSON { viewOverride ?? asset?.inspectView ?? .standard }

    @discardableResult
    private func loadAssetData(_ id: String) async -> Bool {
        do {
            let a = try await app.api.asset(assetId: id)
            let mesh = try GLBReader.read(try await app.api.assetModel(assetId: id))
            asset = a
            assetMesh = mesh
            prepareRefineModel(mesh)
            assetOutline = try? await app.api.assetOutline(assetId: id)  // 없으면 표면 오버레이로 대체
            let prof = a.inspectView ?? .standard
            assetCards = prof.silhouette ? await loadCards(id) : []  // 실루엣을 안 쓰면 64컷을 내려받지 않는다
            DebugLog.log("inspect", "자산 로드 \(id) outline=\(assetOutline.map { "\($0.lines.count)선" } ?? "없음") 사진카드=\(assetCards.count)",
                         extra: ["view": prof.summary])
            return true
        } catch {
            asset = nil; assetMesh = nil; assetOutline = nil; assetCards = []
            DebugLog.error("inspect", "자산 로드 실패 id=\(id): \(error.localizedDescription)")
            message = "자산 로드 실패: \(error.localizedDescription)"
            return false
        }
    }

    /// 방향별 사진 실루엣 카드를 내려받아 텍스처로 만든다 (없거나 실패하면 빈 배열 — 선화만 보인다)
    private func loadCards(_ id: String) async -> [PhotoCardAsset] {
        guard let list = try? await app.api.assetPhotos(assetId: id) else { return [] }
        // 24컷을 동시에 내려받는다 (작은 PNG라 순차로 받으면 검수 시작이 늦다)
        let datas = await withTaskGroup(of: (Int, Data?).self) { group -> [Int: Data] in
            for (i, p) in list.photos.enumerated() {
                group.addTask { (i, try? await self.app.api.assetPhotoPNG(assetId: id, side: p.side)) }
            }
            var m: [Int: Data] = [:]
            for await (i, d) in group { if let d { m[i] = d } }
            return m
        }
        var out: [PhotoCardAsset] = []
        for (i, p) in list.photos.enumerated() {
            guard p.center.count == 3, p.right.count == 3, p.up.count == 3, p.forward.count == 3,
                  let data = datas[i],
                  let src = CGImageSourceCreateWithData(data as CFData, nil),
                  let cg = CGImageSourceCreateImageAtIndex(src, 0, nil),
                  let tex = try? TextureResource.generate(from: cg, withName: nil, options: .init(semantic: .color))
            else { continue }
            func v(_ a: [Double]) -> SIMD3<Float> { SIMD3(Float(a[0]), Float(a[1]), Float(a[2])) }
            out.append(PhotoCardAsset(side: p.side, center: v(p.center), right: v(p.right), up: v(p.up), forward: v(p.forward),
                                      widthM: Float(p.widthM), heightM: Float(p.heightM), texture: tex))
        }
        return out
    }

    // MARK: - 맞추기·검수

    /// 조준점과 그 주변 점 수를 주기적으로 갱신 — 누르기 전에 준비 상태를 보여 준다
    private func refreshAim() {
        guard !busy else { return }
        if let h = ar.centerHitPosition() {
            aimHint = h
            aimPoints = ar.nearbyStableCount(center: h, radius: 0.35)
        } else {
            aimHint = nil
            aimPoints = 0
        }
    }

    /// 자산 찾기: 비춘 사물과 닮은 등록 자산 후보를 유사도 순으로 받아 보여 준다 (판정·저장 없음)
    private func findAssets() async {
        busy = true
        message = nil
        defer { busy = false }
        let restored = worldMap != nil && ar.relocalized
        guard let hint = ar.centerHitPosition() else {
            message = "사물을 찾지 못했습니다 — 십자선을 사물 가운데에 맞추세요"
            return
        }
        let c = ar.contextSample(center: hint, radius: 1.2, maxPoints: 20_000)
        DebugLog.log("inspect", "자산 찾기 pts=\(c.points.count) aim=\(aimPoints)", extra: ["restored": "\(restored)"])
        guard c.points.count >= 40 else {
            message = "사물 주변 점이 부족합니다(\(c.points.count)개) — 사물 둘레를 천천히 이동하며 비춘 뒤 다시 시도하세요"
            return
        }
        lastCloud = (c.points, hint)
        let flat = c.points.flatMap { [$0.x, $0.y, $0.z] }
        let data = flat.withUnsafeBufferPointer { Data(buffer: $0) }
        let req = MatchRequest(spaceId: app.spaceNumericId,
                               cloud: CloudPayload(count: c.points.count, base64: data.base64EncodedString()),
                               hintCenter: Vec3JSON(x: Double(hint.x), y: Double(hint.y), z: Double(hint.z)),
                               worldMapRestored: restored)
        do {
            let r = try await app.api.match(req)
            candidates = r
            result = nil; match = nil
            ar.clearActual(); ar.clearOverlay()
            if let o = r.observedOutline { ar.showObservedOutline(o) }  // 비춘 사물 윤곽(노랑)을 먼저 보여 준다
            DebugLog.log("inspect", "후보 \(r.candidates.count)개 best=\(r.best ?? "없음")",
                         extra: ["cands": r.candidates.map { "\($0.assetId):\(Int($0.similarity))%\($0.accepted ? "✓" : "")" }.joined(separator: ","),
                                 "seg": "\(r.segmentation.nIn)점/\(r.segmentation.quality)"])
        } catch {
            DebugLog.error("inspect", "자산 찾기 실패: \(error.localizedDescription)")
            message = error.localizedDescription  // 등록된 자산이 없으면 서버 안내문이 나온다
        }
    }

    /// 후보 선택 → 그 자산을 고정하고, 찾을 때 쓴 스캔으로 바로 검수한다
    private func chooseCandidate(_ id: String) async {
        DebugLog.log("inspect", "후보 선택 \(id)")
        candidates = nil
        await pin(id)
        await measure(reuseScan: true)
    }

    // MARK: - 후보 패널

    private func candidatePanel(_ c: MatchResultJSON) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(c.accepted ? "자산 찾기 결과 — 닮은 순" : "닮은 자산이 없어 보입니다").font(.subheadline.bold())
                Spacer()
                Button { candidates = nil; ar.clearObserved() } label: { Image(systemName: "xmark.circle.fill") }
                    .foregroundStyle(.secondary)
            }
            if !c.accepted {
                Text("비춘 사물과 크기·형상이 맞는 등록 자산이 없습니다. 사물이 가운데 오도록 다시 비추거나, 아래에서 직접 고르세요.")
                    .font(.caption2).foregroundStyle(.orange)
            }
            ForEach(Array(c.candidates.enumerated()), id: \.element.id) { i, cand in
                HStack(spacing: 10) {
                    CardThumbView(assetId: cand.assetId)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(app.assetName(cand.assetId)).font(.footnote.bold())
                            if i == 0 && cand.accepted {
                                Text("추천").font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 1)
                                    .background(Color.green.opacity(0.85), in: Capsule()).foregroundStyle(.white)
                            }
                        }
                        ProgressView(value: min(cand.similarity, 100), total: 100).tint(cand.accepted ? .green : .orange)
                        Text(String(format: "유사도 %.0f%% · 일치 %.0f%% · 크기비 %.2f/%.2f", cand.similarity, cand.inlier * 100,
                                    cand.sizeRatio.footprint, cand.sizeRatio.height))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Button { Task { await chooseCandidate(cand.assetId) } } label: {
                        Image(systemName: "checkmark").font(.system(size: 16, weight: .bold)).frame(width: 44, height: 38)
                    }
                    .buttonStyle(.borderedProminent).buttonBorderShape(.capsule)
                    .tint(i == 0 && cand.accepted ? .green : .accentColor)
                    .accessibilityLabel("이 자산으로 검수")
                }
            }
        }
        .padding(10)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .padding(.horizontal, 10)
    }

    // MARK: - 맞추기·검수

    /// 고정한 자산에 맞춰 검수. reuseScan=true면 [자산 찾기] 때 쓴 스캔으로 바로 진행한다.
    private func measure(reuseScan: Bool = false) async {
        busy = true
        message = nil
        defer { busy = false }

        // 지도를 불러온 것만으로는 부족 — 재위치 인식이 성공했을 때만 공간 프레임이 맞다
        let restored = worldMap != nil && ar.relocalized
        let designPos = asset.map {
            SIMD3<Float>(Float($0.designPose.positionM.x), Float($0.designPose.positionM.y), Float($0.designPose.positionM.z))
        }
        let points: [SIMD3<Float>]
        let hint: SIMD3<Float>
        if reuseScan, let last = lastCloud {
            points = last.points; hint = last.hint
        } else {
            guard let h = ar.centerHitPosition() ?? (restored && pinnedId != nil ? designPos : nil) else {
                message = "사물을 찾지 못했습니다 — 십자선을 사물 가운데에 맞추고 천천히 움직여 점을 더 모으세요"
                DebugLog.error("inspect", "맞추기 중단: 조준점 표면 없음", extra: ["feature": "\(ar.featurePointCount)"])
                return
            }
            // 서버가 사물을 분리할 수 있게 배경(탁자·바닥)까지 포함한 넉넉한 반경의 점을 보낸다
            points = ar.contextSample(center: h, radius: 1.2, maxPoints: 20_000).points
            hint = h
            let minPoints = ar.hasLiDAR ? 400 : 60
            guard points.count >= minPoints else {
                DebugLog.error("inspect", "맞추기 중단: 점 부족 pts=\(points.count) min=\(minPoints)", extra: ["feature": "\(ar.featurePointCount)"])
                message = "사물 주변 점이 부족합니다(\(points.count)개) — 사물 둘레를 좌우로 천천히 이동하며 비춘 뒤 다시 시도하세요"
                return
            }
        }
        let c = (points: points, isMesh: false)
        DebugLog.log("inspect", "맞추기 시작 pts=\(c.points.count) hint=(\(hint.x), \(hint.y), \(hint.z)) restored=\(restored)",
                     extra: ["mode": pinnedId == nil ? "auto" : "pinned:\(pinnedId!)", "reuseScan": "\(reuseScan)",
                             "tracking": "\(ar.trackingNormal)", "feature": "\(ar.featurePointCount)"])

        let flat = c.points.flatMap { [$0.x, $0.y, $0.z] }
        let data = flat.withUnsafeBufferPointer { Data(buffer: $0) }
        let req = MeasurementRequest(
            assetId: pinnedId,
            autoMatch: pinnedId == nil ? true : nil,
            spaceId: pinnedId == nil ? app.spaceNumericId : nil,
            deviceId: app.deviceId, observationId: nil,
            alignment: "object",
            hintCenter: Vec3JSON(x: Double(hint.x), y: Double(hint.y), z: Double(hint.z)),
            worldMapRestored: restored,
            cloud: CloudPayload(count: c.points.count, base64: data.base64EncodedString()),
            trackingNormal: ar.trackingNormal,
            depthValidRatio: ar.depthValidRatio > 0 ? ar.depthValidRatio : nil
        )
        do {
            let out = try await app.api.measure(req)
            result = out.result
            match = out.match
            resultHidden = false
            actualPose = out.observation.actualPose
            startRefineLoop()
            let matchedId = out.match?.assetId ?? out.observation.assetId
            let a = out.match?.alignment
            DebugLog.log("inspect", "판정 \(out.result.decision) 자산=\(matchedId)",
                         extra: ["by": out.match?.matchedBy ?? "-",
                                 "inlier": a.map { String(format: "%.3f", $0.inlierRatio) } ?? "-",
                                 "rmseMm": a.map { String(format: "%.1f", $0.rmseM * 1000) } ?? "-",
                                 "yaw": a.map { String(format: "%.1f", $0.yawDeg) } ?? "-",
                                 "sizeRatio": out.match.map { String(format: "%.2f/%.2f", $0.sizeRatio.footprint, $0.sizeRatio.height) } ?? "-",
                                 "seg": out.match.map { "\($0.segmentation.nIn)점/\($0.segmentation.quality)" } ?? "-",
                                 "ranking": out.match?.ranking.map { "\($0.assetId):\($0.combined.map { String(format: "%.2f", $0) } ?? "-")" }.joined(separator: ",") ?? "-"])

            // 매칭된 자산이 지금 불러온 것과 다르면 오버레이용으로 불러온다
            if asset?.assetId != matchedId {
                await loadAssetData(matchedId)
            } else if let fresh = try? await app.api.asset(assetId: matchedId) {
                // 같은 자산을 다시 검수할 때도 관리자가 그 사이 바꾼 '검수 표시 설정'을 반영한다
                asset = fresh
                if (viewOverride ?? fresh.inspectView ?? .standard).silhouette, assetCards.isEmpty { assetCards = await loadCards(matchedId) }
            }
            ar.clearActual(); ar.clearObserved(); ar.clearOverlay()
            if let mesh = assetMesh, let pose = actualPose {
                let color: UIColor = out.result.decision == "PASS" ? .systemGreen : (out.result.decision == "FAIL" ? .systemRed : .systemOrange)
                try? ar.showActual(mesh, outline: assetOutline, cards: assetCards, at: pose, color: color, view: viewProfile)
            }
            // 비춘 사물 윤곽(노랑)은 카메라에 보인 면만 있다(보통 사물의 앞 60~80%) — 전체 모양처럼 보이지 않도록
            // 판정 뒤에는 '외곽선' 레이어를 켰을 때만 등록 모델 외곽선과 겹쳐 비교용으로 보여 준다
            lastObservedOutline = out.match?.observedOutline
            if viewProfile.outline, let o = lastObservedOutline { ar.showObservedOutline(o) }
        } catch {
            DebugLog.error("inspect", "맞추기 실패: \(error.localizedDescription)")
            message = error.localizedDescription  // 자동 매칭 실패면 서버 안내문이 그대로 나온다
        }
    }
}


// MARK: - 하단 도크 부품

/// 아이콘 원형 버튼 + 짧은 한 줄 캡션 (글자가 길어 줄바꿈되지 않도록 캡션은 3~4자)
struct DockIcon: View {
    let symbol: String
    let caption: String
    var tint: Color = .white
    var active = false

    var body: some View {
        VStack(spacing: 5) {
            // 아이콘은 원 없이 — 카메라 화면 위에서도 보이도록 그림자만
            Image(systemName: symbol)
                .font(.system(size: 24, weight: .semibold))
                .frame(width: 48, height: 40)
                .foregroundStyle(active ? tint : Color.white)
                .shadow(color: .black.opacity(0.7), radius: 3, y: 1)
            DockCaption(text: caption)
        }
        .frame(width: 58)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(caption)
    }
}

/// 글자가 어두운 배경에서도 읽히도록 글자 자체를 작은 알약 박스에 담는다 (아이콘을 감싸는 원 대신)
struct DockCaption: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 10.5, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.8)
            .foregroundStyle(Color.white)
            .padding(.horizontal, 7).padding(.vertical, 2.5)
            .background(Capsule().fill(Color.black.opacity(0.78)))  // 항상 검정 바탕 + 흰 글자 (아이콘 색과 무관하게 읽힌다)
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5))
    }
}

@MainActor
func haptic(_ style: UIImpactFeedbackGenerator.FeedbackStyle = .light) {
    UIImpactFeedbackGenerator(style: style).impactOccurred()
}
