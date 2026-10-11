"""dev.log(JSONL) → 사람이 읽기 좋은 한 줄 요약. 사용: tools/devloop.sh logs"""
import json
import sys

HIDE = {"ts", "src", "lvl", "tag", "msg", "device", "session", "cts"}

for line in sys.stdin:
    try:
        r = json.loads(line)
    except ValueError:
        print(line.rstrip())
        continue
    extra = {k: v for k, v in r.items() if k not in HIDE}
    tail = f"  {json.dumps(extra, ensure_ascii=False)}" if extra else ""
    print(f"{r.get('ts')} {r.get('src', ''):6} {r.get('lvl', ''):5} {r.get('tag')}: {r.get('msg')}{tail}")
