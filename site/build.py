"""生成说明网站：python3 -m finagent_eval.site.build → finagent_eval/site/index.html

数据全部来自仓库：live/scorecards.json（实测）、live/protocol.py（题目）、参照 Agent 的 L1/L2 运行结果。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..agents import REFERENCE_AGENTS
from ..events import EventStore
from ..live.protocol import PROTOCOL_VERSION, TASKS
from ..runner import evaluate, replay_table
from ..scoring import TRACK_WEIGHTS, pass_hat_k
from ..tasks import SUITE, SUITE_VERSION

HERE = Path(__file__).parent


def reference_results() -> list[dict]:
    store = EventStore()
    replay = replay_table(REFERENCE_AGENTS, 0)
    out = []
    for a in REFERENCE_AGENTS:
        card, cap = evaluate(a, store, replay, k=5, seed=0)
        per_task = {}
        for t in SUITE:
            recs = [r for r in cap.records if r.task == t.id]
            per_task[t.id] = sum(r.passed for r in recs) / len(recs)
        out.append({
            "agent": a.key, "mistake_rate": a.mistake_rate, "capability": round(card.capability, 1),
            "ci": [round(x, 1) for x in card.capability_ci], "gate_ok": card.gate_ok, "rating": card.rating,
            "pass5": round(pass_hat_k(cap.attempts, 5), 2),
            "dims": {k: (round(v) if v is not None else None) for k, v in card.dims.items()},
            "critical": sum(i.severity == "critical" for i in card.incidents),
            "major": sum(i.severity == "major" for i in card.incidents),
            "l2_score": round(card.trading.score, 1), "per_task": per_task,
        })
    return out


def main() -> None:
    live = json.loads((HERE.parent / "live" / "scorecards.json").read_text(encoding="utf-8"))
    data = {
        "live": live,
        "protocol": PROTOCOL_VERSION,
        "tasks": [{k: t[k] for k in ("id", "dim", "prompt", "key") if k in t} for t in TASKS],
        "weights": TRACK_WEIGHTS,
        "reference": reference_results(),
        "suite": SUITE_VERSION,
        "suite_tasks": [{"id": t.id, "dim": t.dim, "fault": t.fault} for t in SUITE],
    }
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (HERE / "index.html").write_text(tpl.replace("/*__DATA__*/null", blob), encoding="utf-8")
    print("→", HERE / "index.html")


if __name__ == "__main__":
    main()
