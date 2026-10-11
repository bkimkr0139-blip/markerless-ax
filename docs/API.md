# API.md — 백엔드 REST 규격 (PoC)

기준: `backend/app/routers/` 구현. 모든 경로 앞에 `/api/v1` 접두사.
JSON은 **camelCase** (지시서 예시 JSON과 동일). 오류: 4xx/5xx `{"detail": "..."}`.
값 단위: 좌표 m, RPY 도(°), 편차 mm/도, 공차 mm/도 — COORDINATE.md 규약.

대화형 문서: 실행 중 `http://<host>:8111/docs` (FastAPI Swagger UI).

## Health

| Method | Path | 설명 |
|--------|------|------|
| GET | `/healthz` | `{"status":"ok"}` |
| GET | `/api/v1/version` | 앱/엔진 버전 |

## Projects / Spaces

| Method | Path | 설명 |
|--------|------|------|
| GET/POST | `/api/v1/projects` | 프로젝트 목록/생성 |
| GET | `/api/v1/projects/{code}` | 프로젝트 조회 |
| POST | `/api/v1/projects/{project_code}/spaces` | 공간 생성 (미존재 프로젝트 자동 생성) |
| GET | `/api/v1/projects/{project_code}/spaces` | 공간 목록 |
| GET | `/api/v1/spaces/{space_id}` | 공간 조회 |
| PUT | `/api/v1/spaces/{space_id}/worldmap` | ARWorldMap 업로드 (바이너리, `X-Sha256` 헤더 필수, bplist 시그니처 검사) |
| GET | `/api/v1/spaces/{space_id}/worldmap` | 월드맵 다운로드 |

공간 요청 예: `{"spaceId": "MECH-301", "name": "지하 기계실", "level": "B1"}`

## Assets — 스캔 자가 생성 (지시서 대안 절차)

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/v1/assets/scan` | 스캔 GLB로 자산 생성/갱신 — multipart `meta`(JSON) + `model`(.glb). GLB는 자산 로컬 프레임(중심 원점), extent 0.05–10m 검사 |
| GET | `/api/v1/spaces/{space_id}/assets` | 공간 자산 목록 |
| GET | `/api/v1/assets/{asset_id}` | 자산 상세 |
| GET | `/api/v1/assets/{asset_id}/model.glb` | 자산 GLB 다운로드 (오버레이용) |
| PUT | `/api/v1/assets/{asset_id}/tolerance` | 공차 변경 |

`meta` 예:
```json
{"assetId":"PUMP-301","projectId":"PJT001","spaceId":"MECH-301",
 "bimRevision":"GEN-001","family":"Pump","type":"scanAsset",
 "designPose":{"positionM":{"x":2.0,"y":1.0,"z":0.5},"rpyDeg":{"roll":0,"pitch":0,"yaw":0}},
 "tolerance":null,"notes":"iPhone 스캔"}
```
`tolerance` 생략 시 기본값 적용(위치 15mm / Z 10mm / 회전 1.0°).

## Packages — BIM 메타 임포트 (Revit 대체/연동)

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/v1/packages` | 모바일 패키지 임포트 (스키마: `revit-addon/schemas/mobile-package.schema.json`) |

**자산에 tolerance/orientation(quat|rpy) 누락 시 422** — BIM 데이터 오류로 검사 중단 (지시서 12.2).

## Inspection Tasks / Observations / Measurements

| Method | Path | 설명 |
|--------|------|------|
| POST/GET | `/api/v1/inspection-tasks` | 검수 작업 생성/목록 |
| GET/PATCH | `/api/v1/inspection-tasks/{task_id}` | 작업 조회/상태 변경 |
| POST | `/api/v1/observations` | 원본 관측 생성 (증거 URI 목록, captureMeta) |
| GET | `/api/v1/observations` | 목록 (`assetId`/`spaceId`/`taskId` 필터) |
| GET | `/api/v1/observations/{id}` | 관측 상세 |
| POST | `/api/v1/measurements` | **핵심 측정** → Observation + InspectionResult 동시 생성 |
| GET | `/api/v1/inspection-results` | 판정 결과 목록 (필터 동일) |
| GET | `/api/v1/inspection-results/{id}` | 결과 조회 |

### POST /measurements 요청 (cloud 경로 — 서버 ICP)

```json
{"assetId":"PUMP-301","deviceId":"IPHONE-SMOKE","observationId":null,
 "cloud":{"frame":"space","count":1200,"base64":"<float32 LE xyz…>"},
 "trackingNormal":true,"depthValidRatio":0.9}
```
- `cloud.frame`은 `"space"`만 허용 (기기가 월드맵 복원 후 세션 좌표로 전송 — COORDINATE.md 3절)
- `count` > 서버 상한(기본 3000)이면 linspace 서브샘플
- 명시 포즈 경로: `cloud` 대신 `actualPose` + `confidence`(없으면 UNKNOWN→RETAKE) + `uncertainty`

### 응답 (MeasurementOut)

```json
{"observation":{ "observationId":"…","assetId":"PUMP-301","bimRevision":"GEN-001",
  "designPose":{…},"actualPose":{…},"deviation":{…},"uncertainty":{"positionMm":3.0,"rotationDeg":0.5},
  "confidence":"HIGH","decision":"PASS","createdAt":"…"},
 "result":{"resultId":"…","observationId":"…","assetId":"PUMP-301",
  "decision":"PASS","confidence":"HIGH",
  "deviation":{"dxMm":4.96,"dyMm":-0.03,"dzMm":0.09,"distMm":4.96,
               "rollDeg":0,"pitchDeg":0,"yawDeg":0.03,"maxRotDeg":0.03},
  "margins":{"position":{"certainlyIn":true,"certainlyOut":false},…},
  "reasons":[{"code":"all_within","message":"…"}]}}
```

판정 규칙(요약 — `spatial_core.inspection.judge`): LOW/UNKNOWN→RETAKE · 확정 초과(dev−unc>tol)→FAIL ·
전 항목 확정 내부+HIGH→PASS · 경계 스트래들 또는 MEDIUM→REVIEW. 클라우드 경로의 신뢰도는
서버 정합 지표(RMSE/인라이어/점수)로 산출 — 기기 보고보다 우선.

## Evidence / Reports

| Method | Path | 설명 |
|--------|------|------|
| POST | `/api/v1/evidence` | 증거 업로드 (multipart file; 확장자 화이트리스트) |
| GET | `/api/v1/evidence/{id}` | 메타 조회 |
| GET | `/api/v1/evidence/{id}/download` | 원본 바이트 |
| POST | `/api/v1/reports` | 보고서 생성 (공간/자산 필터) — 한국어 요약 포함 |
| GET | `/api/v1/reports/{id}` | 보고서 조회 |

## 구성 스위치

- `MARKERLESS_DATABASE_URL` (기본 SQLite `./markerless.db`, Postgres로 교체 가능)
- 서버 설정: `max_cloud_points=3000`, 기본 공차, 업로드 상한 — `backend/app/config.py`


## 디지털 트윈 확장 (2026-10-11)

자세한 계약은 `docs/INTERFACES.md`. 기존 엔드포인트는 변경되지 않았다(`backend/tests/test_api_contract.py`가 스냅샷 81개 오퍼레이션 보존을 검사).

| 영역 | 엔드포인트 |
|---|---|
| 건물·층·배치 | `POST/GET /buildings`, `POST /floors`, `PUT /spaces/{ref}/placement` |
| 스캔 세션 | `POST /spaces/{ref}/scan-sessions`, `.../frames/init-upload`, `PUT .../frames/{i}/parts/{rgb\|depth\|conf}?offset=`, `.../complete-upload`, `.../structure`, `.../finalize`, `.../reconstruct`, `GET .../status` |
| 공간 지오메트리 | `GET /spaces/{ref}/coverage`, `/geometry/revisions`, `POST /spaces/{ref}/merge-sessions`, `POST /spaces/{ref}/design-geometry` |
| 외피·벽 | `POST /buildings/{id}/generate-envelope`, `PATCH /walls/{id}/thickness` |
| CAD | `POST/GET /cad-catalog`, `POST /assets/{id}/cad-replacements/candidates`, `.../{rid}/approve`, `DELETE .../{rid}` |
| 투어 | `GET /buildings/{id}/tour-manifest`, `GET /tours/{id}/scene`, `POST /tours/{id}/portals`, `GET /twin/files/{kind}/{id}.glb` |

관리자 웹용은 `/admin/api/twin/*` (세션+CSRF+역할). `{ref}`는 공간 정수 PK 또는 코드.
