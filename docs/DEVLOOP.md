# DEVLOOP — 개발 폐루프 (실기기 실행 → 로그 수집 → 분석·수정 → 재배포)

사람은 폰에서 조작만 하고, Claude가 로그를 읽어 원인을 찾아 고친다.

```
 폰(앱) ──DebugLog──▶ POST /api/v1/debug/log ─┐
 서버 미들웨어(요청·4xx/5xx 본문·예외 스택) ───┴▶ backend/logs/dev.log (JSONL)
 기기 크래시(.ips) ◀── tools/devloop.sh crashes
```

## 준비 (1회)

- 서버: `make api` — `MARKERLESS_DEBUG=1`로 기동되어야 로그가 쌓인다 (테스트에서는 꺼짐)
- 앱 DEBUG 빌드(Xcode 기본 Debug 구성)만 서버로 로그를 보낸다. 서버 주소는 앱 "공간" 탭의 값(`baseURL`)을 사용
- 배포는 Xcode 앱 경유: 터미널 `xcodebuild` 서명은 로그인 키체인 접근 문제(`errSecInternalComponent`)로 실패

## 한 바퀴

```bash
tools/devloop.sh status     # 서버·기기·설치 점검
tools/devloop.sh mark       # 로그에 새 시도 구분선
tools/devloop.sh deploy     # Xcode로 실기기 빌드+설치+실행 (Xcode 앱 켜져 있어야 함)
# ── 폰에서 시나리오 조작 ──
tools/devloop.sh logs 80    # 서버+앱 통합 로그
tools/devloop.sh errors     # ERROR/FATAL만
tools/devloop.sh crashes    # 새 크래시(.ips) 요약 (스택 상위 프레임)
```

기타: `launch`(앱 실행), `clear`(로그 비우기), `make devlog`(tail -f).

## 로그 형식 (JSONL, 한 줄 = 한 이벤트)

| 필드 | 의미 |
|------|------|
| `src` | `app`(폰) / `server` / `dev`(구분선) |
| `lvl` | INFO / ERROR / FATAL |
| `tag` | app: `api` `scan` `inspect` `space` `ar` `crash` / server: `http` `exception` |
| `msg`, 기타 | 메시지·상태코드·응답본문(`body`)·스택(`trace`)·추가 정보(`extra`) |

앱이 남기는 주요 이벤트: API 요청 결과(상태·ms)와 실패 본문, 화면 진입(공간 선택 상태·LiDAR 여부),
AR 추적 상태 변화(특징점 수), 중심 지정 성공/실패, GLB 생성(삼각형 수), 등록/측정 성공·실패, 치명 예외(다음 실행 때 전송).

## 한계

- 시그널 크래시(배열 범위 초과 등)는 앱이 스스로 못 보내므로 `crashes`로 `.ips`를 가져와야 한다
- 폰이 서버에 닿지 않으면(Wi-Fi 다름) 앱 로그는 유실된다 — 서버 로그(`src: server`)로 요청 도달 여부를 먼저 확인
- Release 빌드는 서버로 전송하지 않는다 (`#if DEBUG`)

## 외부 URL 공개 (어디서나·외부 시연)

폰이 Mac과 같은 Wi-Fi가 아니어도(LTE, 다른 망) 접속하도록 Mac Studio 서버를 공개 URL로 연다.

```bash
make api                          # 서버 (API 키 자동 적용: .api-key)
tools/devloop.sh tunnel start     # Cloudflare 임시 터널 → https://xxxx.trycloudflare.com
tools/devloop.sh deploy           # 현재 URL·API 키를 앱 기본값으로 주입해 재배포
```

- 앱 `① 공간` 탭의 **🌐 외부 URL로 설정** 버튼 한 번이면 주소와 API 키가 채워진다 (긴 주소를 손으로 칠 필요 없음).
- **API 키 필수**: `/api/*`는 `X-API-Key` 헤더가 없으면 401. 키는 `.api-key`(gitignore). 공개 URL은 키를 아는 사람만 쓴다.
- 임시 터널 URL은 터널을 다시 켤 때마다 바뀐다 → 바뀌면 `deploy`로 앱 기본값 갱신. 중지: `tunnel stop`.
- **고정 URL이 필요하면(시연용 권장)**: Tailscale Funnel — tailnet 관리자가 1회 활성화
  (https://login.tailscale.com/f/funnel?node=nbmh7gNZH111CNTRL) 후 `tailscale funnel --bg 8111` → 고정 `https://wizbase-macstudio.<tailnet>.ts.net`
- 앱은 일시 오류(타임아웃·연결 끊김·502/503/504)를 3회 재시도하고, 연결이 복구될 때까지 대기한다.
- 이 Mac의 시스템 DNS(Tailscale MagicDNS)는 새 trycloudflare 주소를 늦게 풀 수 있다 — 폰 접속과 무관.

## 검수 체계 (사물 대 사물 정합 + 자동 매칭)

```
검수 점(배경 포함) ─▶ POI 기준 사물 분리(등록과 동일) ─▶ 크기 서명(L,W,H)으로 후보 선별 ─▶ 4자유도 정합 ─▶ 판정
```

- **분리**: 검수 점에서도 지지면·벽 제거 + 적응형 연결 성분으로 사물만 추린다 (원본 점 전체를 맞추면 배경이 정합을 끌어간다).
- **정합**: `align_object` — yaw 격자(10°) + OBB 방위 후보 × 위치 후보(조준점·관측 AABB 중심·무게중심) → 4자유도(yaw+이동) ICP.
  중력 정렬을 가정해 기울기는 풀지 않는다. 일치 거리 τ는 사물 크기에 비례(0.07·대각선).
- **자동 매칭** (`autoMatch=true`, `spaceId`): 크기 서명이 비슷한 후보 최대 4개를 정합해 가장 잘 맞는 자산을 선택.
  크기 차이·일치율·점수 기준을 못 넘으면 422 "닮은 사물이 없습니다". 자산을 고정(`assetId`)할 수도 있다.
- **월드맵 기본 켜짐**: 재위치 인식이 성공하면 설계 포즈를 prior로 쓰고 위치 편차까지 판정. 12초 안에 복원이 안 되면 자동 해제.
  (공간당 월드맵은 하나 — 같은 공간에 여러 장소를 등록하면 마지막 등록 장소의 지도만 남는다)
- **눈으로 확인**: 맞춘 모델 선화(판정 색) + 관측한 사물 윤곽(노랑)을 겹쳐 표시. 결과 카드 위에 일치율·잔차·방위·크기비 표시.
- 합성 장면 검증(`backend/tests/test_auto_match.py`): 위치 오차 < 1cm, 방위 오차 < 0.6°, 무관한 사물은 거부.

## 등록 품질 (사물 외관 인식 정교화)

검수 일치율은 등록 모델의 품질이 결정한다. 등록 파이프라인:

```
특징점(여러 프레임 평균·게이팅) ─▶ POI 사물 분리(국소 지지면·적응형 연결) ─▶ 정제(스무딩·법선) ─▶ 품질 평가 ─▶ 모델
```

- **점 품질 (앱)**: 3프레임 이상 관측된 점만 사용, 위치는 관측 평균(이후 지수 평균). 추적이 불안정하거나
  카메라가 빠를 때(0.5m/s·57°/s 초과)는 점을 기록하지 않는다.
- **분리 안정화 (서버)**: 지지면은 POI 주변(반경 ≥45cm)에서만 찾고, 탁자·바닥처럼 넓게 퍼진(95%≥25cm) 평면만 채택 →
  스캔 범위를 바꿔도 같은 결과. 연결 반경은 적응형(연속 2단계 정체 시 정지, 누출은 max_radius 기준).
- **정제 (서버, refine=true)**: 국소 평면 스무딩(이동 ≤1.2cm) + 사물 바깥쪽 법선 → 앱이 그대로 surfel 모델 생성.
- **품질 평가 (서버, assessment)**: 방향별 면 확보(그 면 *중앙부*에 점이 있어야 함, 얇은 단면은 한 면으로 합산),
  방위 커버리지, 밀도(점/㎡), 노이즈(mm), 상자형 정도, 보완 안내(tips). 점수 0~100, 면 ≥3·70점이면 good.
- **저장 전 확인 (앱)**: 확보된 면이 3개 미만이거나 poor면 "일부 면만 스캔됨" 확인창 (그래도 저장 가능).
  한쪽 면만 있는 모델은 다른 방향 검수에서 크기·각도가 어긋나고 월드맵 편차 판정이 FAIL로 나올 수 있다.

## 사진 실루엣 카드 + 엔지니어링 선화

BIM/CAD가 없어 스캔으로 만든 임시 자산에 **사진 기반 흑백 실루엣**을 입혀 매칭이 실물처럼 보이게 한다.

- **키프레임 (앱)**: 스캔 중 카메라가 12cm 또는 8° 움직일 때마다 960px JPEG + 자세·내부 파라미터 저장 (최대 140장, 약 14MB).
- **카드 생성 (앱, `PhotoCardBuilder`)**: 방향(앞·오른쪽·뒤·왼쪽)마다 그 쪽에서 사물을 가운데 두고 본 프레임을 골라
  ① 시선에 수직·세계 위가 위인 평면(사물 중심 깊이)으로 **정면 보정**(호모그래피) → ② 3D 사물 점을 평면에 투영한 영역과
  **Vision 전경 인스턴스**를 교차해 사물만 오림(Vision 실패 시 투영 영역만) → ③ 흑백 + 자동 대비 + 알파(윤곽 페더링) PNG.
  촬영 자세에서는 사진이 실물과 정확히 겹치고, 다른 각도에서도 똑바로 선 카드가 된다.
- **저장 (서버)**: `PUT /assets/{id}/photos/{side}` (meta + PNG), `GET /assets/{id}/photos`, `GET /assets/{id}/photos/{side}.png`.
  카드 배치는 자산 로컬 프레임 — 검수 때 맞춘 포즈에 그대로 얹힌다. 재등록·삭제 시 함께 정리.
- **검수 표시**: 현재 보는 방향과 가장 가까운(≈56° 이내) 카드 한 장만 겹쳐 보인다.
- **엔지니어링 선화**: 윤곽선 0.8mm·외곽 박스 1.2mm의 머리카락 굵기, 박스의 가로·깊이·높이에 **치수선(보조선·사선 눈금) + mm 표기**
  (글자는 매 프레임 카메라를 향함).
- **검증**: `PhotoCardBuilder`는 합성 장면(기울어진 체크 평판)으로 Mac에서 검증 — 카드와 실제 텍스처 상관 0.95, 뒤집힘 없음.
  알파 PNG 투명도(UnlitMaterial 블렌딩)는 실기기에서 확인이 필요하다.

## 검수 2단계: 자산 찾기 → 후보 선택 → 검수

- `POST /api/v1/match` (spaceId, cloud, hintCenter): 판정·저장 없이 **후보 순위**(유사도 0~100, 일치율, 크기비, accepted)만 반환.
  유사도 = 일치율 × exp(−1.5·크기벌점) × (1 − 0.5·점수/0.6). 크기 서명(L,W,H)으로 후보를 거른 뒤 정합한다.
- 앱 `③ 검수`: `[자산 찾기]` → 후보 패널(사진 실루엣 썸네일·유사도 막대·추천 표시) → 후보의 `[검수]` →
  그 자산을 고정하고 **같은 스캔으로 바로 검수**(`/measurements`, assetId 지정). 닮은 자산이 없으면(accepted=false)
  그래도 후보를 보여 줘 사용자가 직접 고를 수 있다.
- **조준 사물 주변 점 N/60**을 실시간 표시하고 부족하면 버튼을 막는다 (폰을 고정하면 서로 다른 점이 50~100개뿐 —
  좌우로 이동하며 비춰야 늘어난다). 검수 화면은 안정 점 기준을 2프레임으로 낮춰(등록은 3) 더 빨리 쌓인다.
- 사진 카드: 등록 직후 4방향 썸네일 표시, 검수에서는 설계 위치(복원 시)와 맞춘 결과 위에 보는 방향에 가까운 한 장을 표시.
  표시가 바뀔 때마다 로그(`ar: 사진 카드 표시 front(0.82)`)를 남긴다.

## 통합 백오피스 (`/admin/`)

- 같은 서버·터널에서 제공: `https://<터널>/admin/` (터널 URL은 `tools/devloop.sh tunnel url`). 정적 SPA(빌드 없음), API는 `/admin/api/*`.
- 인증: 세션 쿠키 `mk_admin`(HttpOnly·SameSite=Strict) + 변경 요청에 `X-Requested-With: markerless-admin`. 단말 API(`/api/v1/*`)는 기존 `X-API-Key` 그대로.
- 기본 계정 `admin` (환경변수 `MARKERLESS_ADMIN_USER/PASSWORD`로 시드). **운영 전 시스템 설정 → 비밀번호 변경 필수.**
- 역할: admin(통합관리자) / manager(현장관리자) / inspector(현장 검수자) / viewer(조회 전용). 로그인 5회 실패 시 5분 잠금.
- 이슈 워크플로우: OPEN→CONFIRMED→ASSIGNED→IN_PROGRESS→RESOLVED→REINSPECT→CLOSED (+DISMISSED). 검수 FAIL/REVIEW 시 자동 생성, 종결은 재검수 PASS 필요(아니면 관리자 강제 종결).
- 검수별 3D 리뷰 데이터는 `storage/reviews/{id}.json` (이전 관측에는 없음).
- API 문서(/docs)는 기본 비활성 (`MARKERLESS_DOCS=1`로 켬).
- 도구: `tools/seed_demo.py`(데모 데이터), `tools/ui_check.py <url> <out>`(헤드리스 Chrome으로 전 화면 콘솔 오류 점검).
  데모는 별도 포트/임시 DB로 띄울 것(실서버 DB에 시드 금지).

## 고정 외부 URL (frp) — https://markerless.wizbase.ai.kr
- 맥 → frps(140.238.31.159:7000, nginx가 TLS 종단·Host 라우팅) → `127.0.0.1:8111`. 다른 프로젝트와 같은 공유 frps/토큰.
- 설정: `~/.config/frp/frpc-markerless.toml` (토큰 포함, 커밋 금지), 서비스: LaunchAgent `com.wizbase.frpc-markerless` (부팅 시 자동, KeepAlive), 로그 `~/.config/frp/frpc-markerless.log`.
- 제어: `launchctl bootout gui/$(id -u)/com.wizbase.frpc-markerless` (중지) / `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wizbase.frpc-markerless.plist` (시작).
- 백오피스 `https://markerless.wizbase.ai.kr/admin/`. nginx가 X-Forwarded-Proto를 안 줘도 `*.wizbase.ai.kr` 호스트면 쿠키에 Secure를 붙인다 (`MARKERLESS_SECURE_HOSTS`).
- `tools/devloop.sh deploy` 는 frpc가 떠 있으면 앱 기본 외부 URL을 이 도메인으로 생성한다 (Cloudflare 임시 터널은 백업용).

## 검수 표시 설정 · 사진 실루엣 64컷
- 백오피스 자산 상세 → **검수 표시 설정**: 모델 표면·점군·외곽선·외곽 박스·치수·사진 실루엣 중 검수 때 앱이 그릴 레이어를 자산별로 저장
  (`PATCH /admin/api/assets/{id}` `inspectView`, 전체 적용 `POST /admin/api/assets/inspect-view`, 매니저 이상). 기본 = 박스·치수·실루엣.
  단말은 `GET /api/v1/assets/{id}`의 `inspectView`를 읽는다 (없으면 기본). 실루엣을 끄면 앱은 카드를 내려받지 않는다.
- 사진 실루엣 = 사진이 아니라 3D 점의 투영 실루엣(가상 카메라 1.0m, 수평), 둘레 64컷(5.625°), 이름 `a00..a63`(앞=a00, 시계 방향), 흑백 가는 외곽선(약 2px, 거리 기반 안티앨리어싱).
  서버는 `a00..a95` + 구 `front/right/back/left`를 받는다. 기존 자산(4컷·24컷)은 재등록해야 64컷이 된다.
- 스캔 화면 둘레 링(24칸)은 한 바퀴 돌았는지 안내용(16칸 이상 권장).
- 카드 생성기 오프라인 시험: `PhotoCardBuilder.swift`는 Foundation만 써서 `swiftc PhotoCardBuilder.swift main.swift`로 Mac에서 단독 실행 가능.

## 앱 설치·빌드 번호 (tools/ios_install.sh)
- `tools/ios_install.sh "iPhone 13 Pro"` — 모델명으로 기기를 골라(이름이 같아도 구분) 빌드 → 설치 → **기기에 설치된 번호를 `devicectl`로 읽어 확인**까지 한다. Xcode GUI 불필요.
- 빌드 번호 = `yyMMdd.HHmm`. 앱 **왼쪽 위 작은 번호**, ① 공간 탭 "앱 정보", 서버 로그의 `build` 필드, 앱 정보(CFBundleVersion)가 모두 같은 값. 설치 후 이 번호가 방금 빌드와 같은지 비교하면 최신 앱인지 알 수 있다.
- `BuildInfo.swift`는 빌드 때마다 project.yml의 '빌드 번호 생성' 스크립트가 덮어쓴다(수정 금지, `ENABLE_USER_SCRIPT_SANDBOXING: NO` 필요).

## 2026-10-08 GLB 좌표 손상 버그 (iOS)
- 증상: 모델이 가시처럼 삐죽, 크기·외곽 박스가 실물보다 크게(28cm 사물 → 49cm 정육면체), 정합 회전 오판. 폰으로 등록한 모든 자산이 해당.
- 원인: `GLBWriter`가 `SIMD3<Float>`(메모리 16바이트)를 통째로 써서 glTF VEC3(12바이트)와 어긋남 → 서버가 읽으면 좌표 뒤섞임. `GLBReader`·ARKit 버퍼 읽기도 같은 패턴이라 float 단위로 수정.
- 서버 방어: `read_glb_mesh`가 정점 버퍼 길이(count×12)를 검사해 깨진 GLB는 422("앱을 최신 빌드로…")로 거부. 백오피스 자산 목록·상세에 **모델 손상** 표시(`modelOk`). 손상 자산은 재등록해야 한다.
- 교훈: SIMD 타입을 바이트로 직렬화하지 말 것 (`MemoryLayout<SIMD3<Float>>.size == 16`).

## 대상 먼저 지정 스캔 (② 스캔·등록 재설계, 빌드 261008.1333~)
- 흐름: **대상 지정**(십자선을 사물 한가운데에 → 표면에서 4cm 안쪽에 중심) → **둘레 스캔**(중심 기준) → **분리 확인** → **모델 저장**.
- 중심 기준 효과: LiDAR 깊이 누적을 대상 반경 1.3m 안으로 제한(`DepthFusion.setFocus`, 주변 사물·벽 배제), 대상 주변은 전 픽셀(stride 1) 사용.
- 중심 보정: ① 스캔하는 동안 3초마다 서버 분리(`/scan/segment`)로 초록 외곽선 미리보기 + 중심을 사물 AABB 중앙으로 자동 보정(1회 ≤12cm),
  ② [중심 보정] 패드로 화면 기준 1cm/3cm 미세조정(손대면 자동 보정 꺼짐), ③ 3D 마커(3축 십자+링+기둥)로 어느 각도에서도 중심 확인.
- 둘레 지도 `ScanCoverage`(24방위×3고도, 시작 위치=위쪽): 새 칸이 채워질 때 진동, 한 바퀴(22칸) 완료 시 성공 진동.
  어시스트 한 줄 우선순위: 추적 불안정 → 너무 빠름 → 거리(0.28~1.7m) → 조준 → (서 있는 칸 비었으면 "잠시 더") → 가까운 빈 방위로 좌/우 이동 → 윗면 → 완료.
  분리 확인 버튼은 12칸 이상부터 활성, 저장 전 12칸 미만이면 확인 대화상자.
- 분리 확인 화면: 돔 지도 + "추가 카메라 워크가 필요합니다"(비어 있는 방향·윗면·서버 평가 팁), [더 스캔]은 같은 대상으로 스캔을 이어 간다.
- 화면 가림 최소화: 상단 알약(단계, 누르면 전체 안내) + 우상단 눈(👁) 버튼 + 작은 썸네일(점 이미지+돔 지도, 탭하면 축소). 눈 버튼으로 안내 전체를 숨김/표시 (스캔·검수 화면이 `hud.hidden` 설정을 공유, 기억함). 오류와 조작 버튼은 숨김에서도 표시.
- 시험: `ScanCoverage.swift`는 UI 무관이라 `swiftc ScanCoverage.swift main.swift`로 Mac에서 궤도 시뮬레이션 가능, `DomeMap`/`TopViewDots`는 `ImageRenderer`로 PNG 렌더 확인.

## 설치가 안 될 때 (2026-10-08)
- 증상 A "기기가 잠겨 있다"(`FBSOpenApplicationErrorDomain error 7 / Locked`): 설치는 됐지만 **실행**만 막힌 것 — 폰 화면이 꺼졌거나 잠겼을 때. 잠금을 풀고 `tools/devloop.sh launch`.
- 증상 B "연결된 실기기 대상을 Xcode에서 찾지 못했습니다(-2700)": 예전 `deploy`가 Xcode 창에서 기기 **이름**으로 대상을 골랐는데, 13 Pro와 iPhone Air의 이름이 같아("iPhone 13 pro bkim (3)") 실패.
  → `deploy`를 CLI 방식(`tools/ios_install.sh`, 모델명/UDID로 선택, 설치된 빌드 번호 검증, 자동 실행)으로 교체. 예전 방식은 `deploy-xcode`.
- 점검 순서: `xcrun devicectl list devices`(State=connected) → `devicectl device info details --device <UDID>`(Pairing=paired, Developer Mode=Enabled) → `tools/devloop.sh deploy` (기기 지정: `DEVLOOP_MODEL="iPhone 13 Pro"`).

## 검수 표시 설정이 안 먹을 때 (2026-10-08 점검)
- 원인 ① 백오피스의 [설정 저장]이 일반 [저장]과 따로라 눌러도 서버에 안 갔음(감사 로그 `asset.update`에 inspectView 기록 없음) → **체크하면 자동 저장**으로 변경, 일반 [저장]에도 포함.
- 원인 ② 3D 화면이 실루엣 64장을 동시에 요청 → DB 연결 풀(기본 15) 고갈 `QueuePool limit … reached` → 사진 요청 120건 500 → 풀 확대(30+60) + 화면은 8장씩 요청.
- 원인 ③ 앱이 같은 자산을 다시 검수할 때 예전 설정을 계속 씀 → 판정 때마다 자산 정보를 다시 읽어 반영.
- 모바일 앱 현장 선택: ③ 검수 화면 하단 칩(외곽선·박스·치수·실루엣·표면·점군) — 누르면 즉시 다시 그림, [서버 설정]으로 복귀. 👁 버튼으로 칩 포함 안내 전체 숨김.
- 끝단 점검: `.venv/bin/python tools/e2e_inspect_view.py http://127.0.0.1:8111 [자산ID]` (브라우저 클릭 → 저장 → 모바일 API 값 확인 → 원복).

## 검수 화면 선 색 규약
- **외곽선(높이별 윤곽)** = 판정색(앱: 설계 파랑 / 합격 초록 / 불합격 빨강 / 재확인 주황, 백오피스: 청록) — **외곽 박스·치수(mm)** = 보라(`#c77dff`) — 관측한 사물 윤곽 = 노랑 — 사진 실루엣 = 흰색 가는 선.
- 앱 `boxLineColor`(ARScanView.swift), 백오피스 `viewerui.js`·레이어 칩·범례가 같은 색을 쓴다. 앱 검수 화면 칩에도 같은 색 점이 붙는다.

## 외곽선에 수직선이 없던 이유 (2026-10-08)
- 외곽선(`extract_outline`)은 원래 **높이별 가로 단면(등고선)** 만 만들었다 — 스캔 방향과 무관, 수직으로 스캔해도 수직선은 생기지 않는 설계였다.
- 이제 u축·v축에 수직인 얇은 판으로 자른 **수직 단면 윤곽**을 함께 만든다 (`vsections`, 상한 9000선). 서버 외곽선 캐시는 `*.outline.v2.json`으로 갱신(옛 캐시 무시).
- 앱은 서버에서 외곽선을 받아 그리므로 재설치 없이 반영된다 (검수 화면에서 자산을 다시 불러올 때).

## 저장 시점 실시간 동기화 + 공간 추가·수정·삭제 (2026-10-08)
- **주기적 확인(polling)은 없다.** 저장(DB 커밋)이 일어난 그 시점에 서버가 이벤트(SSE)를 내보내고, 연결된 앱·백오피스 화면이 그때만 새로 읽는다.
  - 서버: `app/events.py` — SQLAlchemy 세션 훅이 Asset/Space/관측/이슈의 커밋을 감지해 `broker.publish`(0.35초 안의 연속 변경은 한 번으로 합침, 롤백은 알리지 않음). 사진 업로드(파일)는 직접 publish.
  - 단말(앱): `GET /api/v1/sync/events` (X-API-Key). 연결 직후 `hello`(+`revision`), 변경 시 `change`(scopes: assets·spaces·inspections·issues). 20초 무변경이면 연결 유지용 `: ping`(데이터 확인 아님).
    앱은 연결을 열어 두고(`AppState.eventLoop`) 이벤트 때만 공간·자산 정보를 다시 읽고 `dataRevision`을 올린다 → ① 공간 · 자산 관리 · ③ 검수 화면이 스스로 새로 읽음. 끊기면 2→30초 간격으로 재연결(연결 복구일 뿐), 연결될 때마다 한 번 따라잡기(`GET /sync/state`와 같은 revision).
    앱이 백그라운드로 가면 연결을 닫고, 돌아오면 다시 연다.
  - 백오피스: `GET /admin/api/events` (로그인 세션) → `EventSource`. 목록·보드·대시보드·공간은 새로 그리고, **입력 중·창이 열림·일괄 선택 중·상세 화면(3D 카메라)** 은 건드리지 않는다. 모바일에서 등록·검수한 내용이 기록 시점에 목록에 나타난다.
  - 프록시: `X-Accel-Buffering: no`, 외부 도메인(nginx→frps) 경유 0.4초 내 도착 확인.
- 모바일 반영 범위: 공간 추가/이름/ID/삭제(선택한 공간이 지워지면 선택 해제+안내), 자산 이름(`AssetOut.name`)·보관(ARCHIVED는 목록·자동 매칭에서 제외)·삭제·공차·검수 표시 설정·재스캔 모델. 검수 중인 자산은 표시 설정·외곽선·실루엣까지 즉시 다시 그림(모델이 재등록되면 전부 다시 받음).
- 공간(백오피스 → 공간): **추가**(`POST /admin/api/spaces`, 매니저↑), **수정**(ID·이름·층, 매니저↑), **삭제**(통합관리자만; 자산이 있으면 409 → 확인 후 `?cascade=true`로 자산·검수 기록·사진·모델·월드맵까지 삭제, 이슈는 보존하고 공간 연결만 해제).
- 자산 목록에서 **이름 바로 수정**: 연필 버튼 또는 이름 더블클릭 → Enter 저장 / Esc 취소.
- 시험: `test_space_crud_and_mobile_sync_revision`, `test_change_events_fire_on_commit_not_before_and_coalesce`.

## 무선(Wi-Fi) 설치 — USB 없이 배포
1. (처음 한 번) 폰을 USB로 연결 → Xcode → Window → **Devices and Simulators** (⇧⌘2) → 왼쪽에서 폰 선택 → **Connect via network** 체크.
   (명령줄에는 이 옵션이 없고, 자동 조작은 macOS "손쉬운 사용" 권한이 필요해 쓰지 않는다)
2. 폰과 Mac이 **같은 Wi-Fi/같은 대역**(예: 192.168.2.x)인지 확인, 폰의 "로컬 네트워크" 권한 허용.
3. USB를 뽑는다. `xcrun devicectl list devices`에서 폰이 `available (paired)`(전송=localNetwork)로 보이면 성공.
4. 이후 `DEVLOOP_MODEL="iPhone 13 Pro" tools/devloop.sh deploy` — 출력 첫 줄이 `(무선(같은 Wi-Fi))`로 나온다. 설치·번호 확인은 USB와 같다.
- 무선에서 앱 실행이 안 되면: 폰 화면을 켜고 잠금 해제 → `tools/devloop.sh launch`. 설치가 느리면 Wi-Fi 신호/대역(5GHz)과 Mac이 유선이면 공유기 AP 격리 설정을 확인.

## 검수 이력 선택 삭제
- 백오피스 검수 이력: 행 체크박스(헤더 = 현재 페이지 전체 선택, 페이지를 넘겨도 선택 유지) → **선택 삭제** (`POST /admin/api/inspections/bulk-delete`, 통합관리자만).
- 삭제 범위: 관측·판정 결과·3D 리뷰 데이터(`reviews/{id}.json`). 연결된 이슈는 남기고 검수 연결만 끊고 이력(`edit`)에 기록, 증거 원본은 보존. 감사 로그 `inspection.bulk_delete`.
- 시험: `test_bulk_delete_inspections_cleans_results_review_and_keeps_issues`.

## 관리자 웹 모바일 점검

폰 폭(390px)에서 모든 화면의 가로 넘침·글자 깨짐(좁은 칸에서 한 글자씩 줄바꿈)·작은 터치 대상을 찾고 전체 페이지 스크린샷을 저장한다.

```
.venv/bin/python tools/ui_mobile.py http://127.0.0.1:8111 /tmp/shots-m admin '<비밀번호>' [화면이름,...]
```

- 좁은 화면(≤700px)에서 표는 카드 목록으로 바뀐다: `app.js`의 `labelTables()`가 칸마다 `data-label`을 붙이고 CSS가 카드로 그린다. 새 표를 만들 때 `<th>` 이름만 있으면 된다.
- 화면에서 인라인으로 `gridTemplateColumns`를 주는 다단 격자는 ≤900px에서 자동으로 한 줄로 쌓인다(`.grid[style*=grid-template-columns]`). 작은 칸을 유지하려면 `keep3` 클래스를 쓴다.
- 한글은 단어 중간에서 끊지 않는다(`word-break: keep-all`).


## 디지털 트윈 개발 루프 (2026-10-11)

| 도구 | 용도 |
|---|---|
| `tools/twin_server.sh [포트]` | **임시 DB**로 검증 서버(비밀번호 강제 변경 끔). 운영 서버(8111)와 분리 |
| `tools/seed_twin.py <base>` | 합성 방 3개+복도를 실제 API로 올려 데모 건물 생성(업로드→재구성→외피) |
| `tools/twin_check.py`, `twin_realism_check.py` | 헤드리스 Chrome 투어/렌더링 점검(오류·FPS·LOD·재질·스크린샷) |
| `tools/twin_bench.py [--quick]` | 합성 정답 벤치(기하·텍스처·재방문·드리프트·정규화·성능) → `logs/twin_bench.json` |
| `tools/depth_prior_bench.py` | 실제 RGB-D(NYU)로 깊이 기법 비교 |
| `tools/fetch_models.sh [--depth]` | AI 모델(선택) |
| `tools/pwchange_check.py` | 비밀번호 강제 변경 화면 점검(`MARKERLESS_ENFORCE_PW_CHANGE=1` 서버) |
| `tools/backup.sh` | DB·저장소 백업(보관 14개) |
| `tools/twin_stress.py <base>` | 동시 업로드·읽기 부하 시험(임시 서버) |
| `tools/pixel_ab.py <sessionId>` | 실기기 스캔으로 픽셀 규약(0.5/0) A/B 판정 |

**비밀번호 강제 변경**: 운영 서버(`make api`)는 기본/약한 비밀번호 계정이 로그인하면 변경 화면만 열린다. 로컬 검증 서버·테스트는 `MARKERLESS_ENFORCE_PW_CHANGE=0`.
기존 UI 점검 도구(`ui_check.py` 등)를 운영 서버에 쓰려면 먼저 비밀번호를 바꾸고 인자로 넘기거나, 임시 서버(`twin_server.sh`)를 쓴다.
