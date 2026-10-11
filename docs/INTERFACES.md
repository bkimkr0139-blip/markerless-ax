# INTERFACES.md — 디지털 트윈 확장 공통 계약 (좌표·포맷·ID·API)

> 공간 전체 스캔 / 디지털 트윈 확장의 모든 구성요소(iOS · 백엔드 · spatial-core · 관리자 웹)가 따르는 단일 계약.
> 기존 계약(`docs/API.md`, `docs/COORDINATE.md`)은 그대로 유효하고, 이 문서는 **추가분**만 다룬다.

## 1. 좌표계·단위·행렬 규약
- 오른손 Y-up, 단위 **미터**, 각도 도(°) (ARKit/glTF와 동일). `docs/COORDINATE.md` 우선.
- 4×4 변환은 JSON에서 **16개 float, 열 우선(column-major)** — `simd_float4x4` 메모리 순서 그대로, three.js `Matrix4.fromArray`와 동일.
- 프레임 이름: `session`(ARKit 세션 월드) → `space`(공간 로컬; 월드맵 복원 시 session≈space) → `building`(건물 좌표) → `site`.
  `T_building_space`는 공간을 건물 안에 놓는 변환(바닥 높이 = 층 elevation 포함).
- 모든 GLB는 해당 객체의 **space 프레임**으로 저장한다(건물 씬에서 `T_building_space`를 노드 행렬로 적용).

## 2. 데이터 출처 구분 (sourceKind)
`OBSERVED` 직접 관측 · `INFERRED` 규칙·가정으로 생성 · `DESIGN` 설계(BIM/IFC/CAD) · `REPLACEMENT` 승인 후 치환 모델.
`OBSERVED` 원본은 어떤 경우에도 덮어쓰거나 삭제하지 않는다. 각 지오메트리 레코드는 `sourceKind, confidence(0..1), uncertaintyM, sourceSessionIds[], approvedBy, reviewStatus`.
`reviewStatus`: `AUTO | REVIEW_REQUIRED | APPROVED | REJECTED`. `REVIEW_REQUIRED`는 공식 설계 검수에 사용 금지.

## 3. ID
- `scanSessionId`, `revisionId`, `tourId`, `portalId`: UUID 문자열. `buildingId`, `floorId`: 정수 PK.
- 공간은 기존 `spaces.id`(정수 PK, API에서는 `spaceId` 문자열 코드 또는 numericId)를 그대로 쓴다.
- 벽: `wallId` = 정수 PK.

## 4. 캡처 프레임 포맷 (iOS → 서버)
프레임 1개 = 3개 파트 + 메타.
| 파트 | 형식 |
|---|---|
| `rgb` | JPEG (센서 원본 방향 landscape, 긴 변 ≤ 1920) |
| `depth` | float32 little-endian, row-major, 미터, `depthWidth*depthHeight*4` 바이트, 무효=0 |
| `conf` | uint8 row-major, 0=low 1=medium 2=high, `depthWidth*depthHeight` 바이트 |

메타(JSON):
```json
{"frameIndex": 12, "segmentIndex": 0, "timestampNs": 123456789,
 "cameraTransform": [16 floats column-major, camera→session],
 "intrinsics": [fx,0,0, 0,fy,0, cx,cy,1]  /* column-major 3x3, RGB 이미지 픽셀 기준 */,
 "imageWidth": 1920, "imageHeight": 1440, "depthWidth": 256, "depthHeight": 192,
 "trackingState": "normal|limited|notAvailable", "exposureDurationS": 0.01, "iso": 100, "whiteBalanceK": 5000,
 "lightEstimateLux": 300, "motionBlurScore": 0.0..1.0, "worldMappingStatus": "mapped"}
```
카메라 좌표계: ARKit(+X 오른쪽, +Y 위, **−Z 전방**). intrinsics는 `capturedImage` 해상도 기준이며 depth 해상도 비율로 변환은 서버가 수행.

## 5. 신규 REST API (`/api/v1`, 기존 API 키 정책 동일)
| Method | Path | 요약 |
|---|---|---|
| POST/GET | `/buildings`, `/buildings/{id}` | 건물 등록(이름·projectCode·GPS·외부진입점) |
| POST | `/floors` | 층 등록 `{buildingId, level, elevationM, heightM}` |
| PUT | `/spaces/{spaceId}/placement` | 공간의 건물/층 배치 `{buildingId, floorId, tBuildingSpace[16], kind}` (kind=ROOM/CORRIDOR/FACILITY_ZONE) |
| POST | `/spaces/{spaceId}/scan-sessions` | 세션 생성 `{mode: SPACE|FACILITY|OBJECT, deviceId, deviceProfile, capabilities, worldMapRevision, relocalized}` → `{scanSessionId, status:"SCANNING"}` |
| POST | `/scan-sessions/{id}/frames/init-upload` | 프레임 업로드 선언 `{meta, parts:{rgb:{bytes,sha256},depth:{...},conf:{...}}, idempotencyKey}` → 이미 완료면 `{state:"DONE"}`, 아니면 `{state:"PENDING", received:{rgb:0,..}}` |
| PUT | `/scan-sessions/{id}/frames/{frameIndex}/parts/{rgb\|depth\|conf}?offset=N` | 청크(append at offset, 재개 가능). 헤더 `X-Sha256`는 전체 파트 해시(마지막 청크에서 검증) |
| POST | `/scan-sessions/{id}/frames/complete-upload` | `{frameIndex}` 파트 완전성·sha256 검증 후 프레임 확정 |
| POST | `/scan-sessions/{id}/structure` | RoomPlan 결과 `space-structure.json` 업로드(선택) |
| POST | `/scan-sessions/{id}/finalize` | 촬영 종료 `{endedReason}` → 품질 사전검사, 상태 `PROCESSING`/`NEED_MORE_CAPTURE` |
| POST | `/scan-sessions/{id}/reconstruct` | 비동기 재구성 잡 큐잉 `{voxelM?, textureMaxPx?, force?}` |
| GET | `/scan-sessions/{id}/status` | `{status, stage, progress, message, quality, coverage, revisionId?}` |
| GET | `/spaces/{spaceId}/coverage` | 면적 기준 커버리지 `{overall, floor, ceiling, walls:[{wallId,coverage}], unobservedAreaM2}` + 히트맵 GLB URL |
| GET | `/spaces/{spaceId}/geometry/revisions` | 지오메트리 리비전 목록(sourceKind별 파일) |
| POST | `/spaces/{spaceId}/merge-sessions` | 다른 세션을 같은 공간에 정합 병합(자동 병합 불확실 시 `REVIEW_REQUIRED`) |
| POST | `/buildings/{id}/generate-envelope` | 추정 외피 생성 `{defaultThicknessM?, buildingType?}` |
| PATCH | `/walls/{id}/thickness` | `{thicknessM, method: MEASURED|BIM|CONFIG_DEFAULT|INFERRED, note}` |
| POST | `/assets/{assetId}/cad-replacements/candidates` | CAD 치환 후보 조회·정합(미확정은 `UNDETERMINED`) |
| POST | `/assets/{assetId}/cad-replacements/{id}/approve` | 관리자 승인(되돌리기: `DELETE`) |
| GET | `/buildings/{id}/tour-manifest` | 투어 매니페스트(§6) |
| GET | `/tours/{tourId}/scene` · POST `/tours/{tourId}/portals` | 장면·포털 |

세션/리비전 상태: `SCANNING PAUSED RECOVERY PROCESSING READY NEED_MORE_CAPTURE FAILED` (잡 단계: `QUEUED RUNNING …`).
대용량 파일은 SSE 이벤트에 싣지 않는다 — SSE는 `{type, id, revision}`만.

## 6. 투어 매니페스트 (`GET /buildings/{id}/tour-manifest`, 관리자 웹은 `/admin/api/twin/buildings/{id}/scene` 동일 JSON)
```json
{
 "building": {"id":1,"name":"본관","projectCode":"PJT001","gps":{"lat":0,"lon":0},"notice":"외관/벽두께 일부 추정"},
 "floors": [{"id":1,"level":"1F","elevationM":0.0,"heightM":3.2}],
 "envelope": {"glbUrl":"/admin/api/twin/files/envelope/1.glb","sourceKind":"INFERRED","reviewStatus":"REVIEW_REQUIRED",
              "walls":[{"id":10,"spaceId":"R101","kind":"EXTERIOR|INTERIOR","thicknessM":0.2,"thicknessMethod":"CONFIG_DEFAULT","confidence":0.4,"reviewStatus":"AUTO"}]},
 "entrances": [{"id":"E1","name":"정문","positionBuilding":[x,y,z],"yawDeg":90,"spaceId":"LOBBY","exteriorViewpoint":[x,y,z]}],
 "spaces": [{"spaceId":"R101","numericId":3,"name":"기계실","floorId":1,"kind":"ROOM","tBuildingSpace":[16],
             "revisionId":"uuid","glbUrl":"…/observed.glb","texturedGlbUrl":"…/textured.glb","lod":{"web":"…","mobile":"…"},
             "bboxSpace":{"min":[..],"max":[..]},"coverage":0.86,"quality":"REVIEW","sourceKinds":["OBSERVED","INFERRED"],
             "layers":{"observed":"url|null","inferred":"url|null","design":"url|null","replacement":"url|null","coverageHeat":"url|null"},
             "unobservedMask":true}],
 "portals": [{"id":"uuid","fromSpace":"LOBBY","toSpace":"R101|null","positionBuilding":[..],"yawDeg":0,"widthM":0.9,"heightM":2.1,"state":"OPEN|CLOSED|UNKNOWN","reviewStatus":"APPROVED"}],
 "assets": [{"assetId":"PUMP-301","name":"…","spaceId":"R101","positionBuilding":[..],"bboxBuilding":{"min":[..],"max":[..]},"replaced":false,"cadGlbUrl":null,"extractedMask":true}],
 "viewpoints": [{"id":"V1","name":"로비 전경","position":[..],"target":[..],"spaceId":"LOBBY"}]
}
```
관리자 웹 파일 서빙: `/admin/api/twin/files/{kind}/{id}.glb` (`kind`: `observed|textured|inferred|design|replacement|envelope|coverage|lodweb|lodmobile`), 세션 쿠키 인증.

## 7. 이중 표면 방지(§7.2)
`assets[].extractedMask=true`이면 웹은 해당 자산 bbox(+2cm) 안의 공간 메시 face를 숨기고(`space-background-only`) 자산 GLB/치환 GLB만 그린다. 공간 GLB의 노드 `extras.assetRegions`에 `[{assetId, min[3], max[3]}]`를 둔다.

## 8. GLB 규약
glTF 2.0, 단일 메시 노드, `POSITION/NORMAL/TEXCOORD_0`, 텍스처는 JPEG 임베드, 메시 `extras`: `{sourceKind, unobservedFaces, textureCoverage}`.
**미관측 텍셀은 아틀라스에서 회색(128)으로 남기고 만들어 채우지 않는다**(관측 마스크는 서버가 보유). 가려진 면을 실제 촬영한 듯 보이게 하지 않기 위해서다.
미관측 구조 평면은 별도 `inferred` GLB(주황 반투명, `sourceKind=INFERRED`)와 `coverage` 히트맵 GLB(초록=관측/빨강=미관측)로 따로 제공한다.
파일 종류(`kind`): `observed textured inferred coverage lodweb lodmobile`(리비전 ID) · `envelope`(건물 ID) · `design`(지오메트리 ID) · `replacement`(치환 ID).

## 9. 구현 중 확정된 규약 (2026-10-11)
- **픽셀 규약**: 정수 인덱스 i 픽셀의 중심은 연속 좌표 i+0.5, intrinsics의 cx,cy는 이미지 좌상단 모서리 기준. 실기기 ARKit 값으로 검증 필요(GROUND_TRUTH.md §4).
- **포털/출입구 `yawDeg`**: *들어가는 방향을 바라볼 때*의 카메라 heading. 방향벡터 = (−sin yaw, 0, −cos yaw) (yaw 0 = −Z 방향, three.js `rotation.y`와 동일). `positionBuilding`은 문 바닥 중심.
- **RoomPlan 구조**: `space-structure.json`의 `frame="roomplan-session"`은 RGB-D 패스와 다른 세션 좌표다. 서버가 메시에서 뽑은 벽에 yaw+이동으로 정렬하고, 모호하면(대칭 방·문/창 단서 없음) **쓰지 않고** 메시 추정 구조로 대체한다.
- **인증**: `/api/v1/*`는 API 키, `/admin/api/*`는 세션+CSRF+역할. 승인(벽 두께 승인·포털 승인·CAD 치환·설계 레이어 검토)은 관리자 API에서 현장관리자 이상. `/api/v1`의 approve는 `X-Actor` 헤더로 행위자를 남기지만 단일 키라 개인 식별은 불가(PRODUCTION_GAP).
- **CAD 치환 승인**: `UNDETERMINED`는 근거 `comment`와 `acknowledge=true`로 사람이 확인해야 승인되며 치수 오차 35%를 넘으면 불가. 승인은 새 GLB(`replacement`)만 만들고 OBSERVED 원본은 바뀌지 않는다. 되돌리기 = `DELETE`/`revert`.
- **관리자 API 추가**: `/admin/api/twin/{buildings, buildings/{id}/scene, spaces/{ref}/revisions|coverage|sessions, walls/{id}/thickness|review, portals(POST/PATCH), revisions/{id}/review|make-current, geometry/{id}/review, assets/{id}/cad/…, files/{kind}/{id}.glb}`.
- **설계 레이어**: `POST /api/v1/spaces/{ref}/design-geometry` (multipart `model` GLB + `meta{designRevision, ifcGuid}`) → DESIGN, `REVIEW_REQUIRED`. 관측 지오메트리는 변경되지 않는다.
- **사물 후보**: 매니페스트 `spaces[].objectCandidates[]` — `UNKNOWN_OBJECT` 라벨, 등록 자산과 IoU>0.2이면 `assetCandidates`로 *제안만*(`autoConfirmed=false`).

