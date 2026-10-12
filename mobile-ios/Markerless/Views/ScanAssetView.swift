// ScanAssetView.swift — ② 스캔·등록: 사물 스캔 → POI(중심) 지정 → 사물 분리 확인 → 임시 설계 모델 저장
//
// 흐름:
//  1. 사물 주위를 돌며 스캔 (LiDAR: 메시 / 그 외: VIO 특징점 누적)
//  2. 십자선을 사물 중심에 맞추고 [중심 지정] → POI
//  3. 서버가 POI 기준으로 사물만 분리 (바닥·탁자·벽 제거 + 연결 성분) → 초록 점 미리보기·통계 확인,
//     범위 반경 슬라이더로 재계산
//  4. 분리된 사물만 자산 로컬 프레임(사물 중심 원점)으로 GLB 생성 → 업로드, designPose = 사물 중심

import simd
import SwiftUI

struct ScanAssetView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var ar = ScanSessionController()

    @State private var assetId = ScanAssetView.newAssetId()
    @State private var family = "설비"
    @State private var center: SIMD3<Float>?  // POI (사용자가 가리킨 점)
    @State private var busy = false
    @State private var message: String?  // 오류/주의 (빨강)
    @State private var infoMessage: String?  // 안내 (파랑)
    @FocusState private var idFocused: Bool

    // 사물 분리
    /// 사물 최대 반경(m) — 분리 결과를 이 범위로 제한한다
    @State private var scopeRadius: Float = 0.8
    @State private var ctx: ScanSessionController.ScanContext?
    @State private var seg: SegmentOutJSON?
    @State private var mask: [Bool] = []
    @State private var segBusy = false
    @State private var segFallback = false  // 서버 분리 실패 → 단순 반경 절단
    @State private var outline: OutlineJSON?  // 분리된 사물의 외곽선 (선으로 표시)
    @State private var assessment: AssessmentJSON?  // 등록 품질 평가 (면 확보·밀도·노이즈)
    @State private var refinedPoints: [SIMD3<Float>] = []  // 서버가 정제한 사물 점 (VIO)
    @State private var refinedNormals: [SIMD3<Float>] = []
    @State private var confirmLowQuality = false

    // 저장 결과
    @State private var savedId: String?
    @State private var saveNote: String?
    @State private var savedCards: [String: UIImage] = [:]  // 방향별 사진 실루엣 썸네일 (저장 직후 확인용)
    @State private var showManager = false
    @State private var showCards = false  // 저장 직후 사진 실루엣 썸네일 펼치기 (기본은 접어서 카메라 시야 확보)

    // 대상(POI)을 먼저 지정하는 스캔
    @State private var reviewing = false  // 분리 확인 단계
    @State private var autoCalib = true  // 중심 자동 보정 (손으로 옮기면 꺼진다)
    @State private var centerConfirmed = false
    @State private var showCalib = false  // 중심 미세조정 패드
    @State private var assist: ScanCoverage.Assist?
    @State private var thumb: ScanSessionController.ThumbData?
    @State private var liveBusy = false
    @State private var lastLiveCount = 0
    @State private var lastLiveAt = Date.distantPast
    @State private var guideExpanded = false
    // 실시간 품질·누락 안내 (0.25초마다 갱신)
    @State private var live: LiveSnapshot?
    @State private var guide: ScanCoverage.Guidance?
    @State private var ringTarget: (ring: Int, sector: Int)?
    @State private var rescan: ScanCoverage.RescanTarget?  // 점검 화면에서 고른 '다시 스캔' 목표
    @State private var showPreReview = false
    @State private var toast: String?
    // 화면을 가리는 안내는 눈(👁) 버튼 하나로 숨기고 보이게 — 설정은 기억한다
    @AppStorage("hud.hidden") private var hudHidden = false
    @AppStorage("hud.thumbExpanded") private var thumbExpanded = true

    // MARK: - 단계

    private enum Phase { case needSpace, aim, scan, review, saved }
    private var phase: Phase {
        if !app.isSpaceSelected { return .needSpace }
        if savedId != nil { return .saved }
        if center == nil { return .aim }
        return reviewing ? .review : .scan
    }
    private struct LiveSnapshot {
        var level: QualityLevel
        var score: Float
        var reasons: [QualityReason]
        var distance: Float?
    }

    private var segmented: Bool { center != nil && (seg != nil || segFallback) && !segBusy }

    private var steps: [GuideStep] {
        let saved = savedId != nil
        return .make(["공간 선택", "대상 지정", "둘레 스캔", "분리 확인", "모델 저장"],
                     done: [app.isSpaceSelected, center != nil || saved, ar.coverage.canReview || reviewing || saved,
                            segmented || saved, saved])
    }

    private var phaseIndexTitle: (Int, String) {
        switch phase {
        case .needSpace: return (1, "공간 선택")
        case .aim: return (2, "대상 지정")
        case .scan: return (3, "둘레 스캔")
        case .review: return (4, "분리 확인")
        case .saved: return (5, "저장 완료")
        }
    }

    private var nextAction: String {
        switch phase {
        case .needSpace: return "먼저 홈 탭에서 공간을 선택하세요"
        case .saved: return "저장 완료 — ③ 검수 탭에서 같은 사물을 비춰보세요"
        case .aim: return "스캔할 사물의 한가운데를 십자선에 맞추고 [대상 지정] — 사물에서 0.3~1m"
        case .scan:
            if !centerConfirmed { return "초록 마커가 사물 가운데인지 [중심 보정]으로 확인하고, 둘레를 천천히 돌며 비추세요" }
            return "사물 둘레를 천천히 돌며 비추세요 — 오른쪽 위 지도가 채워집니다"
        case .review:
            if segBusy { return "사물을 분리하는 중…" }
            if seg?.quality == "poor" || (seg == nil && segFallback) {
                return "분리가 불확실합니다 — 범위 반경을 조절하거나 [더 스캔]으로 보강하세요"
            }
            return "초록 외곽선이 사물만 감싸는지 확인하고 [모델 저장] — 부족하면 [더 스캔]"
        }
    }

    var body: some View {
        ZStack {
            ARScanView(controller: ar).ignoresSafeArea()

            // 십자선: 대상 지정 전에는 크게, 이후에는 작은 십자 (마커가 중심을 보여 준다)
            if phase == .aim || phase == .needSpace {
                ZStack {
                    Circle().strokeBorder(Color.green, lineWidth: 2).frame(width: 40, height: 40)
                    Circle().fill(Color.green).frame(width: 4, height: 4)
                }
            } else if phase == .scan {
                Image(systemName: "plus").font(.system(size: 16, weight: .light)).foregroundStyle(.white.opacity(0.55))
            }

            VStack(spacing: 8) {
                // 상단: 왼쪽 = 단계 알약(눌러서 전체 안내), 오른쪽 = 눈 버튼 + 스캔 썸네일
                HStack(alignment: .top, spacing: 8) {
                    if !hudHidden {
                        VStack(alignment: .leading, spacing: 6) {
                            PhasePill(title: phaseIndexTitle.1, index: phaseIndexTitle.0, total: 5, expanded: $guideExpanded)
                            if guideExpanded {
                                StepGuide(heading: "② 스캔·등록 — 사물을 스캔해 임시 설계 모델을 만듭니다",
                                          steps: steps, nextAction: nextAction,
                                          warning: app.isSpaceSelected ? nil : "공간이 선택되지 않았습니다")
                                    .padding(.horizontal, -10)
                            }
                        }
                    }
                    Spacer(minLength: 0)
                    VStack(alignment: .trailing, spacing: 6) {
                        HudEyeButton(hidden: $hudHidden)
                        if !hudHidden, phase == .scan || phase == .review, let t = thumb {
                            ScanThumbnail(data: t, target: ringTarget, expanded: $thumbExpanded)
                        }
                    }
                }
                .padding(.horizontal, 10)

                if !hudHidden {
                    // 촬영 상태 + 지금 가장 도움 되는 한 줄 (경고가 있으면 그 이유·조치가, 좋으면 방향 안내가 같은 칸에 나온다 → 배너는 하나)
                    if phase == .scan, let l = live {
                        LiveQualityBar(level: l.level, score: l.score, reasons: l.level == .good ? [] : l.reasons,
                                       distanceM: l.distance, hint: scanHint)
                            .padding(.horizontal, 10)
                        if let r = rescan {
                            HStack(spacing: 8) {
                                Label("다시 스캔: \(Self.sideLabel(r.title))", systemImage: "viewfinder").font(.caption.weight(.semibold))
                                Button { rescan = nil } label: { Text("취소").font(.caption) }.buttonStyle(.bordered).controlSize(.mini)
                            }
                            .padding(.horizontal, 10).padding(.vertical, 5).background(.thinMaterial, in: Capsule())
                        }
                    }
                    else if phase == .scan, let a = assist { AssistChip(assist: a) }
                    else if phase == .aim || phase == .needSpace {
                        AssistChip(assist: .init(level: .info, symbol: "scope", text: nextAction))
                    }

                    if phase == .scan || phase == .aim, !ar.trackingNormal {
                        Text(ar.statusText).font(.caption2).foregroundStyle(.orange)
                            .padding(.horizontal, 10).padding(.vertical, 4).background(.thinMaterial, in: Capsule())
                    }

                    if phase == .review { reviewPanel }
                    if phase == .scan && showCalib { calibPanel }

                    if let toast { ScanToast(text: toast) }
                    if let infoMessage {
                        Text(infoMessage).font(.footnote).foregroundStyle(.blue)
                            .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
                            .padding(.horizontal, 10)
                    }
                }
                if let message {  // 오류는 숨김 상태에서도 알린다
                    Text(message).font(.footnote).foregroundStyle(.red)
                        .padding(8).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
                        .padding(.horizontal, 10)
                }

                Spacer()

                bottomActions
            }
            .padding(.top, 6)
            .padding(.bottom, 20)

            // 빠진 방향이 화면 밖이면 가장자리 화살표 (사물 방위는 사람의 왼쪽으로 늘어남 → edgeDeg 부호는 Guidance가 처리)
            if phase == .scan, !hudHidden, let g = guide {
                EdgeGuide(deltaDeg: g.edgeDeg, vertical: g.up ? .up : .none,
                          label: g.up ? "" : ScanCoverage.sideName(start: g.sector, length: 1))
            }
        }
        .fullScreenCover(isPresented: $showPreReview) { preReviewCover }
        .ignoresSafeArea(.keyboard)  // 키보드가 하단 버튼을 밀어 올리거나 가리지 않게
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("완료") { idFocused = false }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { showManager = true } label: { Label("자산 관리", systemImage: "list.bullet") }
            }
        }
        .confirmationDialog("사물의 일부 면만 스캔됐습니다", isPresented: $confirmLowQuality, titleVisibility: .visible) {
            Button("그래도 저장", role: .destructive) { Task { await register() } }
            Button("더 스캔하기", role: .cancel) { backToScanning() }
        } message: {
            Text("확보된 면 \(assessment?.sidesCovered ?? 0)/4 · 품질 \(assessment?.score ?? 0)점\n"
                 + (assessment?.tips.prefix(2).joined(separator: "\n") ?? "")
                 + "\n\n한쪽 면만 있는 모델은 다른 방향에서 검수하면 크기·각도가 맞지 않을 수 있습니다.")
        }
        .sheet(isPresented: $showManager) {
            NavigationStack {
                AssetManagerView(onPick: nil)
                    .navigationTitle("자산 관리").navigationBarTitleDisplayMode(.inline)
            }
        }
        .onAppear {
            DebugLog.log("scan", "화면 진입 space=\(app.spaceId)#\(app.spaceNumericId) lidar=\(ar.usesLiDARMesh)")
            ar.start(restoringWorldMap: nil)  // 첫 진입=새 세션(공간 프레임 정의), 탭 복귀=이어서 재개
            LocationProvider.shared.start()
            if let rid = app.reregisterAssetId {  // 자산 관리의 [재등록]
                app.reregisterAssetId = nil
                resetForNewAsset(id: rid)
                infoMessage = "'\(rid)' 재등록 — 사물을 다시 스캔해 같은 ID로 저장하면 이전 모델을 덮어씁니다"
            }
        }
        .onDisappear { ar.pause() }
        .onReceive(Timer.publish(every: 0.6, on: .main, in: .common).autoconnect()) { _ in tick() }
        .onReceive(Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()) { _ in fastTick() }
    }

    // MARK: - 주기 갱신: 썸네일·어시스트·라이브 외곽 보정

    private func tick() {
        guard phase == .scan || phase == .review else { return }
        thumb = hudHidden ? nil : ar.thumbnailData()
        guard phase == .scan else { return }
        assist = hudHidden ? nil : ar.assistNow()
        maybeLiveRefine()
    }

    // MARK: - 실시간 품질·누락 안내

    private static func sideLabel(_ title: String) -> String { title.components(separatedBy: " · ").first ?? title }

    /// 배너의 보조 문구: 재스캔 중이면 어느 쪽으로 가야 하는지, 윗면만 남았으면 내려다보기, 아니면 기존 어시스트
    private var scanHint: String? {
        if let r = rescan {
            let name = Self.sideLabel(r.title)
            guard let g = guide else { return "‘\(name)’ 보강 중 — 이 방향을 천천히 비추세요" }
            if g.up { return "위에서 내려다보며 윗면을 훑어 주세요" }
            guard let d = g.edgeDeg else { return "‘\(name)’ 보강 중 — 이 방향을 천천히 비추세요" }
            return "‘\(name)’ 다시 스캔 — \(d < 0 ? "왼쪽" : "오른쪽")으로 약 \(abs(Int(d)))° 이동"
        }
        if guide?.up == true { return "위에서 내려다보며 윗면도 훑어 주세요" }
        return assist?.text
    }

    private func showToast(_ t: String) {
        withAnimation { toast = t }
        Task {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            await MainActor.run { withAnimation { if toast == t { toast = nil } } }
        }
    }

    private func fastTick() {
        guard phase == .scan, let sec = ar.cameraSector() else {
            if live != nil { live = nil; guide = nil; ringTarget = nil }
            return
        }
        let cov = ar.coverage
        if var t = thumb { t.coverage = cov; t.camSector = sec; thumb = t }
        if let r = rescan, cov.isSatisfied(r) {  // 목표 달성 → 자동 해제
            ScanHaptics.complete()
            showToast("‘\(Self.sideLabel(r.title))’ 보강 완료")
            rescan = nil
        }
        guide = cov.guidance(camSector: sec, rescan: rescan)
        ringTarget = cov.ringTarget(camSector: sec, rescan: rescan)
        if ar.liveOut != nil {
            live = LiveSnapshot(level: ar.liveFilter.level, score: ar.liveFilter.score, reasons: ar.liveFilter.reasons, distance: ar.liveDistance)
        }
    }

    // MARK: - 분리 확인 전 점검

    private var preReviewCover: some View {
        let cov = ar.coverage
        return ScanReviewView(
            title: "분리 확인 전 점검 — 빠진 면이 있는지 확인",
            grid: cov.reviewGrid, specs: RingSpec.object, mirror: true,
            percent: cov.azimuthProgress, minRecommended: 0.9,
            items: cov.problemDescs().map(ReviewItem.init),
            onRescan: { item in startRescan(item) },
            onProceed: { showPreReview = false; Task { await confirmSeparation() } })
    }

    /// [분리 확인]: 둘레가 90% 미만이거나 품질 낮은 칸이 있으면 점검 화면, 아니면 바로 진행
    private func requestReview() {
        let cov = ar.coverage
        if cov.needsReview() {
            showPreReview = true
        } else {
            if cov.topProgress < ScanCoverage.topListBelow { infoMessage = "둘레는 충분합니다 — 윗면은 아직 거의 없습니다(선택)" }
            Task { await confirmSeparation() }
        }
    }

    private func startRescan(_ item: ReviewItem?) {
        let cov = ar.coverage
        let ds = cov.problemDescs()
        let d = item.flatMap { it in ds.first { $0.ring == it.ring && $0.start == it.sector } } ?? ds.first
        showPreReview = false
        guard let d else { return }
        let t = cov.rescanTarget(for: d)
        ar.coverage.prepareRescan(t)
        rescan = t
        infoMessage = nil
        showToast("‘\(Self.sideLabel(d.title))’ 쪽으로 이동해 다시 비추세요")
    }

    /// 스캔하는 동안 몇 초마다 대상 주변을 서버에서 분리해 초록 외곽선으로 보여 준다.
    /// 외곽선이 사물을 감싸는 모습이 곧 진행·보정 피드백이고, 자동 보정이 켜져 있으면 중심을 사물 중앙으로 당겨 준다.
    private func maybeLiveRefine() {
        guard !liveBusy, !segBusy, let c = center, Date().timeIntervalSince(lastLiveAt) > 3.0 else { return }
        let n = ar.nearbyStableCount(center: c, radius: 0.5)
        guard n >= (ar.hasLiDAR ? 400 : 60), lastLiveCount == 0 || n >= Int(Float(lastLiveCount) * 1.15) else { return }
        lastLiveCount = n
        lastLiveAt = Date()
        liveBusy = true
        Task { await liveRefine(from: c); liveBusy = false }
    }

    private func liveRefine(from c: SIMD3<Float>) async {
        let cx = ar.contextSample(center: c, radius: 1.0, maxPoints: 15_000)  // 3초마다 보내므로 가볍게
        guard cx.points.count >= 120, phase == .scan else { return }
        do {
            let out = try await app.api.segment(points: cx.points, poi: c, maxRadius: min(scopeRadius, 0.8), refine: false)
            guard phase == .scan, out.quality != "poor", out.nIn >= 100 else { return }
            if let o = out.outline { ar.showOutlinePreview(o) }
            guard autoCalib, let cur = center else { return }
            let target = SIMD3<Float>(Float(out.center.x), Float(out.center.y), Float(out.center.z))
            var d = target - cur
            let dist = simd_length(d)
            guard dist > 0.008 else { return }
            if dist > 0.12 { d = d / dist * 0.12 }  // 한 번에 12cm 넘게는 옮기지 않는다 (튀는 분리 방지)
            let moved = cur + d
            center = moved
            ar.moveTarget(moved)
            DebugLog.log("scan", "중심 자동 보정 \(String(format: "%.1f", simd_length(d) * 100))cm",
                         extra: ["nIn": "\(out.nIn)", "quality": out.quality])
        } catch { /* 다음 주기에 다시 시도 */ }
    }

    // MARK: - 중심 보정 패널

    private var calibPanel: some View {
        VStack(spacing: 6) {
            NudgePad(onMove: { r, u, f in nudge(r, u, f) }, onConfirm: {
                centerConfirmed = true
                showCalib = false
                infoMessage = "중심을 확인했습니다 — 이제 둘레를 천천히 돌며 비추세요"
            }, confirmed: centerConfirmed)
            if !autoCalib {
                Button { autoCalib = true; lastLiveAt = .distantPast } label: {
                    Label("자동 보정 다시 켜기", systemImage: "wand.and.stars").font(.caption2)
                }.buttonStyle(.bordered).controlSize(.small)
            } else {
                Text("스캔이 쌓이면 중심이 사물 가운데로 자동 보정됩니다").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 10)
    }

    private func nudge(_ r: Float, _ u: Float, _ f: Float) {
        guard let c = center, let ax = ar.cameraAxes() else { return }
        let nc = c + ax.right * r + ax.up * u + ax.fwd * f
        center = nc
        autoCalib = false  // 손으로 맞춘 중심은 자동 보정이 덮어쓰지 않는다
        centerConfirmed = false
        ar.moveTarget(nc)
    }

    // MARK: - 분리 확인 패널
    private var reviewPanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            TextField("자산 ID (예: PUMP-301)", text: $assetId)
                .textFieldStyle(.roundedBorder).autocorrectionDisabled().textInputAutocapitalization(.never)
                .submitLabel(.done)
                .focused($idFocused)
                .onSubmit { idFocused = false }
            Text("같은 ID로 다시 저장하면 이전 모델을 덮어씁니다").font(.caption2).foregroundStyle(.secondary)

            Divider()

            if !segBusy && (seg != nil || segFallback) {
                let adv = reviewAdvice
                HStack(alignment: .top, spacing: 10) {
                    DomeMap(coverage: ar.coverage, camSector: nil, targetSector: nil).frame(width: 66, height: 66)
                    VStack(alignment: .leading, spacing: 3) {
                        Label(adv.title, systemImage: adv.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .font(.caption.bold()).foregroundStyle(adv.ok ? Color.green : Color.orange)
                        Text(adv.detail).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }

            if segBusy {
                HStack { ProgressView(); Text("사물 분리 중…").font(.caption) }
            } else if let s = seg {
                let size = s.sizeCm
                HStack(spacing: 6) {
                    Text(qualityBadge(s.quality)).font(.caption.bold())
                    Text("사물 \(s.nIn)점 · 배경 \(s.nRemoved)점 제거").font(.caption)
                }
                Text(String(format: "크기 %.0f × %.0f × %.0f cm (가로×높이×깊이)", size.0, size.1, size.2))
                    .font(.caption2).foregroundStyle(.secondary)
                ForEach(Array(s.warnings.prefix(2).enumerated()), id: \.offset) { _, w in
                    Text("· \(w)").font(.caption2).foregroundStyle(.orange)
                }
                if let a = assessment {
                    HStack(spacing: 6) {
                        Text(gradeBadge(a)).font(.caption.bold())
                        Text("등록 품질 \(a.score)점 · 면 \(a.sidesCovered)/4 · 밀도 \(Int(a.densityPerM2))/㎡")
                            .font(.caption2)
                    }
                }
                if let o = outline {
                    FourViewStrip(views: o.views ?? [:], coveredFlags: assessment?.sideCoveredFlags ?? [:])
                }
            } else if segFallback {
                Text("서버 분리 실패 — 반경 \(String(format: "%.2f", scopeRadius))m 안의 모든 점을 사용합니다 (배경이 섞일 수 있음)")
                    .font(.caption2).foregroundStyle(.orange)
            }

            HStack {
                Text("사물 범위 \(String(format: "%.2f", scopeRadius)) m").font(.caption2)
                Slider(value: $scopeRadius, in: 0.2...1.5, step: 0.05) { editing in
                    if !editing { Task { await runSegmentation() } }  // 손을 떼면 다시 분리
                }
            }
            HStack {
                Text("초록 선이 사물보다 넓으면 줄이고, 잘렸으면 늘리세요").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button { Task { await runSegmentation() } } label: { Label("다시 분리", systemImage: "arrow.clockwise") }
                    .font(.caption2).buttonStyle(.bordered).disabled(segBusy)
                Button { clearCenter() } label: { Label("대상 다시", systemImage: "scope") }
                    .font(.caption2).buttonStyle(.bordered)
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding(.horizontal, 10)
    }

    /// 분리 확인 단계의 "누락된 방향·추가 카메라 워크" 안내 — 둘레 지도와 서버 평가(면·밀도)를 합친다
    private var reviewAdvice: (title: String, detail: String, ok: Bool) {
        let cov = ar.coverage
        var needs: [String] = []
        let miss = cov.missingSectors.count
        if miss > 2 { needs.append("지도에서 회색인 방향 \(miss)곳이 남았습니다 — 그쪽으로 돌아 이동하며 비추세요") }
        if cov.topFraction < 0.15 { needs.append("윗면이 부족합니다 — 위에서 내려다보며 훑으세요") }
        if let a = assessment {
            for t in a.tips.prefix(2) where !needs.contains(t) { needs.append(t) }
            if a.sidesCovered < 3 && !needs.contains(where: { $0.contains("면") }) {
                needs.append("사물의 면이 \(a.sidesCovered)/4만 확보됐습니다 — 다른 쪽도 비추세요")
            }
        }
        if needs.isEmpty { return ("충분합니다", "모든 방향이 채워졌습니다 — 저장해도 좋습니다", true) }
        return ("추가 카메라 워크가 필요합니다 — [더 스캔]", needs.prefix(3).map { "· " + $0 }.joined(separator: "\n"), false)
    }

    private func sideLabel(_ s: String) -> String {
        ["front": "앞", "right": "오른쪽", "back": "뒤", "left": "왼쪽"][s] ?? s
    }

    private func gradeBadge(_ a: AssessmentJSON) -> String {
        switch a.grade {
        case "good": return "🟢 등록 품질 양호"
        case "fair": return "🟡 등록 품질 보통"
        default: return "🔴 등록 품질 부족"
        }
    }

    private func qualityBadge(_ q: String) -> String {
        switch q {
        case "good": return "🟢 분리 양호"
        case "fair": return "🟡 분리 보통"
        default: return "🔴 분리 불확실"
        }
    }

    // MARK: - 하단 버튼

    @ViewBuilder
    private var bottomActions: some View {
        switch phase {
        case .needSpace:
            Button { app.selectedTab = 0 } label: {
                Label("홈에서 공간 선택하기", systemImage: "building.2").frame(width: 240, height: 44)
            }
            .buttonStyle(.borderedProminent)

        case .aim:
            Button { setTarget() } label: {
                Label("대상 지정", systemImage: "scope").frame(width: 220, height: 46)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!ar.trackingNormal)

        case .scan:
            HStack(spacing: 10) {
                Button { withAnimation(.snappy) { showCalib.toggle() } } label: {
                    Label("중심 보정", systemImage: "scope").font(.footnote.weight(.semibold))
                }
                .buttonStyle(.bordered).tint(showCalib ? .orange : .blue)
                Button(role: .cancel) { clearCenter() } label: { Text("대상 다시").font(.footnote) }
                    .buttonStyle(.bordered)
                Button { requestReview() } label: {
                    Label("분리 확인", systemImage: "checkmark.viewfinder").frame(height: 30)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!ar.trackingNormal || !ar.coverage.canReview)
            }

        case .review:
            HStack(spacing: 12) {
                Button { backToScanning() } label: { Label("더 스캔", systemImage: "arrow.uturn.backward") }
                    .buttonStyle(.bordered)
                Button {
                    idFocused = false
                    if let a = assessment, a.sidesCovered < 3 || a.grade == "poor" || ar.coverage.azimuthFilled < 12 {
                        confirmLowQuality = true  // 한쪽 면 위주(또는 둘레를 반도 못 돈) 모델은 다른 방향 검수에서 안 맞는다 — 저장 전 확인
                    } else {
                        Task { await register() }
                    }
                } label: {
                    Text(busy ? "저장 중…" : "임시 설계 모델 저장").frame(width: 170, height: 44)
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || segBusy || assetId.isEmpty || !ar.trackingNormal || (seg == nil && !segFallback))
            }

        case .saved:
            savedPanel
        }
    }

    private var savedPanel: some View {
        VStack(spacing: 6) {
            Text("✓ '\(savedId ?? "")' 임시 설계 모델 저장됨").font(.footnote.bold()).foregroundStyle(.green)
            if let note = saveNote { Text(note).font(.caption2).foregroundStyle(.secondary).lineLimit(2) }
            // 사진 실루엣 썸네일 — 기본은 접어 둔다(카메라 시야를 가리지 않게). 펼치면 한 줄로 확인.
            if showCards {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 4) {
                        ForEach(PhotoCardBuilder.sides, id: \.self) { side in
                            ZStack {
                                RoundedRectangle(cornerRadius: 5).fill(Color(white: 0.12))
                                if let img = savedCards[side] {
                                    Image(uiImage: img).resizable().scaledToFit().padding(3)
                                } else {
                                    Text("없음").font(.system(size: 9)).foregroundStyle(.orange)
                                }
                            }
                            .frame(width: 44, height: 44)
                        }
                    }
                }
                .frame(height: 46)
            }
            Button { showCards.toggle() } label: {
                Label(showCards ? "실루엣 \(savedCards.count)컷 접기" : "실루엣 \(savedCards.count)컷 보기", systemImage: showCards ? "chevron.down" : "chevron.up")
                    .font(.caption2)
            }
            HStack(spacing: 12) {
                Button { resetForNewAsset(id: ScanAssetView.newAssetId()) } label: {
                    Label("새 사물 스캔", systemImage: "plus.viewfinder").frame(width: 130, height: 40)
                }
                .buttonStyle(.borderedProminent)
                Button { app.selectedTab = 2 } label: {
                    Label("③ 검수로 이동", systemImage: "scope").frame(width: 130, height: 40)
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(10).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }


    // MARK: - 상태 전환

    /// 대상 지정을 취소하고 처음(조준)으로 — 쌓인 스캔은 새 대상 주변만 남는다
    private func clearCenter() {
        center = nil; reviewing = false; seg = nil; mask = []; ctx = nil; segFallback = false; message = nil
        outline = nil; assessment = nil; refinedPoints = []; refinedNormals = []
        centerConfirmed = false; showCalib = false; autoCalib = true; assist = nil; thumb = nil
        rescan = nil; guide = nil; ringTarget = nil; live = nil; showPreReview = false
        lastLiveCount = 0; lastLiveAt = .distantPast
        ar.clearPreview()
        ar.clearTarget()
    }

    /// 분리 확인에서 부족하면 같은 대상 그대로 스캔을 이어 간다 (지도·점은 유지)
    private func backToScanning() {
        reviewing = false
        seg = nil; mask = []; ctx = nil; segFallback = false; outline = nil; assessment = nil
        refinedPoints = []; refinedNormals = []
        lastLiveCount = 0; lastLiveAt = .distantPast
        infoMessage = "지도에서 회색인 방향으로 이동하며 더 비춘 뒤 다시 [분리 확인]을 누르세요"
    }

    private func resetForNewAsset(id: String) {
        savedId = nil; saveNote = nil; infoMessage = nil; savedCards = [:]; showCards = false
        clearCenter()
        ar.resetScanData()  // 이전 사물의 특징점·키프레임이 남아 새 사물과 섞이지 않게
        assetId = id
    }

    /// 기본 ID는 매번 새로 만든다 — 같은 ID 재사용으로 좋은 모델이 덮어써지는 사고 방지
    static func newAssetId() -> String {
        let f = DateFormatter()
        f.dateFormat = "MMdd-HHmmss"
        return "SCAN-" + f.string(from: Date())
    }

    // MARK: - ② 대상 지정 → 둘레 스캔 → 분리 확인

    /// 스캔을 시작하기 전에 먼저 어떤 사물인지 정한다 — 이후 깊이 누적·둘레 지도·분리가 모두 이 중심을 기준으로 한다.
    private func setTarget() {
        message = nil; infoMessage = nil
        guard let hit = ar.centerHitPosition() else {
            message = ar.hasLiDAR
                ? "조준점의 깊이를 찾지 못했습니다 — 사물에서 0.3~2m 거리에서 조준하세요"
                : "조준점 근처 특징점이 부족합니다 — 텍스처가 있는 사물을 보며 천천히 움직인 뒤 다시 지정하세요"
            DebugLog.error("scan", "대상 지정 실패", extra: ["tracking": ar.statusText, "points": "\(ar.featurePointCount)"])
            return
        }
        var c = hit
        if let ax = ar.cameraAxes() { c = hit + ax.fwd * 0.04 }  // 표면 점이 아니라 사물 안쪽(4cm)에서 시작 — 자동 보정이 가운데로 당긴다
        center = c
        reviewing = false; autoCalib = true; centerConfirmed = false; showCalib = false
        lastLiveCount = 0; lastLiveAt = .distantPast
        ar.setTarget(c)
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        infoMessage = "대상을 지정했습니다 — 초록 마커가 사물 가운데인지 확인하고, 둘레를 천천히 돌며 비추세요"
        DebugLog.log("scan", "대상 지정 OK (\(c.x), \(c.y), \(c.z))",
                     extra: ["points": "\(ar.featurePointCount)", "lidar": "\(ar.hasLiDAR)"])
    }

    /// 둘레를 충분히 돌았으면 사물을 분리해 외곽선·품질을 확인한다
    private func confirmSeparation() async {
        DebugLog.log("scan", ar.coverage.summaryLine())  // 스캔을 떠나는 순간의 품질 요약 (실기기 테스트 디브리프용)
        rescan = nil; guide = nil; ringTarget = nil; live = nil
        infoMessage = nil
        reviewing = true
        showCalib = false
        await runSegmentation()
        DebugLog.log("scan", "분리 확인 단계",
                     extra: ["azimuth": "\(ar.coverage.azimuthFilled)/24", "top": String(format: "%.2f", ar.coverage.topFraction)])
    }


    /// POI 주변 점을 모아 서버에서 사물만 분리 → 초록 점 미리보기. 서버 실패 시 반경 절단으로 대체.
    private func runSegmentation() async {
        guard let poi = center, !segBusy else { return }
        segBusy = true
        message = nil
        defer { segBusy = false }

        let ctxRadius = min(2.0, max(0.6, scopeRadius * 1.6))  // 분리에 필요한 배경(지지면 등)까지 포함
        let c = ar.contextSample(center: poi, radius: ctxRadius)
        ctx = c
        guard c.points.count >= 30 else {
            seg = nil; mask = []; segFallback = false
            message = "중심 주변 점이 너무 적습니다(\(c.points.count)개) — 사물 주위를 더 스캔한 뒤 다시 지정하세요"
            DebugLog.error("scan", "분리 입력 부족", extra: ["points": "\(c.points.count)"])
            return
        }
        do {
            let out = try await app.api.segment(points: c.points, poi: poi, maxRadius: scopeRadius, refine: !c.isMesh)
            seg = out
            mask = out.maskArray()
            segFallback = false
            outline = out.outline
            assessment = out.assessment
            if let r = out.refined, r.count == out.nIn {
                refinedPoints = r.pointArray; refinedNormals = r.normalArray
            } else {
                refinedPoints = []; refinedNormals = []
            }
            if let o = out.outline {
                ar.showOutlinePreview(o)  // 점 조각이 아니라 외곽선(윤곽선+외곽 박스)으로 표시
            } else {
                ar.clearPreview()
            }
            DebugLog.log("scan", "분리 \(out.quality) 사물 \(out.nIn)/배경제거 \(out.nRemoved)",
                         extra: ["warnings": out.warnings.joined(separator: " | "),
                                 "support": "\(out.supportHeightM.map { String(format: "%.3f", $0) } ?? "-")",
                                 "mesh": "\(c.isMesh)", "scope": "\(scopeRadius)"])
        } catch {
            // 서버 분리 실패(네트워크 등): 반경 안의 점을 그대로 사용 (품질 낮음을 UI에 표시)
            seg = nil
            mask = c.points.map { simd_distance($0, poi) <= scopeRadius }
            segFallback = true
            outline = nil; assessment = nil; refinedPoints = []; refinedNormals = []
            ar.clearPreview()
            DebugLog.error("scan", "분리 실패→반경 절단 대체: \(error.localizedDescription)")
        }
        if let a = assessment {
            DebugLog.log("scan", "등록 품질 \(a.score)점 \(a.grade)",
                         extra: ["sides": a.sides.map { "\($0.key):\($0.value)" }.sorted().joined(separator: ","),
                                 "covered": "\(a.sidesCovered)", "azimuth": String(format: "%.2f", a.azimuthCoverage),
                                 "density": "\(Int(a.densityPerM2))", "noiseMm": a.noiseMm.map { String(format: "%.1f", $0) } ?? "-",
                                 "boxness": String(format: "%.2f", a.boxness), "refined": "\(!refinedPoints.isEmpty)",
                                 "tips": a.tips.joined(separator: " | ")])
        }
    }

    // MARK: - 저장

    private func register() async {
        guard let poi = center, let c = ctx, !assetId.isEmpty, mask.count == c.points.count else { return }
        guard app.isSpaceSelected else {
            message = "공간이 선택되지 않았습니다 — '공간' 탭에서 공간을 선택한 뒤 다시 시도하세요"
            DebugLog.error("scan", "등록 중단: 공간 미선택 space=\(app.spaceId)#\(app.spaceNumericId)")
            return
        }
        busy = true
        message = nil
        defer { busy = false }
        DebugLog.log("scan", "등록 시작 id=\(assetId) space=\(app.spaceId)#\(app.spaceNumericId)",
                     extra: ["featurePoints": "\(ar.featurePointCount)", "tracking": "\(ar.trackingNormal)"])

        // 사물 원점 = 분리된 사물의 AABB 중심 (서버 분리 실패 시 POI)
        let origin: SIMD3<Float> = seg.map { SIMD3(Float($0.center.x), Float($0.center.y), Float($0.center.z)) } ?? poi

        // 분리된 사물만 자산 로컬 프레임(원점 = 사물 중심)으로
        var positions: [SIMD3<Float>] = []
        var indices: [UInt32] = []
        var objectPoints: [SIMD3<Float>] = []  // 사물의 세션 좌표 점 — 사진 실루엣 마스크 계산에 쓴다
        if c.isMesh {
            for t in 0..<(c.triangles.count / 3) {
                let ia = Int(c.triangles[t * 3]), ib = Int(c.triangles[t * 3 + 1]), ic = Int(c.triangles[t * 3 + 2])
                guard mask[ia], mask[ib], mask[ic] else { continue }  // 세 정점이 모두 사물일 때만
                let base = UInt32(positions.count)
                positions.append(contentsOf: [c.points[ia] - origin, c.points[ib] - origin, c.points[ic] - origin])
                indices.append(contentsOf: [base, base + 1, base + 2])
            }
            guard indices.count / 3 >= 100 else {
                message = "사물 메시가 부족합니다(\(indices.count / 3)면) — 사물을 더 스캔하거나 범위를 조절하세요"
                return
            }
            objectPoints = positions.map { $0 + origin }
        } else {
            // 서버가 정제(스무딩+바깥쪽 법선)한 점이 있으면 그것으로, 없으면(서버 분리 실패 등) 원본 점+PCA 법선
            let useRefined = refinedPoints.count == mask.filter { $0 }.count && refinedPoints.count == refinedNormals.count
            let rel = (useRefined ? refinedPoints : zip(c.points, mask).filter { $0.1 }.map { $0.0 }).map { $0 - origin }
            guard rel.count >= 120 else {
                message = "사물 점이 부족합니다(\(rel.count)개) — 사물 주위를 더 스캔하거나 범위를 조절하세요 (최소 120)"
                DebugLog.error("scan", "등록 중단: 사물 점 부족", extra: ["kept": "\(rel.count)"])
                return
            }
            objectPoints = rel.map { $0 + origin }
            if useRefined {
                let nrm = refinedNormals
                let built = SurfelMesh.build(points: rel, normals: nrm, maxPoints: ar.hasLiDAR ? 12_000 : 3_500,
                                             size: ar.hasLiDAR ? 0.006 : 0.012)
                positions = built.positions
                indices = built.indices
            } else {
                let view = (ar.cameraPosition ?? origin + SIMD3(0, 0, 1)) - origin
                let built = await Task.detached { SurfelMesh.build(points: rel, viewpoint: view) }.value
                positions = built.positions
                indices = built.indices
            }
        }
        guard positions.count >= 3, indices.count >= 3 else {
            message = "모델을 만들지 못했습니다 — 사물을 더 스캔한 뒤 다시 시도하세요"
            return
        }

        let glb = GLBWriter.write(positions: positions, indices: indices, name: assetId)
        DebugLog.log("scan", "GLB 생성 \(glb.count)B tris=\(indices.count / 3) verts=\(positions.count)",
                     extra: ["assetId": assetId, "lidar": "\(ar.usesLiDARMesh)",
                             "segmented": "\(seg != nil)", "origin": "\(origin.x),\(origin.y),\(origin.z)"])
        let segNote = (seg.map { "분리 \($0.quality): 사물 \($0.nIn)점·배경 \($0.nRemoved)점 제거" } ?? "분리 없음(반경 절단)")
            + (assessment.map { " · 품질 \($0.score)점·면 \($0.sidesCovered)/4" } ?? "")
        let scanTime = Date()
        let loc = LocationProvider.shared.current  // 실내 등으로 위치가 없으면 nil — 등록은 계속
        DebugLog.log("scan", loc.map { String(format: "GPS %.6f,%.6f ±%.0fm", $0.coordinate.latitude, $0.coordinate.longitude, $0.horizontalAccuracy) }
                     ?? "GPS 없음(권한 거부/실내/측위 중)")
        let meta = APIClient.ScanAssetMeta(
            assetId: assetId, projectId: app.projectId, spaceId: app.spaceId,
            bimRevision: "GEN-\(Int(Date().timeIntervalSince1970))",
            family: family, type: "scanAsset",
            designPose: PoseJSON(positionM: Vec3JSON(x: Double(origin.x), y: Double(origin.y), z: Double(origin.z))),
            tolerance: nil,
            notes: (ar.hasLiDAR ? "iPhone LiDAR 깊이 융합 스캔" : "iPhone VIO 특징점 스캔(LiDAR 없음)") + " · " + segNote,
            deviceId: app.deviceId,
            scannedAt: ISO8601DateFormatter().string(from: scanTime),
            geo: loc.map { GeoJSON(lat: $0.coordinate.latitude, lon: $0.coordinate.longitude,
                                   altM: $0.verticalAccuracy >= 0 ? $0.altitude : nil,
                                   accuracyM: $0.horizontalAccuracy) }
        )
        do {
            let out = try await app.api.uploadScanAsset(meta: meta, glb: glb)
            // 월드맵 저장 (선택): 있으면 재방문 시 공간 프레임이 복원되어 위치 편차까지 판정.
            // 없어도 검수는 사물 앵커 정합으로 동작하므로 실패해도 등록은 성공으로 본다.
            var mapNote = "공간 월드맵도 저장됨"
            do {
                let map = try await ar.currentWorldMap()
                try await app.api.uploadWorldMap(spaceNumericId: app.spaceNumericId, data: map.serializedData())
            } catch {
                mapNote = "월드맵 없이 저장 — 검수는 사물 모양으로 맞춥니다"
                DebugLog.error("scan", "월드맵 저장 실패(등록은 성공): \(error.localizedDescription)")
            }
            // 방향별 사진 실루엣 카드 (실패해도 등록은 성공 — 카드는 검수 화면의 시각 보강이다)
            var photoNote = ""
            if let o = outline, objectPoints.count >= 30 {
                infoMessage = "흑백 실루엣 외곽선 만드는 중… (둘레 \(PhotoCardBuilder.count)컷)"
                let cc = o.obb.center
                let obbCenter = SIMD3<Float>(Float(cc[0]), Float(cc[1]), Float(cc[2]))
                let yaw = Float(o.obb.yawDeg)
                let step = max(1, objectPoints.count / 6000)
                let pts = stride(from: 0, to: objectPoints.count, by: step).map { objectPoints[$0] }
                let stampM: Float = ar.hasLiDAR ? 0.012 : 0.025  // LiDAR 점은 촘촘, VIO 특징점은 성기다
                savedCards = [:]
                var cards: [PhotoCard] = []
                for i in 0..<PhotoCardBuilder.count {
                    let card = await Task.detached {
                        PhotoCardBuilder.build(index: i, objectPoints: pts, obbCenter: obbCenter, yawDeg: yaw, origin: origin, stampM: stampM)
                    }.value
                    guard let card else {
                        DebugLog.error("scan", "실루엣 카드 생성 실패: \(PhotoCardBuilder.sides[i])", extra: ["points": "\(pts.count)"])
                        continue
                    }
                    if let img = UIImage(data: card.png) { savedCards[card.side] = img }
                    cards.append(card)
                }
                // 업로드는 6개씩 동시에
                var ok = 0
                let api = app.api, aid = out.assetId
                for chunk in stride(from: 0, to: cards.count, by: 6).map({ Array(cards[$0..<min($0 + 6, cards.count)]) }) {
                    ok += await withTaskGroup(of: Bool.self) { group -> Int in
                        for c in chunk {
                            group.addTask {
                                do { try await api.uploadPhotoCard(assetId: aid, card: c); return true }
                                catch {
                                    DebugLog.error("scan", "실루엣 카드 \(c.side) 업로드 실패: \(error.localizedDescription)")
                                    return false
                                }
                            }
                        }
                        var n = 0
                        for await r in group where r { n += 1 }
                        return n
                    }
                }
                let kb = cards.reduce(0) { $0 + $1.png.count } / 1024
                DebugLog.log("scan", "실루엣 카드 \(ok)/\(PhotoCardBuilder.count) 저장", extra: ["generated": "\(cards.count)", "totalKB": "\(kb)",
                              "sizesCm": cards.prefix(8).map { String(format: "%.0fx%.0f", $0.widthM * 100, $0.heightM * 100) }.joined(separator: ",")])
                photoNote = " · 실루엣 \(ok)/\(PhotoCardBuilder.count)컷"
                infoMessage = nil
            }
            DebugLog.log("scan", "등록 완료 \(out.assetId)", extra: ["map": mapNote, "photos": photoNote])
            savedId = out.assetId
            saveNote = "임시 설계 모델 \(indices.count / 3)면 · \(segNote) · \(mapNote)\(photoNote)"
            center = nil; seg = nil; mask = []; ctx = nil; outline = nil; assessment = nil
            refinedPoints = []; refinedNormals = []
            reviewing = false; centerConfirmed = false; showCalib = false; assist = nil; thumb = nil
            ar.clearPreview()
            ar.clearTarget()
        } catch {
            DebugLog.error("scan", "등록 실패: \(error.localizedDescription)")
            message = "등록 실패: \(error.localizedDescription)"
        }
    }
}

