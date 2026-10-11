// SpacesView.swift — 홈: 서버 연결 상태 · 현재 공간 · 바로가기 · 공간 선택 (PoC 흐름의 시작점)

import SwiftUI

struct SpacesView: View {
    @EnvironmentObject var app: AppState
    @State private var spaces: [SpaceOut] = []
    @State private var newSpaceId = ""
    @State private var status: String?
    @State private var loading = false
    @State private var connected = false
    @State private var showServer = false
    @State private var adding = false
    @State private var scan3D: SpaceCoverageOutJSON?  // 선택한 공간의 3D 스캔 상태 (없거나 서버 미지원이면 nil)

    private let accent = LinearGradient(colors: [Color(red: 0.25, green: 0.62, blue: 1), Color(red: 0.4, green: 0.36, blue: 0.95)],
                                        startPoint: .topLeading, endPoint: .bottomTrailing)

    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                header
                if let notice = app.syncNotice { noticeCard(notice) }
                currentSpaceCard
                quickActions
                spaceList
                serverCard
                footer
            }
            .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 28)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(background.ignoresSafeArea())
        .toolbar(.hidden, for: .navigationBar)
        .task { await reload() }
        .onChange(of: app.dataRevision) { _, _ in Task { await reload() } }  // 관리자가 공간을 추가·수정·삭제하면 바로 반영
    }

    // MARK: - 배경 · 머리

    private var background: some View {
        ZStack(alignment: .top) {
            Color(.systemBackground)
            LinearGradient(colors: [Color.blue.opacity(0.22), Color.purple.opacity(0.10), .clear], startPoint: .top, endPoint: .center)
                .frame(height: 380)
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 13, style: .continuous).fill(accent)
                Image(systemName: "cube.transparent").font(.system(size: 22, weight: .semibold)).foregroundStyle(.white)
            }
            .frame(width: 46, height: 46).shadow(color: .blue.opacity(0.35), radius: 8, y: 3)
            VStack(alignment: .leading, spacing: 2) {
                Text("Markerless Inspect").font(.title3.weight(.bold))
                Text("마커 없이 CAD·BIM 대비 현장 검수").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            connectionPill
        }
        .padding(.top, 22)
    }

    private var connectionPill: some View {
        Button { testConnection() } label: {
            HStack(spacing: 6) {
                if loading { ProgressView().scaleEffect(0.6).frame(width: 8, height: 8) }
                else { Circle().fill(connected ? Color.green : Color.orange).frame(width: 8, height: 8) }
                Text(connected ? "연결됨" : "연결 안 됨").font(.caption.weight(.semibold))
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(.thinMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(connected ? "서버 연결됨, 누르면 다시 확인" : "서버 연결 안 됨, 누르면 다시 확인")
    }

    private func noticeCard(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            Text(text).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
            Button { app.syncNotice = nil } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
        }
        .padding(12)
        .background(Color.orange.opacity(0.14), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.orange.opacity(0.35)))
    }

    // MARK: - 현재 공간

    private var currentSpaceCard: some View {
        let selected = spaces.first { $0.id == app.spaceNumericId }
        return VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("현재 공간").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.75))
                Spacer()
                if selected?.hasWorldMap == true {
                    Label("월드맵", systemImage: "map.fill").font(.caption2.weight(.semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(.white.opacity(0.22), in: Capsule())
                }
            }
            if app.isSpaceSelected {
                VStack(alignment: .leading, spacing: 3) {
                    Text(app.spaceId).font(.system(size: 30, weight: .bold, design: .rounded)).lineLimit(1).minimumScaleFactor(0.6)
                    if !app.spaceName.isEmpty { Text(app.spaceName).font(.subheadline).foregroundStyle(.white.opacity(0.85)) }
                }
                HStack(spacing: 18) {
                    stat("cube", "\(app.assetNames.count)", "등록 자산")
                    if let l = selected?.level, !l.isEmpty { stat("square.3.layers.3d", l, "층") }
                    stat(selected?.hasWorldMap == true ? "checkmark.seal.fill" : "location.slash",
                         selected?.hasWorldMap == true ? "있음" : "없음", "공간 지도")
                }
                Label(scan3DText, systemImage: "cube.transparent.fill").font(.caption).foregroundStyle(.white.opacity(0.9))
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text(connected ? "공간을 선택하세요" : "서버에 연결하세요").font(.title2.weight(.bold))
                    Text(connected ? "아래 목록에서 작업할 현장 구역을 고르거나 새로 만드세요" : "아래 [서버]에서 주소를 확인하고 연결을 테스트하세요")
                        .font(.footnote).foregroundStyle(.white.opacity(0.85))
                }
            }
        }
        .foregroundStyle(.white)
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(accent, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(alignment: .topTrailing) {
            Image(systemName: "building.2.fill").font(.system(size: 84)).foregroundStyle(.white.opacity(0.08)).offset(x: 8, y: 30).clipped()
        }
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .shadow(color: .blue.opacity(0.3), radius: 16, y: 8)
    }

    private func stat(_ symbol: String, _ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                Image(systemName: symbol).font(.caption.weight(.semibold))
                Text(value).font(.headline.weight(.bold)).lineLimit(1)
            }
            Text(label).font(.caption2).foregroundStyle(.white.opacity(0.75))
        }
    }

    // MARK: - 바로가기

    private var quickActions: some View {
        HStack(spacing: 12) {
            actionTile("cube.transparent", "스캔·등록", "사물을 스캔해 자산으로", .teal) { app.selectedTab = 1 }
            actionTile("scope", "검수", "등록 자산과 맞춰 판정", .indigo) { app.selectedTab = 2 }
        }
        .opacity(app.isSpaceSelected ? 1 : 0.45)
        .disabled(!app.isSpaceSelected)
    }

    private func actionTile(_ symbol: String, _ title: String, _ sub: String, _ tint: Color, action: @escaping () -> Void) -> some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            action()
        } label: {
            VStack(alignment: .leading, spacing: 10) {
                Image(systemName: symbol).font(.system(size: 22, weight: .semibold)).foregroundStyle(tint)
                    .frame(width: 44, height: 44).background(tint.opacity(0.15), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline).foregroundStyle(.primary)
                    Text(sub).font(.caption).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.8)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(14)
            .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.primary.opacity(0.06)))
        }
        .buttonStyle(.plain)
    }

    // MARK: - 공간 목록

    private var spaceList: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("공간").font(.headline)
                Text("\(spaces.count)").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    .padding(.horizontal, 7).padding(.vertical, 2).background(Color(.tertiarySystemFill), in: Capsule())
                Spacer()
                Button { withAnimation(.snappy) { adding.toggle() } } label: {
                    Label(adding ? "닫기" : "새 공간", systemImage: adding ? "xmark" : "plus").font(.footnote.weight(.semibold))
                }
            }
            if adding {
                HStack(spacing: 8) {
                    TextField("새 공간 ID (예: MECH-301)", text: $newSpaceId)
                        .autocorrectionDisabled().textInputAutocapitalization(.never)
                        .padding(.horizontal, 12).frame(height: 44)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    Button { createSpace() } label: {
                        Image(systemName: "checkmark").font(.system(size: 17, weight: .bold)).foregroundStyle(.white)
                            .frame(width: 44, height: 44).background(accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                    .disabled(newSpaceId.isEmpty).opacity(newSpaceId.isEmpty ? 0.4 : 1)
                    .accessibilityLabel("공간 추가")
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
            if spaces.isEmpty {
                Text(connected ? "등록된 공간이 없습니다 — [새 공간]으로 만드세요" : "서버에 연결되면 공간 목록이 표시됩니다")
                    .font(.footnote).foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 22)
                    .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(spaces.enumerated()), id: \.element.id) { i, s in
                        spaceRow(s)
                        if i < spaces.count - 1 { Divider().padding(.leading, 60) }
                    }
                }
                .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.primary.opacity(0.06)))
            }
        }
    }

    private func spaceRow(_ s: SpaceOut) -> some View {
        let on = s.id == app.spaceNumericId
        return Button {
            UISelectionFeedbackGenerator().selectionChanged()
            withAnimation(.snappy) { app.spaceId = s.code; app.spaceNumericId = s.id; app.spaceName = s.name ?? "" }
            DebugLog.log("space", "공간 선택 \(s.code)#\(s.id)")
            Task { await app.syncOnce(force: true) }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: on ? "checkmark" : "building.2")
                    .font(.system(size: 16, weight: .semibold)).foregroundStyle(on ? Color.white : Color.blue)
                    .frame(width: 36, height: 36)
                    .background(on ? AnyShapeStyle(accent) : AnyShapeStyle(Color.blue.opacity(0.12)), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(s.code).font(.body.weight(.semibold)).foregroundStyle(.primary)
                    if let name = s.name, !name.isEmpty { Text(name).font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                if s.hasWorldMap {
                    Image(systemName: "map.fill").font(.caption).foregroundStyle(.green)
                        .accessibilityLabel("월드맵 있음")
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: - 서버 설정 (접이식)

    private var serverCard: some View {
        VStack(spacing: 0) {
            Button { withAnimation(.snappy) { showServer.toggle() } } label: {
                HStack(spacing: 12) {
                    Image(systemName: "server.rack").font(.system(size: 16, weight: .semibold)).foregroundStyle(.gray)
                        .frame(width: 36, height: 36).background(Color.gray.opacity(0.14), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("서버").font(.body.weight(.semibold)).foregroundStyle(.primary)
                        Text(app.baseURLString.isEmpty ? "주소 미설정" : app.baseURLString).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: "chevron.down").font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
                        .rotationEffect(.degrees(showServer ? 180 : 0))
                }
                .padding(12).contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showServer {
                VStack(spacing: 10) {
                    field("http://192.168.0.10:8111", text: $app.baseURLString, keyboard: .URL)
                    SecureField("API 키 (외부 URL 접속 시 필요, 로컬은 비움)", text: $app.apiKey)
                        .autocorrectionDisabled().textInputAutocapitalization(.never)
                        .padding(.horizontal, 12).frame(height: 44)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    field("프로젝트 코드", text: $app.projectId)
                    if !DevDefaults.apiKey.isEmpty {  // 배포 때 주입된 접속값 — 긴 주소/키를 손으로 치지 않게
                        HStack(spacing: 8) {
                            if !DevDefaults.tunnelURL.isEmpty {
                                chipButton("globe", "외부 URL") {
                                    app.baseURLString = DevDefaults.tunnelURL; app.apiKey = DevDefaults.apiKey
                                    DebugLog.log("space", "외부 URL 설정"); testConnection()
                                }
                            }
                            if !DevDefaults.localURL.isEmpty {
                                chipButton("wifi", "같은 Wi-Fi") {
                                    app.baseURLString = DevDefaults.localURL; app.apiKey = DevDefaults.apiKey
                                    DebugLog.log("space", "로컬 URL 설정"); testConnection()
                                }
                            }
                        }
                    }
                    Button { testConnection() } label: {
                        HStack(spacing: 8) {
                            if loading { ProgressView().tint(.white) } else { Image(systemName: "bolt.horizontal.fill") }
                            Text("연결 테스트").font(.subheadline.weight(.semibold))
                        }
                        .foregroundStyle(.white).frame(maxWidth: .infinity).frame(height: 44)
                        .background(accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                    if let status {
                        Text(status).font(.footnote).foregroundStyle(status.contains("OK") ? Color.green : Color.orange)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.horizontal, 12).padding(.bottom, 12)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.primary.opacity(0.06)))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func field(_ placeholder: String, text: Binding<String>, keyboard: UIKeyboardType = .default) -> some View {
        TextField(placeholder, text: text)
            .keyboardType(keyboard).autocorrectionDisabled().textInputAutocapitalization(.never)
            .padding(.horizontal, 12).frame(height: 44)
            .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func chipButton(_ symbol: String, _ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol).font(.footnote.weight(.semibold)).lineLimit(1)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Color.blue.opacity(0.12), in: Capsule()).foregroundStyle(Color.blue)
        }
    }

    private var footer: some View {
        VStack(spacing: 2) {
            Text("빌드 \(BuildInfo.number)").font(.caption2.monospaced())
            Text(BuildInfo.time).font(.caption2)
        }
        .foregroundStyle(.tertiary).padding(.top, 4)
    }

    // MARK: - 동작

    /// "3D 공간 스캔 상태" 한 줄 — GET /spaces/{id}/coverage (404/오류면 '스캔 없음')
    private var scan3DText: String {
        guard let c = scan3D, c.overall != nil || c.quality != nil else { return "3D 공간 스캔: 아직 없음" }
        var parts: [String] = []
        if let o = c.overall { parts.append("커버리지 \(Int((o * 100).rounded()))%") }
        if let q = c.quality { parts.append("품질 \(q.display)") }
        if let u = c.unobservedAreaM2 { parts.append("미관측 \(String(format: "%.1f", u))㎡") }
        return "3D 공간 스캔: " + parts.joined(separator: " · ")
    }

    private func reload() async {
        if app.isSpaceSelected { scan3D = (try? await app.api.spaceCoverage(spaceNumericId: app.spaceNumericId)) ?? nil } else { scan3D = nil }
        do { spaces = try await app.api.spaces(projectId: app.projectId); connected = true }
        catch {
            connected = false
            DebugLog.error("space", "공간 조회 실패: \(error.localizedDescription)")
            status = "조회 실패: \(error.localizedDescription)"
        }
    }

    private func testConnection() {
        status = nil
        loading = true
        Task {
            defer { loading = false }
            do {
                _ = try await app.api.health()
                connected = true
                status = "서버 연결 OK ✓"
                await reload()
            } catch {
                connected = false
                status = "연결 실패: \(error.localizedDescription)\n같은 Wi-Fi인지, Mac의 IP:포트를 확인하세요."
                showServer = true
            }
        }
    }

    private func createSpace() {
        Task {
            do {
                let s = try await app.api.createSpace(projectId: app.projectId, spaceId: newSpaceId, name: nil)
                app.spaceId = s.code
                app.spaceNumericId = s.id
                newSpaceId = ""
                withAnimation(.snappy) { adding = false }
                DebugLog.log("space", "공간 생성·선택 \(s.code)#\(s.id)")
                status = "공간 생성: \(s.code) — 이 공간이 스캔 기준(공간 프레임)이 됩니다"
                await reload()
            } catch {
                DebugLog.error("space", "공간 생성 실패: \(error.localizedDescription)")
                status = "생성 실패: \(error.localizedDescription)"
                showServer = true
            }
        }
    }
}
