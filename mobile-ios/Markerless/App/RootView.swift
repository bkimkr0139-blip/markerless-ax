// RootView.swift — 작업 순서 = 탭 순서: ① 공간 선택 → ② 스캔·등록(임시 설계 모델 만들기) → ③ 검수(저장 모델에 맞추기)

import SwiftUI

struct RootView: View {
    @EnvironmentObject var app: AppState

    var body: some View {
        TabView(selection: $app.selectedTab) {
            NavigationStack { SpacesView() }
                .tabItem { Label("홈", systemImage: "house.fill") }
                .tag(0)

            NavigationStack {
                ScanHubView()
                    .navigationTitle("② 스캔·등록")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbarBackground(.visible, for: .navigationBar)
            }
            .tabItem { Label("② 스캔·등록", systemImage: "cube.transparent") }
            .tag(1)

            NavigationStack {
                InspectionView()
                    .navigationTitle("③ 검수")
                    .navigationBarTitleDisplayMode(.inline)
            }
            .tabItem { Label("③ 검수", systemImage: "scope") }
            .tag(2)
        }
        .task {  // 공간 스캔 업로드 큐: 서버 설정 연결 + 앱 재시작 후 이어서 올리기
            FrameUploader.shared.apiProvider = { [weak app] in app?.api ?? APIClient(baseURL: nil) }
            FrameUploader.shared.resumePending()
        }
        .overlay(alignment: .topLeading) {  // 설치된 앱이 최신 빌드인지 한눈에 — 모든 화면 왼쪽 위
            Text("\(BuildInfo.number)")
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.9))
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(.black.opacity(0.45), in: Capsule())
                .padding(.leading, 6).padding(.top, 2)
                .allowsHitTesting(false)
        }
    }
}
