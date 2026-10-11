# REPORT.md — 마커리스 BIM·CAD 시공검수 PoC 스프린트 보고

작성일: 2026-10-06 · 범위: 개발지시서 Week 1~3 전체 + 스마트폰 실측 가능 수준 완성
작성: PoC 개발 세션 (Claude Code) · 검증: 73개 자동 테스트 + 실서버 E2E


> **2026-10-11 갱신 — 디지털 트윈 확장(Sprint 0~7 범위)**: 아래 §0이 최신이다. 이하 본문(§1~8)은 2026-10-06 초기 스프린트 기록이며 수치(테스트 73개, 엔드포인트 25개 등)는 **구버전**이다 (현재: spatial-core 83 + backend 76 테스트, API 81개 기존 오퍼레이션 보존 + 신규).

## 0. 디지털 트윈 확장 요약 (2026-10-11)

**한 줄:** 기존 객체 스캔·검수를 보존한 채, LiDAR 공간 전체 스캔 → TSDF 융합 → 사진 질감 3D 메시 → 구조·커버리지·개구부 → 건물 외피(추정) → 관리자 웹 외부→실내 투어 → CAD/설계 레이어까지 end-to-end 구현하고, 정밀·품질 기법을 측정으로 채택/기각했다.

### 구현 범위
| 영역 | 내용 | 문서 |
|---|---|---|
| iOS | 모드 선택(공간/시설/사물), 공간 키프레임 수집(RGB·depth·conf·포즈·intrinsics), 선명도·깊이 평균 품질 로직, RoomPlan 구조 패스, 재개 가능한 청크 업로드 큐, 커버리지 HUD | `mobile-ios/CAPTURE_QUALITY.md` |
| 백엔드 | 건물/층/배치, 스캔 세션·프레임 업로드(청크·sha256·idempotency), 비동기 재구성 잡, 리비전·커버리지·벽/개구부, 외피 생성, 포털/투어 매니페스트, CAD 카탈로그·후보·승인/되돌리기, 설계 레이어, 38개 테이블 | `docs/INTERFACES.md` |
| spatial-core | 희소 TSDF, 구조 추출, 세션 정렬, 포즈 그래프, 메시 정규화, 텍스처 아틀라스, GLB 내보내기, 외피, CAD 매칭, AI 마스크 | `docs/QUALITY_TECHNIQUES.md` |
| 관리자 웹 | 3D 디지털 트윈 메뉴: 외부 조망→출입문→실내 투어, 레이어(관측/추정/설계/치환/히트맵), 벽 두께·포털 편집·승인, 측정·핀·사진 모드, 점진 LOD | |
| 보안 | 비밀번호 정책, 기본/임시 비밀번호 변경 강제, 신규 API 입력 방어 | `docs/PRODUCTION_GAP.md` |

### 정밀·품질 기법 결과 (상세: QUALITY_TECHNIQUES.md)
채택: half-pixel 규약 통일, 평면 정밀화(방 치수 오차 60~85→4~5mm), 포즈 그래프(드리프트 벽 번짐 86.5→5.2mm), 메시 정규화(평면편차 2.8→1.0mm), 텍스처 노출/WB 보정(얼룩 −24%), AI 사람 마스크(유령 4.24→0㎡).
**기각(실측 근거):** RGB 가이드 업샘플, AI 단안 깊이 채우기(LiDAR 범위 안에서는 단순 최근접보다 나쁨 7.4% vs 5.8%, 범위 밖 25~30%), 프레임-모델 자세 보정(평면 방에서 미끄러져 악화).

### 한계 (정직)
- **실기기 검증 없음**: 공간 규모 LiDAR 스캔·RoomPlan·업로드 규모·열/메모리·투어 FPS(실GPU)는 미검증. 모든 정확도 수치는 합성/공개 RGB-D 기준.
- 자동 확정 없음: 사물→자산 연결은 후보 제안만, 미확정 CAD는 사람 확인 필요.
- 이 저장소는 여전히 git 커밋이 없다.


---

## 1. 한 줄 요약

**iPhone 스캔으로 자가 BIM/CAD 자산을 생성·저장하고, 재스캔 시 서버 정합(ICP)으로 매칭해
AR 오버레이 + 편차 판정(PASS/FAIL/REVIEW/RETAKE)을 내는 흐름이 백엔드까지 완성되었다.**
iOS 앱 소스와 Xcode 프로젝트까지 생성 완료 — 실기기 빌드는 Xcode 설치(GUI 설치 대기 중) 후 가능.

## 2. 스프린트 목표 대비 결과

| 지시서 항목 | 상태 | 산출물 |
|-------------|------|--------|
| 저장소/문서 기반 (Week 1) | ✅ | README, DESIGN.md, COORDINATE.md, API.md |
| 정합·판정 엔진 (Week 2) | ✅ | spatial-core: 2단 ICP, 판정, 신뢰도, GLB I/O |
| 백엔드 API (Week 2~3) | ✅ | FastAPI 25 엔드포인트, 스캔 자산, 월드맵, 측정 |
| iOS 클라이언트 (Week 3) | ✅ 소스 완성 | SwiftUI+ARKit 10파일, xcodegen 프로젝트 생성 |
| 스캔→자산화→재스캔 매칭 (대안 절차) | ✅ | GLB 자산 등록 + ICP 매칭 + 오버레이 설계 |
| Galaxy/ARCore | △ 스텁 | mobile-android/README 아키텍처 확정 |
| Astra 에이전트 | △ 스텁 | agent/ 도구 목록+프롬프트 (API 전용 원칙) |
| Revit 애드온 | △ 스텁 | 스키마(JSON Schema)+C# 골격, 패키지 임포트는 서버 완성 |
| Ground Truth | ✗ (의도대로) | 템플릿만 — **실측 전 정확도 주장 없음** (지시서 15절) |

## 3. 검증 결과 (실측)

### 3.1 자동 테스트 — 73개 전부 통과

- **spatial-core 46개** (`make test`): 좌표 변환 roundtrip, 판정 규칙 전 분기(RETAKE/FAIL/PASS/REVIEW/예외),
  ICP 항등·알려진 변환 복원·아웃라이어 트리밍·**point-to-plane 접선 미끄럼 제거**, GLB 왕복, 신뢰도 캡
- **backend 27개** (`make backend-test`): 공간/월드맵 왕복(bplist), 패키지 임포트+422 규칙,
  스캔 자산 라이프사이클, 클라우드 측정 PASS/FAIL/RETAKE, 회전 2° FAIL(±0.3°), 관측 부착, 보고서/증거

### 3.2 실서버 E2E (uvicorn + curl, `test-data/` 픽스처)

| 입력 | 기대 | 실측 |
|------|------|------|
| 패키지 임포트 (quat 경로 포함 2자산) | 201 | imported 2 |
| 스캔 자산 등록 (GLB multipart) | 201 | 자산+공차+AABB 저장 |
| 측정 +5mm | PASS/HIGH | **PASS/HIGH dx 4.9mm** |
| 측정 +27mm (Demo 2) | FAIL/HIGH | **FAIL/HIGH dx 26.9mm** |
| 측정 40점 | RETAKE/LOW | **RETAKE/LOW** |
| yaw 0.6° 실측 (구현 개선 케이스) | 정확 복원 | **yaw 0.63°, 위치 오차 ~0.02mm** |

### 3.3 성능

- 측정 1회 서버 왕복 **1.12초** (1200점 클라우드, 2단 ICP 40iter, 핫 상태) — 지시서 9.5 목표(≤5초/자산) 여유
- ICP target 샘플은 면적 밀도 기반(6만 점/m², 300~24000 클램프) — 캐시로 반복 측정 시 재계산 없음

## 4. 개발 중 발견·수정한 핵심 문제 (학습 기록)

1. **ICP 초깃값 이중 변환**: 클라우드는 이미 공간 프레임인데 designPose를 초깃값으로 넣어 2m 오류.
   → 초깃값=항등으로 확정, COORDINATE.md 3절에 명문화
2. **point-to-point 접선 미끄럼**: 대칭 박스에서 p2p가 Z축 +12mm/회전 0.6° 잔여를 남김(안전하게 REVIEW로 떨어짐).
   → **2단 정합**(p2p coarse → point-to-plane fine, 지시서 9.3 권장)으로 해결 — 실측에서 접선 오차 0.02mm 수준
3. **표면 샘플 부족**: 균일 300점 샘플로 RMSE 바닥 ~3cm → 판정 오류. → 면적 밀도 기반 샘플링으로 RMSE 바닥 5mm대
4. **불확도 과대**: 회전 불확도 1.0°는 공차 1° 판정을 불가능하게 함 → 0.5° (GT 보정 대상 명시)
5. ICP 방향 의미 확정: T는 "실측→설계"이므로 **actualPose = T⁻¹∘designPose**

## 5. 좌표·판정 설계 확정 (COORDINATE.md/DESIGN.md)

- 공간 프레임 = 자산 등록 세션의 ARKit 월드. **ARWorldMap 영속화로 T_space_session ≈ Identity**
- 자가 에셋 GLB = 자산 로컬 프레임(사용자 탭 중심 원점, 축 평행) → designPose = 중심, rpy 0
- 판정: RETAKE(LOW/UNKNOWN) → FAIL(확정 초과) → PASS(전 항목 확정 내부+HIGH) → REVIEW(나머지)
- 자동 PASS는 HIGH 신뢰도에서만 — 신뢰도는 서버 정합 지표 기반(RMSE/인라이어/점수/추적상태), 하드캡 포함

## 6. 스마트폰 실측 방법 (다음 단계 — 유일한 남은 관문)

1. **Xcode 설치**: App Store 페이지가 열려 있음(GUI에서 설치 클릭 필요 — CLI 설치는 sudo 불가 확인됨)
2. 저장소 루트: `make api` (백엔드 8111) → `cd mobile-ios && open Markerless.xcodeproj`
3. Team 선택 후 iPhone(LiDAR 모델)에 설치 → 같은 Wi-Fi에서 서버 주소 입력
4. 공간 생성 → **자산 등록 탭**에서 사물 스캔→중심 탭→등록 → **검수 탭**에서 오버레이 확인→측정
   (상세: `mobile-ios/README.md`)
- Swift 소스는 문법 파싱 검증(swiftc -parse) 통과. 타입체크/기기 빌드는 Xcode 설치 후 즉시 가능.

## 7. 알려진 제한

| 항목 | 내용 | 완화 |
|------|------|------|
| 회전 불확도 0.5° 고정 | 공차 1° 근처는 REVIEW로 안전 실패 | GT 캠페인으로 보정 (ground-truth/) |
| 월드맵 복원 실패 시 | 오버레이 어긋남 → ICP가 흡수하나 큰 변화 환경은 RETAKE 안전 실패 | 등록 세션과 유사한 장면 진입 안내 |
| 합성 데이터 검증 | 정확도 수치는 실측 GT 없음 | 보고는 "합성 기준" 명시 — 지시서 15절 준수 |
| ARCore 클라이언트 | 미구현 | 서버 공유 엔진 설계로 앱만 추가하면 됨 |
| GLB 파서 서브셋 | POSITION+indices만 | PoC 자가 에셋은 이 포맷으로 생성됨 |

## 8. 다음 스프린트 제안

1. iPhone 실기기 빌드·현장 시연 (Xcode 설치 직후 가능)
2. Ground Truth 캠페인 → 불확도 모델 회귀 보정 → REVIEW→PASS 전환율 개선
3. ARCore 클라이언트 (Cloud Anchors로 T_space_session 대응 — mobile-android/README)
4. 실 Revit 연동 패키지로 자가 에셋과 병행 검증
