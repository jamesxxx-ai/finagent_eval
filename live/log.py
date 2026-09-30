"""追加一条实测结果到 results.jsonl。用法：python3 -m finagent_eval.live.log '<json>'"""
import json, sys, time
from pathlib import Path
P = Path(__file__).with_name("results.jsonl")
rec = json.loads(sys.argv[1])
rec.setdefault("logged_at_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
with P.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("ok", rec["agent"], rec["task"])
