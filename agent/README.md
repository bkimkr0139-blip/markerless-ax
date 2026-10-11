# agent — Astra (AI 검수 관제 에이전트) 스텁

개발지시서 10절의 Astra 에이전트. **원칙: Astra는 백엔드 API만 호출하고, 기하 계산(ICP/판정)은 절대 하지 않는다.**
판정의 단일 진실원천은 서버(지시서 7절 — 자체 좌표 계산 금지).

## 역할

| 기능 | 수단 |
|------|------|
| 검수 진행 질의·요약 | `GET /api/v1/inspection-results`, `GET /api/v1/reports/{id}` |
| RETAKE 안내 | 결과 decision=RETAKE인 observation 목록 조회 → 사용자 재측정 유도 |
| 현황 보고서 생성 | `POST /api/v1/reports` (서버가 계산한 요약을 포맷만) |
| 이상 탐지 트렌드 | 자산별 편차 추이 조회/요약 (계산은 서버가) |

## tools.json

에이전트에게 노출할 도구 목록 — 전부 백엔드 REST 프록시뿐이다.
`spatial_core`를 임포트하거나 수치를 직접 계산하는 도구는 의도적으로 없다.

## prompts/system-prompt.md

Astra 시스템 프롬프트 초안 — 도구 호출만으로 답하고, 좌표/기하 질문은 서버 결과를 인용해 답하도록 제한.
