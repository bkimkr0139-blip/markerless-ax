# DESIGN.md — 시스템 설계

version 1.0 · 2026-10-06 · Week 1–3 산출물
Single Source of Truth: 상위 디렉터리의 개발지시서

## 1. 목표와 범위

현장 스마트폰 촬영만으로 "설계(BIM/CAD) 대비 실물의 위치·방위 편차"를 측정하고
공차 판정(PASS/REVIEW/FAIL)을 내리는 마커리스 검수 PoC.

**PoC 방식 결정 — 자가 에셋 생성(Scan-to-BIM/CAD)**: 실제 Revit/CAD 데이터를 구할 수 없으므로,
스마트폰 LiDAR 스캔으로 사물의 CAD(GLB)+BIM 속성(자산 JSON)을 먼저 생성해 자산으로 등록하고,
이후 같은 사물을 촬영하면 저장된 모델과 정합해 오버레이·편차를 계산한다.
데이터 구조는 개발지시서 6.1절 패키지 스키마와 동일하므로, 실 Revit 연동 시
임포트 경로(`POST /api/v1/packages`)만 추가되고 검수 파이프라인은 그대로 사용한다.

## 2. 아키텍처

```text
┌────────────────────────────────────────────────────────────┐
│                    iPhone 앱 (SwiftUI+ARKit)                │
│  캡처: LiDAR 메시/Depth + VIO 포즈 + ARWorldMap 저장/복원     │
│  오버레이: 자산 GLB를 설계 Pose에 표시, 결과 실시간 표시        │
└───────────────┬────────────────────────────────────────────┘
                │ REST (JSON + GLB/바이너리)
                ▼
┌────────────────────────────────────────────────────────────┐
│              Backend (FastAPI, Mac Studio)                  │
│  자산/공간/작업/관측/판정/증거/리포트 관리                      │
│  ┌──────────────────────────────────────────────┐          │
│  │ 정합엔진 = spatial-core (Python/numpy) 공용     │          │
│  │  GLB 파싱·표면 샘플링 → ICP → pose_delta → judge │          │
│  │  → derive_confidence                           │          │
│  └──────────────────────────────────────────────┘          │
└────────────────────────────────────────────────────────────┘
```

### 핵심 설계 결정

| # | 결정 | 근거 |
|---|------|------|
| D1 | 정합엔진을 **백엔드에서 공통 실행** (기기 내 상주 X) | iOS/Android 동일 엔진 재사용(지시서 11절), PoC 기간 알고리즘 개선 속도, Mac Studio 연산 여력. 이후 기기 내 이식은 Production Gap에서 판단 |
| D2 | 공간 좌표 복원은 **ARWorldMap 영속화** | 완전 자동 초기정합에 집착하지 않는다(지시서 8.2). 첫 세션의 월드 좌표계 = 공간 좌표계로 정의하고 월드맵을 저장/복원해 T_space_session ≈ Identity로 해결. 잔여 오차는 자산별 ICP가 흡수 |
| D3 | 스캔 자산의 설계 Pose: **사용자가 지정한 객체 중심**, 자산 로컬 프레임 = 중심 원점 | 위치·방향 기준점 명확성(지시서 2.2). GLB는 자산 로컬, designPose는 공간 프레임으로 분리 저장 |
| D4 | ICP는 **numpy 자체 구현** (p2p coarse → point-to-plane fine 2단) | Python 3.14 wheel 호환 리스크 회피. p2p 단독의 접선 미끄럼을 p2plane으로 제거 (실측 확인) |
| D5 | 신뢰도 **HIGH만 자동 PASS**, LOW/UNKNOWN은 RETAKE | 지시서 원칙 11, 14절 |
| D6 | 불확도를 판정에 직접 반영 (dev ± unc vs tol) | 지시서 14/15절. ICP 경로는 RMSE에서 불확도를 추정 |

## 3. 데이터 흐름

### 3.1 자산 등록 (앱에서 1회)

1. 앱에서 공간 생성 (`POST /api/v1/projects/{p}/spaces`)
2. 사물 스캔 2~5초 (Scene Reconstruction 메시 누적)
3. 화면 탭으로 객체 중심 지정 → GLB를 중심 원점 자산 로컬 프레임으로 변환
4. `POST /api/v1/assets/scan` (meta JSON + GLB multipart)
   - 서버: GLB 검증(크기/삼각형 수) → AABB 계산 → 표면 샘플 캐시 생성 → 자산 저장
5. 공간 기준 저장: `PUT /api/v1/spaces/{id}/worldmap` (ARWorldMap 바이너리)

### 3.2 검수 (반복)

1. 공간 진입 → 월드맵 다운로드 → `ARWorldTrackingConfiguration.initialWorldMap` 로 로컬라이즈
2. 자산 목록에서 선택 → GLB 다운로드 → 설계 Pose에 AR 오버레이 (CAD vs Reality)
3. 촬영 2~5초 → SceneDepth 포인트 클라우드를 공간 프레임으로 변환, 자산 OBB 주변만 필터, ≤6000점 서브샘플
4. `POST /api/v1/measurements` (cloud base64 + 기기 품질 지표)
5. 서버: ICP(초깃값=designPose) → actualPose → pose_delta → judge → confidence
6. 앱: actualPose 오버레이 + 편차/판정/신뢰도 표시, 원본 증거 업로드 (`POST /api/v1/evidence`)
7. 리포트: `POST /api/v1/reports` → 한국어 요약(Astra가 읽을 형식, 12.3절)

## 4. 컴포넌트

### 4.1 spatial-core (Python, 테스트 42건)

| 모듈 | 내용 |
|------|------|
| `coordinate.poses` | SE(3) Pose, intrinsic ZYX RPY, quat, `Z_UP_TO_Y_UP`, `pose_delta` |
| `registration.glb` | 최소 GLB 리더/라이터, 면적비례 표면 샘플링, AABB |
| `registration.icp` | 2단 ICP: p2p(Kabsch) + point-to-plane(선형화 최소제곱), 트리밍/대응거리 상한 |
| `inspection.judgment` | 판정 규칙 (RETAKE→FAIL→PASS→REVIEW 우선순위, 불확도 반영) |
| `confidence.scoring` | 지표 기반 HIGH/MEDIUM/LOW (하드캡 포함, 보수적) |

### 4.2 backend (FastAPI)

- 라우터: projects, spaces(+월드맵), assets(스캔 생성/GLB/공차 Rule), packages(Revit 임포트),
  inspection-tasks, observations, **measurements(정합+판정)**, inspection-results, reports, evidence
- 저장: SQLite(기본)/PostgreSQL 전환 가능(`MARKERLESS_DATABASE_URL`), 파일은 `storage/`(sha256 파일명)
- 판정/근거/불확도/ICP 지표를 Observation + InspectionResult로 영속화 (원칙 8/12/13)

### 4.3 mobile-ios

- SwiftUI + ARKit(WorldTracking, SceneReconstruction, SceneDepth) + RealityKit 오버레이
- 모듈: App / Models / Net(APIClient) / Capture(ARCaptureController, GLBWriter) / Views
- 상세: `mobile-ios/README.md`

## 5. 판정 규칙 요약 (구현은 `spatial_core.inspection.judgment`)

```text
LOW/UNKNOWN 신뢰도            → RETAKE (자동 판정 거부)
(dist−unc > posTol 등)        → FAIL  (불확도 반영해도 확정 초과)
모든 항목 확정 내부 + HIGH     → PASS  (자동 PASS는 HIGH만)
모든 항목 확정 내부 + MEDIUM   → REVIEW
공차 경계가 불확도와 겹침      → REVIEW
```

Demo 1(+8/−4/+3mm→PASS), Demo 2(+27mm→FAIL)는 테스트로 고정했다.

## 6. 보안 (지시서 20절 — PoC 수준)

- 업로드 형식 화이트리스트(evidence), GLB 파싱 전 크기 검증, 저장소 경로 탈출 차단
- 원본 증거 무손상 저장 + sha256 기록
- 미구현(파일럿 전환 항목): TLS/로그인/프로젝트 권한/Audit. CORS 오픈은 사내 망 전제.

## 7. 알려진 제한 (Week 3 시점)

- 초기정합이 월드맵 의존: 월드맵 복원 실패 시 재스캔 필요 (Visual Relocalization은 고도화 항목)
- VIO 드리프트가 긴 촬영에서 누적될 수 있음 → 자산별 ICP가 흡수하지만 공간 전체 기준은 별도
- 회전 불확도 추정은 고정값(1°) — Ground Truth 수집 후 보정 (지시서 15/16절)
- PnP 기반 초기정합, 법선 추정 기반 미가공 클라우드 정합은 Week 4 확장 항목
- 텍스처 없는 GLB 오버레이(회색 렌더링) — 스캔 메시 텍스처는 PoC 범위 밖

## 8. KPI 측정 준비 (지시서 17절)

- Observation에 deviceId·시간·ICP 지표·판정이 모두 기록되어 성공률/반복성 산출 가능
- Ground Truth 비교 양식: `ground-truth/template.csv` (Total Station 등으로 실측 후 비교)
- 정확도 수치는 Ground Truth 확보 전까지 주장하지 않는다 (원칙 10)
