# AS_IS_AUDIT.md — 현행 시스템 감사 (2026-10-11, 디지털 트윈 확장 착수 전)

개발내역서(`markerless-development-2026-10-11.md`)의 주장과 **실제 소스·실행 결과**를 대조한 기록이다.
"구현되었다"는 문서상 주장과 "이 감사에서 직접 확인한 사실"을 구분한다.

## 1. 직접 확인한 사실
| 항목 | 문서 주장 | 감사 결과 |
|---|---|---|
| 자동 테스트 | HANDOFF: 73개 | **spatial-core 67 + backend 65 = 132개 통과** (HANDOFF/REPORT의 73은 구버전) |
| API 수 | REPORT: 25 | 현재 **81개 오퍼레이션**(관리자 API 포함) — `docs/baseline/api_baseline_2026-10-11.json` |
| DB 건수 | 3공간·7자산·16검수·10이슈·1사용자 | **일치** (공간 3, 자산 7, 검수 16, 이슈 10, 사용자 1, 프로젝트 1) |
| 자산 GLB 무결성 | "구 앱 빌드 GLB 손상 자산 있음" | 저장된 7개 GLB **모두 파싱 성공·유한 좌표**. 다만 *형상이 올바른지*는 별개 — 손상 의심 자산은 관리자 3D에서 육안 확인·재등록 필요 |
| Python | 3.11+ | 실행 환경 **3.14.6**. Open3D 0.20은 설치되나 시스템 libusb가 없어 로드 실패 → **Open3D 비의존**(TSDF는 numpy 자체 구현, 메시 추출은 scikit-image) |
| git | HANDOFF: 커밋 0개 | **여전히 커밋 0개 (전부 Untracked)** — 변경 이력 보호가 없다 |

## 2. 문서와 소스가 다르거나 확인되지 않은 부분
1. `docs/REPORT.md`, `HANDOFF.md`의 테스트/엔드포인트 수치는 오래됨 (위 표).
2. "약 1cm 자세 정확도"는 **합성 벤치마크**(`tools/pose_bench.py`)이며 실측 Ground Truth는 없다.
3. 자동 매칭 미등록 사물 오확정 약 63%는 `tools/match_bench.py` 합성 기준. 새 공간 스캔의 자산 자동 확정에 **재사용하지 않는다**(이 확장에서는 자동 확정 차단, 후보 제시만).
4. 앱의 RoomPlan·TSDF·텍스처 기능은 존재하지 않았다(신규).
5. 실기기(LiDAR) 검증은 사용자가 수행한 객체 스캔·검수 범위뿐이며, 공간 규모 스캔은 미검증.

## 3. 운영 위험 (확장 전에 다룰 것)
| 위험 | 현황 | 조치 |
|---|---|---|
| 약한 관리자 비밀번호 | 기본/약한 암호, 공개 인터넷 노출 | 비밀번호 정책 + 최초 변경 강제 (Sprint 0 보안 작업) |
| 로그인 시도 제한 | `auth.throttle_*` **이미 있음** | 유지·테스트 확인 |
| API 키 | 단일 키(`MARKERLESS_API_KEY`) | 단말별 토큰 전환은 PRODUCTION_GAP로 이관 |
| 단일 서버·SQLite | 개발 Mac + frp | 백업 스크립트 `tools/backup.sh` 추가, PostgreSQL은 PRODUCTION_GAP |
| 대용량 사진 저장 | 신규 | 업로드 SHA-256, 프로젝트별 ACL, 용량 상한, 사진 다운로드 관리자 인증 필수 |
| git 이력 없음 | 커밋 0 | 사용자 승인 후 체크포인트 커밋 권장 |

## 4. 보존 대상 확인 (회귀 기준)
- 기존 라우트 81개를 `docs/baseline/api_baseline_2026-10-11.json`에 스냅샷 → `backend/tests/test_api_contract.py`가 **삭제·개명되면 실패**.
- 백업: `backups/markerless-*.db`, `backups/storage-*.tgz` (`tools/backup.sh`로 주기 실행, 보관 14개).
- ARSession 카메라 점유: `ScanSessionController.active`/`pause()`/프레임 워치독으로 회귀 방지(수정 2026-10-11). 공간 스캔 모드도 같은 규칙을 따른다.

## 5. 확장 시 변경하지 않는 것
`/assets/scan`, `/segment`, `/match`, `/measurements`, `/packages`, 월드맵 PUT/GET, SSE, 3탭, 객체 POI 스캔, `PoseRefiner`, 관리자 대시보드/자산/검수/이슈, 감사 로그.
