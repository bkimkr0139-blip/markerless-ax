# HANDOFF.md — 재부팅 후 작업 복귀 가이드

작성: 2026-10-06 (macOS 26.6+ 업그레이드 + Xcode 설치를 위한 재부팅 전 체크포인트)
갱신: 2026-10-06 (재부팅 복귀 완료 — Xcode 27 설치, iOS 기기 빌드 컴파일 성공)
갱신: 2026-10-07 (**sudo 단계 전부 완료 확인** — 남은 것은 Xcode GUI 서명 + 실기기 실행뿐, §2)
이 문서 하나면 어느 세션에서든 작업을 그대로 이어갈 수 있다.

## 1. 현재 상태 (2026-10-06 재부팅 복귀 후)

**PoC 코드 전체 완성·검증 완료. iOS 앱 컴파일도 성공. 남은 것 = 코드 서명 + iPhone 실기기 실행뿐.**

| 항목 | 상태 |
|------|------|
| spatial-core + backend | ✅ 완성, **테스트 73개(46+27) 전부 통과** (업그레이드 후 재검증 완료) |
| iOS 앱 (mobile-ios/) | ✅ 소스 12개, **`xcodebuild -sdk iphoneos` BUILD SUCCEEDED** — 창작 API 15건 수정 완료(§3) |
| Xcode | ✅ **27.0 (27A266a)** 설치 + `xcode-select -p` 전환·라이선스 수락(`-checkFirstLaunchStatus` OK) **완료** — 2026-10-07 확인 |
| 문서 (docs/) | ✅ DESIGN·COORDINATE·API·REPORT 전부 작성 |
| 스텁 | ✅ mobile-android·revit-addon·agent·test-data·ground-truth |
| 실HTTP E2E | ✅ PASS/FAIL/RETAKE 3케이스 검증 (test-data/ 픽스처) |
| ICP | ✅ 2단(p2p coarse → point-to-plane fine) — 접선 미끄럼 해결, 측정 1회 1.12초 |
| git | ⚠️ **커밋 0개, 전부 Untracked** — 체크포인트 커밋 권장 |
| 백엔드 서버 | `make api`로 재기동 가능 (8111 포트, healthz 확인됨) |

## 2. 다음 절차 (코드 서명 + 실기기 실행) — 2026-10-07 갱신

**sudo 작업은 전부 완료됨. 이하는 전부 Xcode GUI + iPhone 작업.**

```bash
# (완료 2026-10-07) sudo 단계 — 재확인 불필요:
#   xcode-select -p → /Applications/Xcode.app/Contents/Developer
#   xcodebuild -checkFirstLaunchStatus → OK
#   xcodebuild -version → Xcode 27.0 (27A266a)

# 0) iOS 서명 없이 컴파일 회귀 점검 (선택 — 이전에 통과)
cd ~/works/bimcad/markerless-poc/mobile-ios
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project Markerless.xcodeproj -scheme Markerless -sdk iphoneos \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build

# 1) Xcode에서 서명 + 기기 설치 (아래 상세 절차)
open Markerless.xcodeproj

# 참고) 백엔드 기동: 저장소 루트에서 make api → curl localhost:8111/healthz
# 참고) 무결성: make test → 73 passed
# 참고) Mac IP 확인: ipconfig getifaddr en0
```

### Xcode GUI 상세 (타깃은 Markerless 하나뿐)

1. **Apple ID 등록** (미등록 시): Xcode → Settings…(⌘,) → Accounts → 좌하단 + → Apple ID 로그인
   → 목록에 "Personal Team (본인이름)" 보이면 완료
2. **서명 설정 ★핵심★**: 좌측 최상단 파란 `Markerless` 프로젝트 클릭 → 가운데 **TARGETS → Markerless**
   (PROJECT 쪽 아님) → **Signing & Capabilities** 탭 →
   ☑ Automatically manage signing → **Team**에서 본인 Apple ID(Personal Team) 선택
   - 프로젝트에 DEVELOPMENT_TEAM이 비어 있고 `CODE_SIGN_IDENTITY = "iPhone Developer"`만 있음 — 이 단계가 필수
   - 빨간 에러 "bundle ID … is not available" → `kr.poc.markerless.Markerless` 끝에 접미사 붙여 고유화
3. **iPhone 준비** (LiDAR 탑재 모델):
   - 설정 → 개인정보 보호 및 보안 → **개발자 모드** 켬 → 재부팅 (이 메뉴는 Mac에 1회 연결해야 생길 수 있음)
   - USB 연결 → iPhone에서 "이 컴퓨터 신뢰" → Xcode ⇧⌘2 (Devices and Simulators)에서
     *Preparing device for development* 완료까지 대기 (첫 연결은 수 분)
4. **실행**: 툴바 실행 대상에서 **실기기 iPhone** 선택 (Simulator 아님, 이름에 "(unavailable)" 있으면 미준비) → **⌘R**
   - 첫 빌드는 서명 준비 때문에 몇 분 소요 가능
5. **iPhone 첫 실행**: 앱 탭 → "신뢰할 수 없는 개발자" 팝업 → 취소 후
   설정 → 일반 → VPN 및 기기관리 → 본인 Apple ID → **"Markerless" 신뢰** → 앱 재실행 → 카메라 권한 허용
   - 개인 무료 Team 서명은 **7일 만료** → 만료 시 ⌘R 재빌드로 재설치
6. **백엔드 연결**: Mac에서 `make api` → iPhone과 같은 Wi-Fi → 앱 첫 탭 "공간"에서
   서버 주소를 `http://<Mac-IP>:8111` 로 변경

### 자주 나오는 에러 → 처치

| 에러 | 처치 |
|------|------|
| `Unable to install "Markerless"` | Team 비었는지 확인, bundle ID 변경 |
| `device is not registered in your provisioning profile` | 개발자 모드·기기 준비 완료 확인 후 ⌘R 재시도 |
| `Failed to prepare device for software development` | iPhone 재부팅 + 케이블 재연결 |
| `A valid provisioning profile … was not found` | 7일 만료 → ⌘R 재빌드 |
| 앱 켜지는데 서버 연결 실패 | 같은 Wi-Fi·`make api` 실행 중·서버 주소에 `http://` 포함 확인 |

## 3. iOS 빌드 수정 이력 (2026-10-06, swiftc -parse만 검증된 소스 → 실빌드로 잡은 것)

swiftc -parse는 타입 검사를 안 하므로, 처음 실빌드에서 **존재하지 않는 API(창작)가 다수 발견**됐다.
SDK 헤더(`/Applications/Xcode.app/.../ARKit.framework/Headers`)·swiftinterface 대조로 확정 후 수정:

| 위치 | 원래 코드 (존재하지 않는 API) | 수정 |
|------|------|------|
| ARScanView | `material.faceCulling = .none` | `if #available(iOS 18.0, *)` 가드 |
| ARScanView | `try await session.getCurrentWorldMap()` | completion API를 `withCheckedThrowingContinuation` 래핑 |
| ARScanView | `makeRaycastQuery(allowing: .existingSceneGeometry)` | ARKit 메시와의 직접 교차(몰러–트럼보어)로 대체 — 사물 표면 지정에 더 정확 |
| ARScanView | `geo.vertices[i]`, `geo.triangles[i].a` | `MTLBuffer` 직독 확장 `simd3Array()`/`int32Array()` + `.faces` |
| ARScanView | `MeshDescriptor(positions:normals:)` | `MeshDescriptor(name:)` + `.positions/.normals = MeshBuffer(...)` |
| ARScanView | `self.init(...) * quat` 체인 | 변수 q에 조립 후 `init(ix:iy:iz:r:)` |
| InspectionView | `ARWorldMap(data:)` | `ARWorldMap.deserialize(from:)` (NSKeyedUnarchiver) — import ARKit 추가 |
| ScanAssetView | `map.serializedData()` | ARWorldMap 확장 (NSKeyedArchiver) — import simd 추가 |
| GLBReader | 중첩 unsafe 클로저 (컴파일러 진단 크래시) | 바이트 직접 합성 LE 파서 |

## 4. 남은 작업 목록 (우선순위)

1. **코드 서명 + iPhone 실기기 실행** (위 2절) — 유일한 미검증 구간
2. iPhone에서 PoC 시나리오: 공간 생성 → 사물 스캔·자산 등록 → 재스캔 매칭·오버레이·측정 판정
   (상세 시나리오: `mobile-ios/README.md` 3절)
3. 런타임 문제 대응: 컴파일은 통과였으나 화면 흐름·AR 세션은 미검증 — 오류 메시지 기반 수정
4. 이후: Ground Truth 캠페인, ARCore 클라이언트 — `docs/REPORT.md` 8절

## 5. 핵심 파일 지도

```
docs/REPORT.md          ← 스프린트 총보고 (지금까지 뭘 했는지 전부)
docs/COORDINATE.md      ← 좌표 규약 (수정 시 필독)
docs/API.md             ← 백엔드 REST 규격
mobile-ios/README.md    ← iPhone 빌드·테스트 시나리오
test-data/README.md     ← 검증된 E2E 픽스처 (재현 결과 포함)
Makefile                ← make setup / test / api (8111 포트)
```
