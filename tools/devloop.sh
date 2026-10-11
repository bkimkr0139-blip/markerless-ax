#!/bin/zsh
# devloop.sh — 개발 폐루프: 배포 → 실행 → 로그/크래시 수집 (사람이 폰에서 조작, Claude가 로그 분석·수정)
#
#   tools/devloop.sh deploy    (DEVLOOP_MODEL="iPhone 13 Pro" 로 기기 지정 가능) CLI로 실기기 빌드+설치+설치 번호 확인 (tools/ios_install.sh)
#   tools/devloop.sh deploy-xcode  예전 방식(Xcode 앱 경유). 기기 이름이 같은 폰이 둘이면 대상을 못 찾을 수 있다
#   tools/devloop.sh logs [N]  서버+앱 통합 로그 마지막 N줄(기본 60), 사람이 읽기 좋게 포맷
#   tools/devloop.sh errors    ERROR/FATAL만
#   tools/devloop.sh crashes   기기의 새 Markerless 크래시(.ips)를 가져와 원인 요약
#   tools/devloop.sh launch    설치된 앱 실행
#   tools/devloop.sh mark      로그에 구분선 삽입 (새 시도 시작 표시)
#   tools/devloop.sh clear     로그 비우기
#   tools/devloop.sh status    서버/기기/설치 상태 점검
#   tools/devloop.sh tunnel [start|stop|url]   외부 공개 URL(Cloudflare 임시 터널) — API 키 필수(.api-key)

ROOT="${0:A:h:h}"
TUNLOG="${TMPDIR:-/tmp}/markerless-tunnel.log"
LOG="$ROOT/backend/logs/dev.log"
BUNDLE="kr.poc.markerless.Markerless"
PROJ="Markerless.xcodeproj"
CRASH_DIR="${TMPDIR:-/tmp}/markerless-crashes"
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer

# 앱 기본 접속값(외부 터널 URL·로컬 URL·API 키)을 DevDefaults.swift로 생성 — 폰에서 긴 주소/키를 손으로 치지 않게
write_defaults() {
  local tun="" ip key
  tun=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNLOG" 2>/dev/null | tail -1)
  pgrep -f "cloudflared tunnel --url" >/dev/null || tun=""
  # 고정 도메인(frps)이 우선: frpc-markerless 가 떠 있으면 그 주소를 쓴다
  pgrep -f "frpc-markerless.toml" >/dev/null && tun="https://markerless.wizbase.ai.kr"
  ip=$(ipconfig getifaddr en1 2>/dev/null || ipconfig getifaddr en0 2>/dev/null)
  key=$(cat "$ROOT/.api-key" 2>/dev/null)
  cat > "$ROOT/mobile-ios/Markerless/App/DevDefaults.swift" <<SWIFT
// DevDefaults.swift — 개발용 접속 기본값. tools/devloop.sh 가 배포 때마다 덮어쓴다 (gitignore, 비밀 포함).
enum DevDefaults {
    static let tunnelURL = "$tun"
    static let localURL = "${ip:+http://$ip:8111}"
    static let apiKey = "$key"
}
SWIFT
  echo "접속 기본값 생성: 외부=${tun:-없음} 로컬=${ip:-없음}"
}


# 연결된 실기기 선택. 기본=처음 연결된 기기, DEVLOOP_MODEL="iPhone 13 Pro" 처럼 모델명(부분 일치)으로 지정 가능
pick_device() {  # 출력: "<이름>\t<UDID>"
  local j="${TMPDIR:-/tmp}/devloop-devices.json"
  xcrun devicectl list devices --json-output "$j" >/dev/null 2>&1
  python3 -I -c '
import json, os, sys
want = os.environ.get("DEVLOOP_MODEL", "").lower()
for d in json.load(open(sys.argv[1]))["result"]["devices"]:
    hp, cp = d["hardwareProperties"], d["connectionProperties"]
    if hp.get("reality") != "physical" or cp.get("tunnelState") != "connected":
        continue
    model = (hp.get("marketingName") or "") + " " + (hp.get("productType") or "")
    if want and want not in model.lower():
        continue
    print(d["deviceProperties"]["name"] + "\t" + hp["udid"]); break
' "$j"
}
device_name() { pick_device | cut -f1; }
device_id()   { pick_device | cut -f2; }

fmt() { python3 -I "$ROOT/tools/fmtlog.py"; }

case "$1" in
  deploy)
    # 기본: Xcode 창 없이 CLI 빌드→설치→설치 번호 확인 (기기를 모델명/UDID로 고른다 — 이름이 같은 폰이 여러 대여도 안전)
    write_defaults
    "$ROOT/tools/ios_install.sh" "${DEVLOOP_MODEL:-}" || exit 1
    echo "폰에서 앱 실행·조작 후: tools/devloop.sh logs"
    ;;
  deploy-xcode)
    # 예전 방식: Xcode 앱으로 빌드/설치. 기기 '이름'으로 대상을 고르므로 이름이 같은 폰이 둘이면 실패할 수 있다.
    write_defaults
    osascript <<EOF
tell application "Xcode"
  activate
  open "$ROOT/mobile-ios/$PROJ"
  delay 2
  set wd to first workspace document whose name is "$PROJ"
  set picked to false
  repeat with d in (run destinations of wd)
    if (name of d) is "$(device_name)" then
      set active run destination of wd to d
      set picked to true
      exit repeat
    end if
  end repeat
  if picked is false then error "연결된 실기기 대상을 Xcode에서 찾지 못했습니다"
  run wd
end tell
EOF
    [ $? -eq 0 ] || exit 1
    echo "Xcode 빌드/설치 대기… (키체인 팝업이 뜨면 Mac에서 허용)"
    # Run 액션은 앱이 실행 중이면 계속 running 이므로, 빌드 로그의 최종 문구로 완료를 판정한다
    res="timeout"
    for i in $(seq 1 120); do
      blog=$(osascript -e "tell application \"Xcode\" to get (build log) of (last scheme action result of (first workspace document whose name is \"$PROJ\"))" 2>&1)
      case "$blog" in
        *"Build succeeded"*) res="succeeded"; break;;
        *"Build failed"*|*"BUILD FAILED"*) res="failed"; break;;
      esac
      sleep 3
    done
    echo "빌드 결과: $res"
    if [ "$res" != "succeeded" ]; then
      echo "$blog" | grep -iE "error|failed" | head -10
      exit 1
    fi
    echo "설치 완료 — 폰에서 앱 실행·조작 후: tools/devloop.sh logs"
    ;;
  logs)    tail -n "${2:-60}" "$LOG" 2>/dev/null | fmt ;;
  errors)  grep -E '"lvl": "(ERROR|FATAL)"' "$LOG" 2>/dev/null | tail -n "${2:-30}" | fmt ;;
  mark)    python3 -I -c 'import json,time,sys; open(sys.argv[1],"a").write(json.dumps({"ts":time.strftime("%H:%M:%S"),"src":"dev","lvl":"INFO","tag":"mark","msg":"──────── 새 시도 ────────"},ensure_ascii=False)+"\n")' "$LOG" ;;
  clear)   : > "$LOG"; echo "로그 비움" ;;
  launch)
    xcrun devicectl device process launch --device "$(device_id)" "$BUNDLE" 2>&1 | tail -3 ;;
  crashes)
    mkdir -p "$CRASH_DIR"; dev=$(device_id)
    [ -n "$dev" ] || { echo "연결된 기기 없음"; exit 1; }
    cd /tmp
    xcrun devicectl device info files --device "$dev" --domain-type systemCrashLogs 2>/dev/null \
      | awk '$1 ~ /^Markerless-.*\.ips$/ {print $1}' | sort | while read -r f; do
        [ -f "$CRASH_DIR/$f" ] && continue
        xcrun devicectl device copy from --device "$dev" --domain-type systemCrashLogs --source "$f" --destination "$CRASH_DIR/$f" >/dev/null 2>&1
        echo "== 새 크래시: $f"
        python3 -I - "$CRASH_DIR/$f" <<'PY'
import json, sys
head, body = open(sys.argv[1]).read().split("\n", 1)
b = json.loads(body)
print(" 시각:", b.get("captureTime"), "| 예외:", b.get("exception", {}).get("type"), b.get("exception", {}).get("signal"))
t = b.get("termination") or {}
if t.get("details"): print(" 종료사유:", " ".join(t["details"])[:300])
th = next((x for x in b.get("threads", []) if x.get("triggered")), None)
if th:
    img = b.get("usedImages", [])
    for fr in th["frames"][:14]:
        n = img[fr["imageIndex"]].get("name") if fr.get("imageIndex") is not None else "?"
        print("  ", n, fr.get("symbol", ""), fr.get("sourceFile", ""), fr.get("sourceLine", ""))
PY
      done
    echo "(크래시 요약 끝 — 새 항목 없으면 위 출력 없음)" ;;
  tunnel)
    sub="${2:-start}"
    tunnel_url() { grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNLOG" 2>/dev/null | tail -1; }
    case "$sub" in
      stop) pkill -f "cloudflared tunnel --url" && echo "터널 종료" || echo "실행 중인 터널 없음" ;;
      url)  pgrep -f "frpc-markerless.toml" >/dev/null && { echo "https://markerless.wizbase.ai.kr (frp 고정 도메인)"; exit 0; }
            u=$(tunnel_url); [ -n "$u" ] && pgrep -f "cloudflared tunnel --url" >/dev/null && echo "$u" || echo "터널 꺼짐 (tools/devloop.sh tunnel start)" ;;
      start)
        if ! pgrep -f "cloudflared tunnel --url" >/dev/null; then
          curl -s -m 2 localhost:8111/healthz >/dev/null || { echo "서버가 꺼져 있습니다 — make api 먼저"; exit 1; }
          : > "$TUNLOG"
          # http2: 사내/공용망에서 QUIC(UDP)이 막혀도 동작
          nohup cloudflared tunnel --url http://localhost:8111 --protocol http2 --no-autoupdate >"$TUNLOG" 2>&1 &
          for i in $(seq 1 40); do [ -n "$(tunnel_url)" ] && break; sleep 1; done
        fi
        u=$(tunnel_url)
        [ -n "$u" ] || { echo "터널 URL을 얻지 못했습니다 — 로그: $TUNLOG"; tail -5 "$TUNLOG"; exit 1; }
        echo "외부 URL : $u"
        echo "API 키   : $(cat "$ROOT/.api-key" 2>/dev/null)   (앱 ① 공간 탭 → 'API 키')"
        printf "확인     : "; curl -s -m 15 -o /dev/null -w "healthz %{http_code}, " "$u/healthz"
        curl -s -m 15 -o /dev/null -w "키없이 %{http_code}, " "$u/api/v1/projects"
        curl -s -m 15 -o /dev/null -w "키있음 %{http_code}\n" -H "X-API-Key: $(cat "$ROOT/.api-key")" "$u/api/v1/projects" ;;
      *) echo "usage: tunnel [start|stop|url]" ;;
    esac ;;
  status)
    printf "서버: "; curl -s -m 2 localhost:8111/healthz || echo "꺼짐 (make api)"; echo
    printf "Mac IP(Wi-Fi): "; ipconfig getifaddr en1 2>/dev/null || ipconfig getifaddr en0;
    dev=$(device_id); echo "기기: ${dev:-연결 없음}"
    [ -n "$dev" ] && xcrun devicectl device info apps --device "$dev" --bundle-id "$BUNDLE" 2>&1 | tail -1 ;;
  *) sed -n 2,12p "$0" ;;
esac
