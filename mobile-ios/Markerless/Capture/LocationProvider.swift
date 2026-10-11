// LocationProvider.swift — 자산 스캔 위치(GPS). 사용 중 권한만 요청하고, 실내라 위치가 없으면 nil로 둔다 (등록은 막지 않음).

import CoreLocation
import Foundation

@MainActor
final class LocationProvider: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = LocationProvider()

    @Published private(set) var last: CLLocation?
    @Published private(set) var denied = false
    private let mgr = CLLocationManager()

    override init() {
        super.init()
        mgr.delegate = self
        mgr.desiredAccuracy = kCLLocationAccuracyBest
        mgr.distanceFilter = 5
    }

    /// 스캔 화면 진입 시 호출 — 권한 요청 + 위치 갱신 시작
    func start() {
        switch mgr.authorizationStatus {
        case .notDetermined: mgr.requestWhenInUseAuthorization()
        case .denied, .restricted: denied = true; return
        default: break
        }
        mgr.startUpdatingLocation()
    }

    func stop() { mgr.stopUpdatingLocation() }

    /// 등록에 쓸 현재 위치 (오래됐거나 정확도 불량이면 nil)
    var current: CLLocation? {
        guard let l = last, l.horizontalAccuracy >= 0, l.horizontalAccuracy <= 200,
              abs(l.timestamp.timeIntervalSinceNow) < 120 else { return nil }
        return l
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let l = locations.last else { return }
        Task { @MainActor in self.last = l }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            let s = manager.authorizationStatus
            self.denied = (s == .denied || s == .restricted)
            if s == .authorizedWhenInUse || s == .authorizedAlways { manager.startUpdatingLocation() }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}
}
