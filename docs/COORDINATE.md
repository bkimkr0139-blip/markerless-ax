# COORDINATE.md — 좌표계 규격 (PoC의 최중요 문서)

version 1.0 · 2026-10-06
구현: `spatial-core/spatial_core/coordinate/poses.py` (테스트: `tests/test_poses.py`)

## 1. 기본 규약

| 항목 | 규약 |
|------|------|
| 좌표계 손성 | 오른손 계열만 사용 (BIM, glTF, ARKit, ARCore 모두) |
| 내부 단위 | 미터(m), 라디안(rad) — 코드 내부 |
| 보고 단위 | 위치 편차 mm, 회전 편차 도(°), 공차 mm/도 |
| RPY 순서 | intrinsic Z-Y-X: R = Rz(yaw)·Ry(pitch)·Rx(roll) |
| Quaternion | API JSON은 {x, y, z, w} (Hamilton, w가 마지막) |
| 변환 표기 | **T_A_B**: B프레임 점을 A프레임으로. p_A = T_A_B · p_B |

Pose canonical JSON (저장/전송 표준):

```json
{"positionM": {"x": 12.35, "y": 8.22, "z": 1.25},
 "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 90}}
```

입력은 `quat: {x,y,z,w}`도 허용하며 서버가 canonical로 정규화한다.
회전행렬↔RPY는 유일하지 않다(yaw 200° ≡ −160°) — 비교는 항상 행렬로 한다.

## 2. 프레임 정의 (PoC)

```text
T_site_bim      : 미사용 (실 BIM 연동 시 도입. survey 기준)
T_bim_space     : 미사용 (PoC에서는 공간 프레임이 최상위. 실 연동 시 Revit shared coord→space)
T_space_session : ARWorldMap 복원으로 해결 (아래 4절)
T_session_camera: ARKit VIO가 매 프레임 제공 (ARFrame.camera.transform)
T_camera_object : ICP가 산출 (측정 시점)
```

PoC에서는 **공간 프레임(Space) = 자산 등록 세션의 ARKit 월드 좌표계**로 정의한다.
즉 BIM 좌표계가 없는 대신 공간 로컬 좌표계가 기준이며, 실 Revit 연동 시
`T_bim_space`를 패키지에 추가하면 상위 체인만 확장된다 (검수 로직 불변).

- ARKit 월드: Y-up, 오른손, 세션 시작 원점. glTF와 동일 규약 → 자산 GLB 오버레이에 추가 변환 불필요
- 공간 프레임의 Z-up 버전(측량 관례)이 필요하면 `R_zup_yup = Z_UP_TO_Y_UP`(구현 제공)로 변환.
  BIM(Z-up) → glTF/AR(Y-up): (x, y, z)up → (x, z, −y)up. 오른손 유지(det=+1, 반사 없음)

## 3. 변환 체인 (실제 계산)

```text
측정 클라우드(세션 프레임)
  → T_space_session 월드맵 복원     : 세션 → 공간 (기기에서 수행)
  → 자산 OBB 주변 필터 후 업로드 (공간 프레임)
  → ICP(init = Identity) → 보정행렬 T ("실측 → 설계 위치")
  → actualPose = T⁻¹ ∘ designPose   : T_space_actualObject

Delta = pose_delta(designPose, actualPose)
      = { dxMm, dyMm, dzMm, distMm, rollDeg, pitchDeg, yawDeg, maxRotDeg }
```

- 위치 편차: **공간 프레임 X/Y/Z 성분**으로 보고 (자산 로컬 축 보고는 Week 5 옵션).
  회전 편차: R_rel = R_designᵀ·R_actual 의 ZYX 분해
- 클라우드는 이미 공간 프레임이므로 ICP 초깃값은 **항등**이다. designPose를 초깃값으로
  넘기면 이중 변환된다. T는 "실측→설계" 방향이므로 실제 포즈는 T⁻¹에 설계를 합성해 얻는다.

## 4. T_space_session — ARWorldMap 영속화 (설계 결정 D2)

1. 자산 등록 세션 종료 전 `getCurrentWorldMap` → `PUT /spaces/{id}/worldmap`
2. 재방문 시 다운로드 → `ARWorldTrackingConfiguration.initialWorldMap` 설정
3. 로컬라이즈 성공 시 세션 월드 = 등록 당시 월드 → **T_space_session ≈ Identity**
4. 잔여 오차(수 cm): VIO 드리프트 + 월드맵 정합 오차 → 자산별 ICP가 흡수 (ICP 초깃값=designPose)

복원 실패 시(특징 부족/환경 변화): 재스캔 안내. 완전 자동 초기정합은 고도화 항목(지시서 8.2).

## 5. 자가 에셋 생성 프레임 규약

```text
자산 로컬 프레임 (Asset)
  원점: 사용자가 탭으로 지정한 객체 중심
  축  : 등록 시점 세션 월드 축과 평행 (yaw 0)
  GLB : 이 프레임 좌표로 저장 (센터링 완료)

designPose (공간 프레임)
  position : 객체 중심의 공간 좌표
  rpy      : (0, 0, 0) — 재스캔/회전 보정은 ICP가 담당
```

이 규약 덕분에 오버레이는 `Entity.transform = designPose` 단순 계산이고,
실제 설비의 설계 방위가 있는 경우(Revit 연동) designPose.rpy에 그대로 들어간다.

## 6. 불확도와 판정 연계

```text
자동 PASS 조건 = (Confidence == HIGH) AND (dist + unc ≤ tol 등 모든 항목 확정 내부)
ICP 경로 불확도: unc_pos ≈ max(3mm, 2×RMSE), unc_rot = 0.5° (PoC 고정값 — GT 보정 예정)
```

Root-chaining 참고: 세션→공간 변환 오차는 클라우드에 그대로 누적되므로
월드맵 복원 품질이 낮으면 ICP가 잘못된 초깃값에서 시작할 수 있다.
이때는 ICP 잔차/인라이어 비율이 나빠져 자동으로 신뢰도 강하 → RETAKE로 안전하게 실패한다.

## 7. 체크리스트 (새 좌표 코드 작성 시)

- [ ] 프레임 이름을 T_A_B로 명시했는가 (방향 헷갈림 방지)
- [ ] 단위는 내부 m/rad 유지, 변환은 입출력 경계에서만
- [ ] RPY 비교를 각도가 아니라 행렬로 하는가
- [ ] Z-up↔Y-up 변환에 회전(반사 아님)을 썼는가
- [ ] 새 변환 추가 시 roundtrip 테스트(compose·inverse) 작성했는가


## 6. 디지털 트윈 확장 좌표 규약 (2026-10-11)

- 프레임: `session`(ARKit) → `space`(공간 로컬) → `building`. `T_building_space`는 `spaces.t_building_space`(열 우선 16). GLB는 항상 **space 프레임**, 건물 씬에서 변환 적용.
- 4×4 행렬 JSON = 열 우선 16 float (`simd_float4x4` 순서, three.js `fromArray`와 동일).
- **픽셀 규약**: 정수 인덱스 i 픽셀 중심 = 연속 좌표 i+0.5, intrinsics의 cx,cy는 이미지 좌상단 모서리 기준. 실기기 ARKit으로 검증 필요(틀리면 3m에서 ~1cm).
- ARKit 카메라(+X 오른쪽, +Y 위, −Z 전방) ↔ CV 카메라(x 오른쪽, y 아래, z 전방): `T_cv = T_arkit · diag(1,−1,−1,1)`.
- 포털 `yawDeg` = 들어가는 방향을 바라볼 때의 heading, 방향벡터 (−sin, 0, −cos) — three.js `rotation.y`.
- RoomPlan 구조는 별도 세션 프레임(`roomplan-session`) → 서버가 메시 벽에 yaw+이동 정렬, 모호하면 사용 안 함.
