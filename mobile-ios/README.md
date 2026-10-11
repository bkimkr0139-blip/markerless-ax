# Markerless iOS 클라이언트 (PoC)

iPhone(LiDAR 탑재 Pro 권장, LiDAR 없는 기기는 카메라+IMU VIO 모드)에서 사물을 스캔해 **자가 BIM/CAD 자산을 생성**하고,
같은 사물을 다시 스캔하면 **저장된 설계 모델과 매칭(ICP)해 AR 오버레이 + 시공검수 판정**을 보여주는 앱.

## 사전 준비

| 항목 | 요구 |
|------|------|
| Mac | Xcode 15 이상 설치 ([App Store](https://apps.apple.com/app/xcode/id497799835) — 본 PoC에서 설치 페이지가 열려 있음, GUI에서 설치 클릭 필요) |
| iPhone | iOS 17+. **LiDAR 탑재(Pro 계열)** 면 메시 모드, **LiDAR 없는 기기(예: iPhone Air)** 는 카메라+IMU VIO 특징점 모드로 자동 전환 |
| 프로젝트 생성 | 이 폴더에서 `xcodegen generate` → `Markerless.xcodeproj` 생성 |
| 네트워크 | iPhone과 Mac Studio가 **같은 Wi-Fi** |

## 빌드·실행

```bash
cd mobile-ios
xcodegen generate
open Markerless.xcodeproj
```

1. Xcode에서 **Markerless 타깃 → Signing & Capabilities → Team** 선택 (무료 개인 Apple ID 가능)
2. iPhone을 Mac에 연결 → 기기 선택 → Run(⌘R)
   - 최초 실행 시 iPhone: 설정 → 일반 → VPN·기기 관리에서 개발자 앱 신뢰
3. 앱 첫 탭 "공간"에서 **서버 주소**를 Mac의 IP로 변경 (예: `http://192.168.0.10:8111`)
   - Mac IP 확인: `ipconfig getifaddr en0`
   - 서버 실행: 저장소 루트에서 `make api` (백엔드 기동, 8111 포트)

## PoC 테스트 시나리오 (지시서: 스캔 → 자산화 → 재스캔 매칭)

1. **공간 탭** — 연결 테스트 → 새 공간 생성 (예: `MECH-301`). 이 공간이 스캔 기준(공간 프레임)이 됩니다.
2. **자산 등록 탭** — 검수할 사물(펌프, 밸브, 기둥 등 0.05~10m) 주위를 천천히 한 바퀴 스캔 →
   조준점(초록 원)을 사물 중심에 맞추고 **① 중심 지정** → 자산 ID 입력 후 **② 자산 등록**.
   - 서버가 GLB를 저장하고(자가 BIM/CAD), 자산의 designPose = 탭한 중심
   - 세션 월드맵도 함께 저장 → 다음 방문에서 공간 프레임 자동 복원
3. **검수 탭** — 자산 선택 → 사물을 비추면 **파랑 반투명 설계 모델 오버레이**가 뜹니다
   (월드맵 복원 성공 시 사물 위에 정확히 얹힘) → 사물 주위를 스캔한 뒤 **측정**:
   - 서버가 2단 ICP(p2p→p2plane)로 정합 → `actualPose = T⁻¹∘designPose`
   - 판정: **PASS(초록)/FAIL(빨강)/REVIEW(주황)/RETAKE** + 편차 mm/도 + 신뢰도
   - 실물 모델이 actualPose에 초록/빨강으로 겹쳐 표시 → 설계 대비 어긋남을 AR로 직관 확인

## 좌표 규약 (구현이 따르는 문서)

- `docs/COORDINATE.md` — 공간 프레임 = 등록 세션 ARKit 월드(Y-up). 클라우드는 `frame:"space"`로 전송,
  GLB 오버레이는 designPose 배치만으로 정합 (glTF와 ARKit이 동일 Y-up 오른손 규약)
- 불확도: 위치 ≈ max(3mm, 2×RMSE), 회전 0.5° → 자동 PASS는 HIGH 신뢰도 + 전 항목 확정 내부만

## 알려진 제한 (PoC)

- **VIO 모드(LiDAR 없음, 예: iPhone Air)**: ARKit `rawFeaturePoints`를 누적해 사용. 점이 희소해
  자산 모델은 점마다 작은 면(surfel, PCA 법선)으로 만든 근사 표면이며, 텍스처 없는 면(흰 벽 등)엔 점이 거의 없음.
  깊이 유효비가 없어 신뢰도가 낮게 나오고 판정이 REVIEW로 기울 수 있음 — 정밀 판정은 LiDAR 기기 대상

- Samsung/ARCore 클라이언트는 미구현 (`mobile-android/README.md` 로드맵)
- 월드맵 복원 실패(환경 변화 큼) 시 오버레이가 어긋남 → 자산 등록 세션과 비슷한 각도로 장면 진입
- 회전 불확도 0.5° 고정값 → 공차 1° 근처 편차는 REVIEW(사람 확인)로 안전 실패 — Ground Truth 보정 대상 (15절)
