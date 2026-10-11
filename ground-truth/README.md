# ground-truth — 정확도 검증용 Ground Truth (지시서 15절)

"정확도 보고는 Ground Truth 확보 전까지 금지"가 원칙. 본 PoC는 실측 GT 미확보 상태이므로
`docs/REPORT.md`의 수치는 **합성 데이터 정합 성능**임을 명시한다. 현장 GT 캠페인 착수 시
아래 템플릿으로 측정하고, 불확도 모델(spatial-core: unc_pos = max(3mm, 2×RMSE), unc_rot = 0.5°)을
실측으로 보정하는 것이 PoC의 다음 단계이다.

## 측정 절차 (제안)

1. 검수 대상 사물 선정 (10개 이상, 크기/형상 다양하게)
2. 레이저 거리계/와이어 스케일로 기준 치수 측정 — 설계 도면 대비 **실제 설치 위치**를 기록
3. iPhone으로 각 사물 3회 이상 재스캔 → `POST /api/v1/measurements`
4. 서버가 보고한 편차(dx/dy/dz mm, 회전 도)와 GT 편차의 차 = 시스템 오차
5. 오차 분포 → 불확도 모델 회귀 보정 → CONFIDENCE 캘리브레이션

## template.csv

| 컬럼 | 의미 |
|------|------|
| asset_id | 자산 ID |
| gt_dx/dy/dz_mm | GT 설치 편차 (mm, 공간 프레임 X/Y/Z) |
| gt_rot_deg | GT 설치 회전 편차 (도) |
| sys_dx/dy/dz_mm | 시스템 보고 편차 (측정 결과 JSON에서 복사) |
| sys_rot_deg | 시스템 보고 회전 편차 |
| err_mm / err_deg | 오차 = sys − gt (계산 열) |
| rmse_m, inlier_ratio, confidence | ICP 지표 (Observation extra.icp) |
| device, captured_at | 기기/시각 |
