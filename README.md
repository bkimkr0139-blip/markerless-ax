# Markerless BIM/CAD 시공검수 PoC

Mac Studio + 최신 스마트폰(iPhone ARKit+LiDAR 우선) 기반 마커리스 시공검수 PoC.
전체 개발지시서는 상위 디렉터리의 `Mac Studio + 최신 스마트폰 기반 마커리스 BIM·CAD 시공검수 PoC 개발지시서.md`가 Single Source of Truth다.

## PoC 방식: 자가 에셋 생성 (Scan-to-BIM/CAD)

실제 Revit/CAD 데이터를 구할 수 없으므로, 본 PoC는 다음 흐름으로 진행한다.

```text
1. 자산 등록 (앱에서 1회)
   사물을 스캔 (ARKit Scene Reconstruction + LiDAR)
   → 메시 생성 → GLB 변환 → 백엔드에 자산(CAD/BIM JSON + GLB)으로 저장

2. 검수 (반복)
   같은 공간 재방문 → ARWorldMap으로 공간 좌표 복원
   → 저장된 CAD(GLB)를 설계 Pose에 AR 오버레이
   → 촬영 (Depth 포인트 클라우드) → ICP 정합 → Actual Pose
   → Design-vs-Actual 편차 → 공차 판정 (PASS / REVIEW / FAIL)
```

생성된 자산은 개발지시서 6.1절의 모바일 패키지 스키마와 동일한 구조(`designPose`, `tolerance`, `cad`, `bimRevision`)로 저장되므로,
이후 실제 Revit 연동 시 `POST /api/v1/packages` 임포트 경로로 교체만 하면 된다.

## 구성

| 구성요소 | 경로 | 설명 |
|---|---|---|
| spatial-core | `spatial-core/` | 좌표·정합(ICP)·판정·Confidence 엔진 (Python/numpy, 테스트 완비) |
| backend | `backend/` | FastAPI + SQLAlchemy (SQLite 기본 / PostgreSQL 전환 가능) |
| mobile-ios | `mobile-ios/` | SwiftUI + ARKit + RealityKit 앱 (LiDAR 기기 필요) |
| mobile-android | `mobile-android/` | Week 7 이후 (공통 정합엔진 = 백엔드 재사용) |
| revit-addon | `revit-addon/` | 실 Revit 연동 시 사용 (PoC에서는 스키마만) |
| agent | `agent/` | Astra 역할 정의 (도구 스키마 + 시스템 프롬프트) |
| test-data | `test-data/` | 패키지/측정 픽스처 |
| ground-truth | `ground-truth/` | 정밀측량 비교 템플릿 |
| docs | `docs/` | DESIGN / COORDINATE / API / REPORT |

정합엔진은 기기별로 중복 구현하지 않고 **백엔드에서 공통 실행**한다(모바일은 캡처+오버레이 전담).
이후 Galaxy(Week 7)도 동일 엔진을 호출하며, Production 전환 시 기기 내 C++ 코어로 이식을 검토한다.

## 빠른 시작 (Mac Studio)

```bash
# 1. Python 환경 (저장소 루트의 .venv 공용)
python3 -m venv .venv
.venv/bin/pip install -e spatial-core -e backend

# 2. 테스트
make test

# 3. 백엔드 실행
make api          # http://0.0.0.0:8000 (docs: /docs)
```

## iOS 앱 빌드

`mobile-ios/README.md` 참고. 요약:

```bash
cd mobile-ios
xcodegen generate    # Markerless.xcodeproj 생성 (xcodegen: brew install xcodegen)
open Markerless.xcodeproj
```

- Xcode(15+) + ARKit 지원 iPhone (LiDAR 있으면 메시 모드, 없으면 VIO 특징점 모드)
- Signing Team 설정 후 실행
- 앱 첫 화면 Settings에서 백엔드 주소(예: `http://192.168.x.x:8000`) 입력

## 원칙 (개발지시서 22절 요약)

1. 첫 성공경로는 iOS + LiDAR
2. 좌표계는 명시적으로 관리 — `docs/COORDINATE.md`
3. Pose 결과에는 항상 Confidence 포함
4. 한 프레임으로 판정하지 않는다 (다중 프레임 통합)
5. Ground Truth 없이 정확도를 주장하지 않는다
6. 자동 PASS는 HIGH Confidence만 허용
7. 테스트 우선, Sprint마다 `docs/REPORT.md` 갱신


## 디지털 트윈 확장 (2026-10-11)

공간 전체 스캔 → 사진 질감 3D 메시 → 외부→실내 투어. 문서: `docs/INTERFACES.md`(계약), `docs/QUALITY_TECHNIQUES.md`(기법·측정), `docs/ACCEPTANCE.md`(인수 현황),
`docs/GROUND_TRUTH.md`(실측 계획), `docs/FAILURE_CASES.md`, `docs/PRODUCTION_GAP.md`, `docs/AS_IS_AUDIT.md`. 선택 의존성: `pip install -e "spatial-core[reconstruction]"`, AI 모델은 `tools/fetch_models.sh`.
