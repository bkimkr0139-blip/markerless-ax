# 공간 스캔 촬영 품질 로직

서버 3D 재구성·텍스처링에 들어가는 원본(키프레임 RGB-D)의 품질을 앱에서 끌어올리는 로직과 임계값. 순수 로직은 `Markerless/Capture/SpaceScanLogic.swift`, 시험은 `Tools/spacescan_logic_check.swift` (Mac `swiftc` 하네스).

## 1. 선명도 기반 키프레임 선택
- 프레임마다(창이 열려 있을 때만) `capturedImage`의 Y 평면을 약 320px 너비로 샘플링해 4-이웃 Laplacian 분산을 구한다 (`SpaceFrameWriter.sharpness`, `SharpnessMath.laplacianVariance`).
- 절대값은 장면 의존(무늬 없는 흰 벽은 항상 낮음)이므로 **최근 창의 최대 선명도(피크 홀드, 창마다 ×0.85 감쇠)**를 기준으로 상대 비교한다. 기준이 15 미만이면 판단을 보류(0)한다.
  - 상대값 ≥ 0.65 → sharpBlur 0, ≤ 0.20 → 1 (선형).
- `motionBlurScore = min(1, max(speedBlur, sharpBlur) + 0.2*min(...))`. speedBlur는 기존 `KeyframeGate.blurScore`(각속도·선속도·노출). 필드 이름·범위(0 선명…1 흐림)는 그대로.
- 이동 게이트(0.15 m 또는 10°, 열 상태에 따라 0.25 m)가 열리면 **최대 0.4초 / 6프레임** 창(`BestFramePicker`)을 열고 블러 점수가 가장 낮은 프레임 1장을 확정한다. 창 중 ARFrame은 최선 후보 1개만 유지.
- 최선 후보도 `maxBlur(0.5)`를 넘으면 거절하고 HUD에 "흔들림 — 천천히"를 1.5초 표시. 게이트는 갱신하지 않으므로 다음 프레임에서 창이 다시 열린다.
- 기존 속도 상한(각속도 < 1 rad/s, 선속도 < 0.5 m/s)은 유지.

## 2. 정지 시 깊이 시간 평균
- 각속도 < 0.05 rad/s, 선속도 < 0.03 m/s 인 동안 sceneDepth/confidence를 최대 5프레임 링 버퍼(`DepthRing`)에 쌓고, 움직이면 비운다(다른 시점 깊이를 섞지 않음).
- 키프레임 확정 시 링에 2장 이상이면 `DepthDenoise.average`: 픽셀별로 0/비유한 값 제외 → 중앙값에서 max(3 cm, 3 %) 이내 표본만 → conf 가중(0:0.25, 1:1, 2:2) 평균. 표본 2 미만이면 단일 값 유지. 출력 conf = 기여 표본 conf 평균의 반올림. 정지 상태에서 노이즈 표준편차가 ≈ 1/√5 로 줄어드는 것을 하네스로 검증.
- 메타 `depthFramesAveraged`(1 또는 2~5).

## 3. 고화질 사진 (옵션, 기본 OFF)
- 대기 중 SpaceScanView의 "고화질 사진" 토글(iOS 16+ 이고 `recommendedVideoFormatForHighResolutionFrameCapturing` 가 있는 기기에서만 표시). 켜면 해당 비디오 포맷으로 세션을 리셋 없이 재개한다.
- 정지(링 ≥ 2장) + 발열 정상일 때 키프레임에서 `ARSession.captureHighResolutionFrame` 요청. 스틸의 `camera.transform`을 cameraTransform으로 쓰고, **깊이는 일반 프레임 것**이므로 두 포즈 차이가 1 cm·0.5° 미만일 때만 채택(`StillCapture.poseCompatible`). 아니면, 실패하거나 2초 안에 안 오면 조용히 일반 이미지로 대체.
- JPEG q 0.88, 긴 변 ≤ 4032, **6 MB 초과 시 품질 하향 → 해상도 25% 축소** (서버 rgb 한도 12 MB).
- 내부 파라미터는 저장한 이미지의 픽셀 격자 기준으로 스케일: fx,cx는 W'/W, fy,cy는 H'/H 배. 규약(spatial_core `camera.py`): 정수 i 픽셀의 중심 = i+0.5, cx/cy는 좌상단 **모서리** 기준 → 연속 좌표라 단순 비례이며 ±0.5 보정은 필요 없다. `imageWidth/Height`는 저장 JPEG 크기. 메타 `highResStill: true`.

## 4. 메타 확장 (계약 §4 호환, 모두 선택 키)
`sharpness`, `exposureOffset`, `ambientIntensity`, `ambientColorTemperature`, `trackingReason`(limited일 때), `depthFramesAveraged`, `highResStill`. 기존 `exposureDurationS`, `whiteBalanceK`, `lightEstimateLux`, `motionBlurScore`는 그대로. 서버는 meta JSON을 그대로 저장하므로 서버 변경 불필요. (현재 키프레임은 추적 normal에서만 만들어지므로 `trackingReason`은 보통 비어 있다.)

## 5. HUD / 발열
- 품질 알약: 선명 비율(통과한 창/전체 창), 평균 깊이 신뢰도(0~2), 시간평균 적용 키프레임 %, 고화질 장수.
- `thermalState >= .serious` 이면 자동 일시정지 + 안내. 이미 쓰기 중이거나 저장된 프레임은 유지·업로드 대기열에 남는다. 아직 뜨거우면 [이어서]도 거절.

## 실기기에서만 확인 가능한 것
- 선명도 임계값(0.65/0.20 상대 기준, 15 하한)과 창 길이가 실제 흔들림 판정과 맞는지 — 로그의 `sharpness`·`motionBlurScore` 분포로 튜닝.
- 고해상도 포맷 사용 시 sceneDepth 지속, 프레임레이트 저하, `captureHighResolutionFrame` 지연(대개 0.2~0.5 s)과 포즈 차이 통과율, 스틸의 intrinsics 값.
- 정지 판정 임계값, 시간평균의 실제 노이즈 감소량과 가장자리 번짐.
- 발열 자동 일시정지 동작, Laplacian 계산(메인 액터)의 프레임 시간 영향.
