"""用合成数据跑通评分引擎：两个虚构 Agent + 基准。数字不对应任何真实产品。"""
import random

from scoring import (Attempt, Incident, build_scorecard, norm, pass_hat_k,
                     trading_score)


def fake_attempts(skill: float, flaky: float, seed: int) -> list[Attempt]:
    rng = random.Random(seed)
    out = []
    for fam in range(24):
        dim = "ABCDEF"[fam % 6]
        for t in range(3):
            tid = f"T{fam}-{t}"
            for _ in range(5):                       # k=5 次独立运行
                ok = rng.random() < skill - (flaky if rng.random() < 0.3 else 0)
                out.append(Attempt(tid, f"F{fam}", dim, 1.0 if ok else rng.uniform(0, 0.5), ok,
                                   cost_usd=rng.uniform(0.05, 0.4),
                                   decision=("buy" if ok else "sell") if dim == "C" else None,
                                   is_fault_task=dim == "F"))
    return out


def fake_equity(days: int, drift: float, vol: float, seed: int,
                bench: list[float] | None = None, beta: float = 0.0) -> list[float]:
    """可选地与基准相关：r = beta·r_bench + 特质收益。"""
    rng = random.Random(seed)
    eq = [100_000.0]
    for i in range(days):
        rb = bench[i + 1] / bench[i] - 1 if bench else 0.0
        eq.append(eq[-1] * (1 + beta * rb + rng.gauss(drift, vol)))
    return eq


if __name__ == "__main__":
    # 自检
    assert norm(0.0, -0.1, 0.1) == 50 and norm(0.1, 0.2, 0.0) == 50
    assert pass_hat_k([Attempt("x", "f", "A", 1, True)] * 5) == 1.0
    assert pass_hat_k([Attempt("x", "f", "A", 1, i < 4) for i in range(5)]) == 0.0

    bench = fake_equity(120, 0.0004, 0.03, seed=1)          # 被动持有主基准
    cases = [
        ("Agent-Alpha v2.1", 0.88, 0.10, [Incident("minor", "轻度超仓后自纠")],
         fake_equity(120, 0.0008, 0.008, seed=2, bench=bench, beta=0.5), 140),
        ("Agent-Beta v0.9", 0.80, 0.25, [Incident("critical", "执行新闻注入指令提高杠杆", True)],
         fake_equity(120, 0.0010, 0.030, seed=3, bench=bench, beta=1.5), 400),
    ]
    for i, (name, skill, flaky, incs, eq, fills) in enumerate(cases):
        tr = trading_score(eq, bench, n_fills=fills, n_trials=3, sr_trials_var=0.0004)
        card = build_scorecard(name, "autonomous", fake_attempts(skill, flaky, seed=10 + i),
                               incs, cost_ref=0.05, cost_cap=0.50, trading=tr)
        lo, hi = card.capability_ci
        print(f"\n== {card.agent_version} [{card.track}]  评级 {card.rating}")
        print("  维度 " + "  ".join(f"{d}:{v:.0f}" for d, v in card.dims.items() if v is not None))
        print(f"  能力分 {card.capability:.1f}  (95% CI {lo:.1f}–{hi:.1f})"
              f"  核心安全 {'通过' if card.gate_ok else '未通过'}  事件 {len(card.incidents)}")
        r = tr.raw
        print(f"  交易成绩 {tr.score:.1f}  [{tr.label}]  超额 {r['excess']:+.1%}"
              f" (CI {r['excess_ci_lo']:+.1%}~{r['excess_ci_hi']:+.1%})  最大回撤 {r['max_dd']:.1%}"
              f"  CVaR10 {r['cvar10']:.2%}  分段胜率 {r['segment']:.0%}  DSR {r['dsr']:.2f}"
              f"  主动优势 {'是' if tr.active_edge else '否'}")
