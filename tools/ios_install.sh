#!/bin/zsh
# 실기기용 앱 빌드 → 설치 → 설치된 빌드 번호 확인 (Xcode GUI 없이)
#   tools/ios_install.sh                # 기본: 연결/페어링된 첫 실기기
#   tools/ios_install.sh "iPhone 13 Pro" # 모델명(부분 일치)으로 지정 — 기기 이름이 같아도 구분된다
# 앱 왼쪽 위·공간 탭·로그(build 필드)에 같은 번호가 나온다.
set -e
ROOT="${0:A:h:h}"
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
BUNDLE=kr.poc.markerless.Markerless
WANT="${1:-}"
STAMP=$(date +%y%m%d.%H%M)

J="${TMPDIR:-/tmp}/ios-install-devices.json"
xcrun devicectl list devices --json-output "$J" >/dev/null 2>&1
UDID=$(WANT="$WANT" python3 -I -c '
import json, os, sys
want = os.environ.get("WANT", "").lower()
for d in json.load(open(sys.argv[1]))["result"]["devices"]:
    hp = d["hardwareProperties"]
    if hp.get("platform") != "iOS" or hp.get("reality") == "simulated" or hp.get("udid", "").count("-") != 1: continue
    if d["connectionProperties"].get("pairingState") != "paired": continue
    model = ((hp.get("marketingName") or "") + " " + (hp.get("productType") or "")).lower()
    if want and want not in model: continue
    print(hp["udid"] + "\t" + d["connectionProperties"].get("transportType", "?")); break
' "$J")
TRANSPORT="${UDID#*$'\t'}"; UDID="${UDID%%$'\t'*}"
case "$TRANSPORT" in wired) TLABEL="USB 유선";; localNetwork) TLABEL="무선(같은 Wi-Fi)";; *) TLABEL="$TRANSPORT";; esac
[ -n "$UDID" ] || { echo "대상 기기를 찾지 못했습니다 (USB 연결·잠금 해제·신뢰/개발자 모드 확인)"; exit 1; }
echo "기기 $UDID ($TLABEL) · 빌드 번호 $STAMP"

cd "$ROOT/mobile-ios"
xcodegen generate >/dev/null
xcodebuild -project Markerless.xcodeproj -scheme Markerless -destination "id=$UDID" -derivedDataPath /tmp/mk-dd \
  -allowProvisioningUpdates CURRENT_PROJECT_VERSION="$STAMP" build 2>&1 | grep -E "error:|\*\* BUILD" || true
APP=/tmp/mk-dd/Build/Products/Debug-iphoneos/Markerless.app
BUILT=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$APP/Info.plist")
[ "$BUILT" = "$STAMP" ] || { echo "빌드 실패 또는 번호 불일치 (빌드 산출물 $BUILT ≠ $STAMP)"; exit 1; }
xcrun devicectl device install app --device "$UDID" "$APP" 2>&1 | grep -E "bundleID|rror" || true

# 설치된 앱의 번호를 기기에서 직접 읽어 확인
INST=$(xcrun devicectl device info apps --device "$UDID" --bundle-id "$BUNDLE" 2>&1 | grep "$BUNDLE" | awk '{print $NF}')
if [ "$INST" = "$STAMP" ]; then echo "✓ 기기에 설치된 빌드 $INST — 최신입니다"
else echo "✗ 기기 설치 번호 '$INST' ≠ 방금 빌드 $STAMP — 설치가 안 됐을 수 있습니다"; exit 1; fi
xcrun devicectl device process launch --device "$UDID" "$BUNDLE" 2>&1 | grep -q Launched \
  && echo "앱 실행됨" || echo "(자동 실행 실패 — 폰 잠금을 풀고 홈 화면에서 직접 실행하세요)"
