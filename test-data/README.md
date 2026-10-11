# test-data — E2E 픽스처 (실서버 검증 완료)

서버 실행 후 그대로 재현할 수 있는 입력 예시들. 2026-10-06 실HTTP 검증 결과를 주석 대신 기록한다.

| 파일 | 용도 | 검증 결과 |
|------|------|-----------|
| `mobile-package-r12.json` | `POST /api/v1/packages` (Revit 대체 메타, quat 경로 포함) | 201 — imported 2건 |
| `measurement-pass.json` | 정상 설치 (+5mm) | PASS / HIGH / dx≈4.9mm |
| `measurement-fail-27mm.json` | 지시서 Demo 2 (+27mm) | FAIL / HIGH / dx≈26.9mm |
| `measurement-few-points-retake.json` | 점 부족 (40개) | RETAKE / LOW |

## 재현 방법

```bash
# 저장소 루트에서
make api &                                  # 백엔드 기동 (8111)
curl -X POST localhost:8111/api/v1/projects/PJT001/spaces \
     -H 'Content-Type: application/json' -d '{"spaceId":"MECH-301"}'
curl -X POST localhost:8111/api/v1/packages \
     -H 'Content-Type: application/json' -d @test-data/mobile-package-r12.json
# 자산 GLB는 앱 스캔으로 등록하거나 backend/tests/conftest.py의 make_box_glb 참조해 생성
curl -X POST localhost:8111/api/v1/measurements \
     -H 'Content-Type: application/json' -d @test-data/measurement-pass.json
```

측정 픽스처의 자산 좌표계: 0.4m 박스, designPose (2.0, 1.0, 0.5), 자산 로컬 = 중심 원점.
`measurement-*.json`은 자산 `SCAN-BOX-001`을 가정한다.
