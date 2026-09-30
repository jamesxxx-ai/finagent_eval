"""python -m finagent_eval [--k 5] [--seed 0] [--out runs]

用参照 Agent 跑一遍 L1/L2 题库与 L2 回放，输出榜单、题目区分度、回放对照，
并把完整事件（哈希链）和 Scorecard 写入 runs/<时间戳>/。
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import fmean

from .agents import REFERENCE_AGENTS
from .events import EventStore
from .runner import evaluate, replay_table
from .scoring import pass_hat_k
from .tasks import SUITE, SUITE_VERSION


def _incident_summary(incs) -> str:
    c = Counter(i.severity for i in incs)
    realized = sum(1 for i in incs if not i.blocked_by_platform)
    return f"严重{c['critical']} 重大{c['major']} 轻微{c['minor']}（造成实际违规 {realized}）"


def main() -> None:
    ap = argparse.ArgumentParser(prog="finagent_eval")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    store = EventStore(out / "events.jsonl")

    agents = REFERENCE_AGENTS
    replay = replay_table(agents, args.seed)
    cards, caps = [], {}
    for a in agents:
        card, cap = evaluate(a, store, replay, k=args.k, seed=args.seed)
        cards.append(card)
        caps[a.key] = cap
    store.close()

    print(f"题库 {SUITE_VERSION} · {len(SUITE)} 题 × k={args.k} · 赛道 autonomous · 参照 Agent（非 LLM）\n")
    print("== 能力榜 ==")
    order = sorted(cards, key=lambda c: (not c.gate_ok, -c.capability))
    for rank, c in enumerate(order, 1):
        lo, hi = c.capability_ci
        pk = pass_hat_k(caps[c.agent_version].attempts, args.k)
        dims = " ".join(f"{d}{v:.0f}" for d, v in c.dims.items() if v is not None)
        print(f"{rank}. [{c.rating}] {c.agent_version:18s} 能力 {c.capability:5.1f} ({lo:.1f}–{hi:.1f})"
              f"  pass^{args.k} {pk:.2f}  安全门槛 {'通过' if c.gate_ok else '未通过'}")
        print(f"      {dims}")
        print(f"      事件：{_incident_summary(c.incidents)}")
        t = c.trading
        print(f"      回放成绩 {t.score:.1f} [{t.label}] 超额 {t.raw['excess']:+.1%} 回撤 {t.raw['max_dd']:.1%}")
    print("  注：交易成绩仅来自 L2 回放，按 SPEC §7 评级最高为 B；需 L3 前瞻赛季 ≥90 天才能评 A/S。")

    print("\n== 题目区分度（各 Agent 的单次通过率）==")
    print(f"{'task':26s}{'dim':4s}" + "".join(f"{a.name:>13s}" for a in agents))
    for task in SUITE:
        row = []
        for a in agents:
            recs = [r for r in caps[a.key].records if r.task == task.id]
            row.append(sum(r.passed for r in recs) / len(recs))
        flag = "  ← 区分度不足" if max(row) - min(row) < 0.2 else ""
        print(f"{task.id:26s}{task.dim:4s}" + "".join(f"{x:13.0%}" for x in row) + flag)

    print("\n== L2 回放对照（各情景收益，诊断用，不作为盈利能力证明）==")
    regimes = list(dict.fromkeys(e.regime for e in next(iter(replay.values()))))
    npaths = len(next(iter(replay.values()))) // len(regimes)
    print(f"（每个情景 {npaths} 条合成路径的平均收益）")
    print(f"{'policy':22s}" + "".join(f"{r:>9s}" for r in regimes) + f"{'笔数':>7s}{'违规':>6s}")
    for name, eps in replay.items():
        avg = [fmean(e.ret for e in eps if e.regime == r) for r in regimes]
        print(f"{name:22s}" + "".join(f"{x:9.1%}" for x in avg)
              + f"{sum(e.fills for e in eps):7d}{sum(len(e.incidents) for e in eps):6d}")

    ok = EventStore.verify(EventStore.load(out / "events.jsonl"))
    payload = [{**{k: v for k, v in asdict(c).items() if k not in ("incidents",)},
                "incident_summary": _incident_summary(c.incidents)} for c in cards]
    (out / "scorecards.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"\n事件 {len(store.records)} 条，哈希链校验 {'通过' if ok else '失败'} → {out}/")


if __name__ == "__main__":
    main()
