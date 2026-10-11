// MarkerlessApp.swift — 앱 진입점 (개발지시서 PoC iOS 클라이언트)

import SwiftUI

@main
struct MarkerlessApp: App {
    @StateObject private var appState = AppState()
    @Environment(\.scenePhase) private var scenePhase

    init() { DebugLog.install() }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appState)
                .onAppear { appState.startSync() }
                .onChange(of: scenePhase) { _, phase in
                    // 앱이 켜져 있는 동안만 변경 이벤트 연결을 유지한다. 돌아오면 다시 연결하고(연결 직후 한 번 따라잡는다).
                    if phase == .active { appState.startSync() } else { appState.stopSync() }
                }
        }
    }
}
