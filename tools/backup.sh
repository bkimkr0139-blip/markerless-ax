#!/usr/bin/env bash
# DB(일관된 스냅샷) + 저장소를 backups/ 로 백업. 사용: tools/backup.sh [보관개수=14]
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${1:-14}"; TS=$(date +%y%m%d-%H%M%S)
mkdir -p backups
sqlite3 backend/markerless.db ".backup 'backups/markerless-$TS.db'"
tar czf "backups/storage-$TS.tgz" -C backend storage
ls -1t backups/markerless-*.db 2>/dev/null | tail -n +$((KEEP+1)) | xargs -I{} rm -f {}
ls -1t backups/storage-*.tgz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -I{} rm -f {}
echo "백업 완료: backups/markerless-$TS.db, backups/storage-$TS.tgz"
