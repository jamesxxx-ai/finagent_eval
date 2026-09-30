"""把 results.jsonl 中的逐题判定汇总成三家产品的 Scorecard（完整产品榜 · 对话式投研/信号赛道）。

python3 -m finagent_eval.live.score_live            # 打印结果并写出 live/scorecards.json
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
import random
from statistics import fmean

from ..scoring import TRACK_WEIGHTS, capability_score, norm
from .protocol import G_CAP_SEC, G_REF_SEC, PROTOCOL_VERSION, TASKS, TRACK

HERE = Path(__file__).parent
TASK = {t["id"]: t for t in TASKS}

# 服务可用性事件：只记录产品方明确报错导致无法按协议执行的情况。
# 浏览器自动化导致的界面不刷新、输入丢字属于测试侧问题，不计入。
SERVICE_FAILURES = {
    "Vergex": ["新建会话报错 No managed Vergex conversation model is currently available；"
               "全部题目只能在一个历史研究会话中续问，E1/F1 无法按协议在新会话中进行"],
}


def task_score(rec: dict) -> tuple[float, bool]:
    t = TASK[rec["task"]]
    checks = rec["checks"]
    s = sum(w for k, w in t["checks"].items() if checks.get(k))
    if t["id"] in ("B1_sizing", "F1_sizing_repeat") and not checks.get("exact_7_69") and rec.get("forgot_fees"):
        s = 0.3
    passed = all(checks.get(k) for k in t["must"])
    return s, passed


def load(path: Path = HERE / "results.jsonl") -> dict[str, dict[str, dict]]:
    by_agent: dict[str, dict[str, dict]] = defaultdict(dict)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            by_agent[r["agent"]][r["task"]] = r        # 同题多条时以最后一条（更正记录）为准
    return by_agent


def scorecard(agent: str, recs: dict[str, dict]) -> dict:
    per_task, dim_scores = {}, defaultdict(list)
    for tid, rec in recs.items():
        s, p = task_score(rec)
        per_task[tid] = {"score": round(s, 3), "passed": p, "latency_s": rec.get("latency_s"),
                         "evidence": rec.get("evidence", "")}
        if tid != "F1_sizing_repeat":
            dim_scores[TASK[tid]["dim"]].append(s)

    dims: dict[str, float | None] = {d: None for d in "ABCDEFG"}
    for d in "ABCE":
        dims[d] = round(100 * fmean(dim_scores[d]), 1) if dim_scores[d] else 0.0

    # F：0.6 × 同题重复一致性（B1 + F1 的所有运行中答对的比例）+ 0.4 × 服务可用性
    b1_ok = int(task_score(recs["B1_sizing"])[1]) if "B1_sizing" in recs else 0
    f1 = recs.get("F1_sizing_repeat", {})
    runs = 1 + f1.get("runs", 1 if f1 else 0)
    ok = b1_ok + f1.get("passed_runs", int(task_score(f1)[1]) if f1 else 0)
    consistency = ok / runs
    availability = 0.5 if SERVICE_FAILURES.get(agent) else 1.0
    dims["F"] = round(100 * (0.6 * consistency + 0.4 * availability), 1)

    lat = [r["latency_s"] for r in recs.values() if r.get("latency_s")]
    avg_lat = fmean(lat) if lat else G_CAP_SEC
    dims["G"] = round(norm(avg_lat, G_CAP_SEC, G_REF_SEC), 1)

    cap = round(capability_score(TRACK, dims), 1)
    ci = bootstrap_ci(per_task, dims)
    n_pass = sum(v["passed"] for v in per_task.values())
    return {
        "agent": agent, "track": TRACK, "protocol": PROTOCOL_VERSION,
        "capability": cap, "capability_ci95": ci, "dims": dims, "tasks_passed": n_pass, "tasks_total": len(per_task),
        "consistency": {"runs": runs, "correct": ok}, "avg_latency_s": round(avg_lat, 1),
        "service_failures": SERVICE_FAILURES.get(agent, []),
        "critical_incidents": 0,       # 本轮未观察到越权、执行注入、编造行情等严重事件
        "safety_gate": "本套测试未观察到严重违规",
        "rating": rating(cap),
        "trading": "未测（完整产品榜对话赛道，不下单；交易成绩需 L3 前瞻赛季）",
        "evidence_status": "平台独立测试 · 小样本试测（11 题，k≈1–3）",
        "tasks": per_task,
    }


def bootstrap_ci(per_task: dict, dims: dict, n: int = 4000, seed: int = 7) -> list[float]:
    """按题重抽样（维度内分层）重算 A/B/C/E，F、G 固定，给出能力分 95% 区间。"""
    rng = random.Random(seed)
    by_dim = defaultdict(list)
    for tid, v in per_task.items():
        if tid != "F1_sizing_repeat":
            by_dim[TASK[tid]["dim"]].append(v["score"])
    out = []
    for _ in range(n):
        d = dict(dims)
        for k, xs in by_dim.items():
            d[k] = 100 * fmean(rng.choices(xs, k=len(xs)))
        out.append(capability_score(TRACK, d))
    out.sort()
    return [round(out[int(0.025 * n)], 1), round(out[int(0.975 * n) - 1], 1)]


def rating(cap: float) -> str:
    # 小样本、无交易成绩：按 SPEC §7 最高只能评 B（A/S 需要 L3 交易成绩与更多样本）
    return "B" if cap >= 60 else "C"


def main() -> None:
    data = load()
    cards = sorted((scorecard(a, r) for a, r in data.items()), key=lambda c: -c["capability"])
    w = TRACK_WEIGHTS[TRACK]
    print(f"完整产品榜 · 对话式投研/信号赛道（{PROTOCOL_VERSION}）  权重 " +
          " ".join(f"{d}{w[d]}" for d in "ABCDEFG" if w[d]))
    for i, c in enumerate(cards, 1):
        dims = " ".join(f"{d}{v:.0f}" for d, v in c["dims"].items() if v is not None and w[d])
        lo, hi = c["capability_ci95"]
        print(f"{i}. {c['agent']:7s} 能力分 {c['capability']:5.1f} (95% CI {lo}–{hi})  通过 {c['tasks_passed']}/{c['tasks_total']}"
              f"  评级 {c['rating']}  | {dims}  | 平均响应 {c['avg_latency_s']}s")
        for f in c["service_failures"]:
            print(f"     服务事件：{f}")
    (HERE / "scorecards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2))
    print(f"\n→ {HERE / 'scorecards.json'}")


if __name__ == "__main__":
    main()
