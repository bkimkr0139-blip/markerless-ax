// DebugLog.swift — 개발 폐루프: 앱 이벤트/오류를 서버(backend/logs/dev.log)로 전송
//
// DEBUG 빌드에서만 전송한다 (Release에선 os.Logger만). 전송은 fire-and-forget이고 실패해도 앱 동작에 영향 없음.
// 치명 예외(NSException)는 UserDefaults에 저장했다가 다음 실행 때 전송한다. 시그널 크래시(.ips)는
// tools/devloop.sh crashes 로 기기에서 직접 수집한다.

import Foundation
import os
import UIKit

enum DebugLog {
    private static let logger = Logger(subsystem: "kr.poc.markerless", category: "app")
    private static let session = String(UUID().uuidString.prefix(8))
    private static let pendingKey = "debuglog.pendingCrash"

    static func log(_ tag: String, _ message: String, level: String = "INFO", extra: [String: String]? = nil) {
        logger.log("[\(tag, privacy: .public)] \(message, privacy: .public)")
        #if DEBUG
        send(level: level, tag: tag, message: message, extra: extra)
        #endif
    }

    static func error(_ tag: String, _ message: String, extra: [String: String]? = nil) {
        log(tag, message, level: "ERROR", extra: extra)
    }

    /// 앱 시작 시 1회: 이전 실행의 치명 예외 전송 + 예외 핸들러 설치
    static func install() {
        #if DEBUG
        if let pending = UserDefaults.standard.string(forKey: pendingKey) {
            UserDefaults.standard.removeObject(forKey: pendingKey)
            send(level: "FATAL", tag: "crash", message: "이전 실행 치명 예외", extra: ["detail": pending])
        }
        NSSetUncaughtExceptionHandler { exc in
            let detail = "\(exc.name.rawValue): \(exc.reason ?? "-")\n" + exc.callStackSymbols.prefix(20).joined(separator: "\n")
            UserDefaults.standard.set(detail, forKey: "debuglog.pendingCrash")
        }
        let info = Bundle.main.infoDictionary
        log("app", "launch",
            extra: ["version": "\(info?["CFBundleShortVersionString"] ?? "?")(\(info?["CFBundleVersion"] ?? "?"))", "buildInfo": BuildInfo.label + " " + BuildInfo.time,
                    "os": UIDevice.current.systemVersion])
        #endif
    }

    #if DEBUG
    private static func send(level: String, tag: String, message: String, extra: [String: String]?) {
        guard let base = AppState.normalizedURL(UserDefaults.standard.string(forKey: "baseURL") ?? "") else { return }
        var req = URLRequest(url: base.appendingPathComponent("api/v1/debug/log"))
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let key = UserDefaults.standard.string(forKey: "apiKey"), !key.isEmpty {
            req.setValue(key.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-API-Key")
        }
        var payload: [String: Any] = [
            "level": level, "tag": tag, "message": message,
            "device": UIDevice.current.name, "session": session,
            "clientTs": ISO8601DateFormatter().string(from: Date()),
        ]
        var ex = extra ?? [:]
        ex["build"] = BuildInfo.number  // 로그만 봐도 어느 빌드가 보냈는지 알 수 있다
        payload["extra"] = ex
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        Task.detached { _ = try? await URLSession.shared.data(for: req) }
    }
    #endif
}
