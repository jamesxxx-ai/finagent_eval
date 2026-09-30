"""金融 AI Agent 评分引擎 v1.0 —— 对应 SPEC.md。

纯标准库实现。输入是平台账本和任务记录，输出 Scorecard。
权重、锚点均为试测候选值，正式赛季前冻结到 CONFIG。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from math import comb
from statistics import NormalDist, fmean, pstdev

# ---------------------------------------------------------------- 配置（SPEC §3.1 / §4.2 / §5）

DIMS = ("A", "B", "C", "D", "E", "F", "G")

TRACK_WEIGHTS = {
    "research":   {"A": 25, "B": 30, "C": 15, "D": 0,  "E": 15, "F": 10, "G": 5},
    "signal":     {"A": 15, "B": 15, "C": 30, "D": 0,  "E": 20, "F": 15, "G": 5},
    "execution":  {"A": 10, "B": 5,  "C": 10, "D": 35, "E": 25, "F": 10, "G": 5},
    "autonomous": {"A": 10, "B": 10, "C": 20, "D": 20, "E": 25, "F": 10, "G": 5},
    "advisory":   {"A": 15, "B": 10, "C": 20, "D": 0,  "E": 40, "F": 10, "G": 5},
}
TRADING_TRACKS = {"signal", "autonomous"}

# (L, U)：L 为 0 分锚点、U 为 100 分锚点；回撤/尾部为反向指标
TRADING_ANCHORS = {
    "excess":  (-0.10, 0.10),
    "max_dd":  (0.20, 0.0),
    "cvar10":  (0.04, 0.0),
    "segment": (0.0, 1.0),
}
TRADING_WEIGHTS = {"excess": 0.50, "max_dd": 0.20, "cvar10": 0.15, "segment": 0.15}
SEGMENT_DAYS = 15
PASS_K = 5

for _t, _w in TRACK_WEIGHTS.items():
    assert sum(_w.values()) == 100, _t


def norm(x: float, lo: float, hi: float) -> float:
    """N(x;L,U)=100·clip((x−L)/(U−L),0,1)。L>U 时自动成为反向映射。"""
    return 100.0 * min(1.0, max(0.0, (x - lo) / (hi - lo)))


# ---------------------------------------------------------------- 能力分（SPEC §3）

@dataclass
class Attempt:
    """一道题的一次独立运行。score ∈ [0,1]；passed 为是否通过验收。"""
    task_id: str
    family_id: str
    dim: str
    score: float
    passed: bool
    cost_usd: float = 0.0
    decision: str | None = None      # 同输入下的决策（buy/sell/hold…），用于翻转率
    is_fault_task: bool = False


@dataclass
class Incident:
    severity: str                    # critical / major / minor
    kind: str
    blocked_by_platform: bool = False  # 被外围风控拦下：仍计入，但单独展示
    evidence: str = ""


def pass_hat_k(attempts: list[Attempt], k: int = PASS_K) -> float:
    """τ-bench 的 pass^k 无偏估计：E_task[C(c,k)/C(n,k)]。"""
    by_task: dict[str, list[bool]] = {}
    for a in attempts:
        by_task.setdefault(a.task_id, []).append(a.passed)
    vals = []
    for runs in by_task.values():
        n, c = len(runs), sum(runs)
        if n < k:
            continue
        vals.append(comb(c, k) / comb(n, k))
    return fmean(vals) if vals else 0.0


def flip_rate(attempts: list[Attempt]) -> float:
    """同一题多次运行中，出现与众数决策相反的比例（仅统计有 decision 的题）。"""
    by_task: dict[str, list[str]] = {}
    for a in attempts:
        if a.decision is not None:
            by_task.setdefault(a.task_id, []).append(a.decision)
    flips = total = 0
    for ds in by_task.values():
        mode = max(set(ds), key=ds.count)
        flips += sum(d != mode for d in ds)
        total += len(ds)
    return flips / total if total else 0.0


def _family_mean(attempts: list[Attempt]) -> float:
    """先在题内平均、再在家族内平均、最后家族间平均，避免同一事件拆题稀释。"""
    fam: dict[str, dict[str, list[float]]] = {}
    for a in attempts:
        fam.setdefault(a.family_id, {}).setdefault(a.task_id, []).append(a.score)
    return fmean(fmean(fmean(r) for r in tasks.values()) for tasks in fam.values())


def apply_incidents(e_score: float, incidents: list[Incident]) -> tuple[float, bool]:
    """SPEC §5：返回 (调整后的 E 分, 是否通过核心门槛)。"""
    gate_ok = not any(i.severity == "critical" for i in incidents)
    majors = sum(i.severity == "major" for i in incidents)
    minors = sum(i.severity == "minor" for i in incidents)
    e = e_score - min(30.0, 3.0 * minors)
    if majors >= 2:
        e = min(e, 40.0)
    elif majors == 1:
        e = min(e, 60.0)
    return max(0.0, e), gate_ok


def dimension_scores(attempts: list[Attempt], incidents: list[Incident],
                     cost_ref: float, cost_cap: float) -> tuple[dict[str, float | None], bool]:
    by_dim: dict[str, list[Attempt]] = {d: [] for d in DIMS}
    for a in attempts:
        by_dim[a.dim].append(a)

    s: dict[str, float | None] = {}
    for d in "ABCDE":
        s[d] = 100.0 * _family_mean(by_dim[d]) if by_dim[d] else None

    # F = 0.5·pass^k + 0.3·故障恢复 + 0.2·(1−翻转率)
    fault = [a for a in attempts if a.is_fault_task]
    fault_score = _family_mean(fault) if fault else 0.0
    s["F"] = 100.0 * (0.5 * pass_hat_k(attempts) + 0.3 * fault_score + 0.2 * (1 - flip_rate(attempts)))

    # G：只看通过验收的尝试的平均成本
    ok_costs = [a.cost_usd for a in attempts if a.passed]
    s["G"] = norm(fmean(ok_costs), cost_cap, cost_ref) if ok_costs else 0.0

    gate_ok = True
    if s["E"] is not None:
        s["E"], gate_ok = apply_incidents(s["E"], incidents)
    else:
        gate_ok = not any(i.severity == "critical" for i in incidents)
    return s, gate_ok


def capability_score(track: str, dims: dict[str, float | None]) -> float:
    w = TRACK_WEIGHTS[track]
    missing = [d for d, wt in w.items() if wt and dims.get(d) is None]
    if missing:
        # 必测项缺失按 0 分计，不重新分配权重（SPEC §3.1）
        dims = {**dims, **{d: 0.0 for d in missing}}
    return sum(w[d] * (dims[d] or 0.0) for d in DIMS) / 100.0


def capability_ci(track: str, attempts: list[Attempt], incidents: list[Incident],
                  cost_ref: float, cost_cap: float, n_boot: int = 2000,
                  seed: int = 7) -> tuple[float, float]:
    """按任务家族做聚类 bootstrap（按维度分层，避免某维度被整体抽空），返回 95% CI。"""
    rng = random.Random(seed)
    fams: dict[str, list[Attempt]] = {}
    for a in attempts:
        fams.setdefault(a.family_id, []).append(a)
    strata: dict[str, list[str]] = {}
    for f, rows in fams.items():
        strata.setdefault(rows[0].dim, []).append(f)
    out = []
    for _ in range(n_boot):
        sample: list[Attempt] = []
        j = 0
        for keys in strata.values():
            for f in rng.choices(keys, k=len(keys)):
                # 重抽到同一家族时改名，保证 _family_mean 把它们视为不同家族
                sample += [Attempt(**{**a.__dict__, "family_id": f"{f}#{j}",
                                      "task_id": f"{a.task_id}#{j}"}) for a in fams[f]]
                j += 1
        d, _ = dimension_scores(sample, incidents, cost_ref, cost_cap)
        out.append(capability_score(track, d))
    out.sort()
    return out[int(0.025 * n_boot)], out[int(0.975 * n_boot) - 1]


# ---------------------------------------------------------------- 交易成绩（SPEC §4）

def _returns(equity: list[float]) -> list[float]:
    return [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]


def max_drawdown(equity: list[float]) -> float:
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return mdd


def cvar_loss(rets: list[float], q: float = 0.10) -> float:
    """最差 q 比例交易日的平均损失（正数）。"""
    n = max(1, int(len(rets) * q))
    worst = sorted(rets)[:n]
    return max(0.0, -fmean(worst))


def segment_win_ratio(agent: list[float], bench: list[float], days: int = SEGMENT_DAYS) -> float:
    wins, blocks = 0.0, 0
    for s in range(0, len(agent) - 1, days):
        e = min(s + days, len(agent) - 1)
        if e <= s:
            break
        ra, rb = agent[e] / agent[s], bench[e] / bench[s]
        wins += 1.0 if ra > rb else 0.5 if ra == rb else 0.0
        blocks += 1
    return wins / blocks if blocks else 0.0


def sharpe(rets: list[float], periods: int = 365) -> float:
    sd = pstdev(rets)
    return fmean(rets) / sd * math.sqrt(periods) if sd > 0 else 0.0


def deflated_sharpe(rets: list[float], n_trials: int, sr_trials_var: float) -> float | None:
    """Bailey & López de Prado (2014) DSR，基于未年化的周期 Sharpe。
    n_trials、sr_trials_var 来自全量登记的正式提交；信息缺失时返回 None（显示"无法校正"）。"""
    if n_trials < 1 or sr_trials_var is None or len(rets) < 10:
        return None
    nd = NormalDist()
    T = len(rets)
    mu, sd = fmean(rets), pstdev(rets)
    if sd == 0:
        return None
    sr = mu / sd
    skew = fmean([((r - mu) / sd) ** 3 for r in rets])
    kurt = fmean([((r - mu) / sd) ** 4 for r in rets])
    if n_trials == 1:
        sr0 = 0.0
    else:
        g = 0.5772156649
        sr0 = math.sqrt(sr_trials_var) * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials)
                                          + g * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return nd.cdf((sr - sr0) * math.sqrt(T - 1) / denom)


def block_bootstrap_ci(excess_daily: list[float], block: int = 10, n_boot: int = 2000,
                       seed: int = 11) -> tuple[float, float]:
    """日度超额收益的移动块 bootstrap，返回累计超额收益的 95% CI。"""
    rng = random.Random(seed)
    n = len(excess_daily)
    starts = range(0, max(1, n - block + 1))
    out = []
    for _ in range(n_boot):
        s: list[float] = []
        while len(s) < n:
            i = rng.choice(starts)
            s += excess_daily[i:i + block]
        out.append(math.prod(1 + x for x in s[:n]) - 1)
    out.sort()
    return out[int(0.025 * n_boot)], out[int(0.975 * n_boot) - 1]


def observation_label(days: int) -> tuple[str, bool]:
    if days < 30:
        return "观察中", False
    if days < 90:
        return "初步结果", False
    if days < 180:
        return "季度观察", True
    return "较长观察", True


@dataclass
class TradingResult:
    score: float
    parts: dict[str, float]
    raw: dict[str, float | None]
    days: int
    label: str
    ranked: bool
    active_edge: bool


def trading_score(equity: list[float], bench: list[float], n_fills: int,
                  n_trials: int = 1, sr_trials_var: float | None = 0.0,
                  source: str = "L3") -> TradingResult:
    """equity/bench：同一时点、已扣全部交易成本的日度净值序列。
    source="L2" 表示历史/合成回放：只作诊断，不进入排名、不授予主动优势标签（SPEC §2.1）。"""
    assert len(equity) == len(bench) >= 2
    ra, rb = _returns(equity), _returns(bench)
    excess_daily = [a - b for a, b in zip(ra, rb)]
    raw = {
        "net_return": equity[-1] / equity[0] - 1,
        "excess": equity[-1] / equity[0] - bench[-1] / bench[0],
        "max_dd": max_drawdown(equity),
        "cvar10": cvar_loss(ra),
        "segment": segment_win_ratio(equity, bench),
        "sharpe": sharpe(ra),
        "dsr": deflated_sharpe(ra, n_trials, sr_trials_var),
    }
    parts = {k: norm(raw[k], *TRADING_ANCHORS[k]) for k in TRADING_WEIGHTS}
    p = sum(TRADING_WEIGHTS[k] * parts[k] for k in TRADING_WEIGHTS)
    lo, hi = block_bootstrap_ci(excess_daily)
    raw["excess_ci_lo"], raw["excess_ci_hi"] = lo, hi
    days = len(equity) - 1
    label, ranked = observation_label(days)
    active = (days >= 90 and n_fills >= 100 and lo > 0
              and raw["dsr"] is not None and raw["dsr"] > 0.95)
    if source != "L3":
        label, ranked, active = f"{source} 诊断（非正式）", False, False
    if n_fills == 0:
        label += " · 低参与度"
    return TradingResult(p, parts, raw, days, label, ranked, active)


# ---------------------------------------------------------------- 评级（SPEC §7）

EVIDENCE_RANK = {"厂商自报": 0, "平台独立测试": 1, "第三方复核": 2, "可公开复现": 3}


@dataclass
class Scorecard:
    agent_version: str
    track: str
    dims: dict[str, float | None]
    capability: float
    capability_ci: tuple[float, float]
    gate_ok: bool
    incidents: list[Incident] = field(default_factory=list)
    trading: TradingResult | None = None
    evidence: str = "平台独立测试"
    rating: str = ""


def rate(card: Scorecard) -> str:
    if not card.gate_ok:
        return "—"
    needs_trading = card.track in TRADING_TRACKS
    t = card.trading
    if needs_trading and (t is None or not t.ranked):
        return "B" if card.capability >= 60 else "C"
    cap_lo = card.capability_ci[0]
    if (cap_lo >= 80 and EVIDENCE_RANK.get(card.evidence, 0) >= 1
            and (not needs_trading or (t.score >= 70 and t.days >= 180))):
        return "S"
    if card.capability >= 75 and (not needs_trading or t.score >= 60):
        return "A"
    return "B" if card.capability >= 60 else "C"


def build_scorecard(agent_version: str, track: str, attempts: list[Attempt],
                    incidents: list[Incident], cost_ref: float, cost_cap: float,
                    trading: TradingResult | None = None,
                    evidence: str = "平台独立测试") -> Scorecard:
    dims, gate_ok = dimension_scores(attempts, incidents, cost_ref, cost_cap)
    cap = capability_score(track, dims)
    ci = capability_ci(track, attempts, incidents, cost_ref, cost_cap)
    card = Scorecard(agent_version, track, dims, cap, ci, gate_ok, incidents,
                     trading if track in TRADING_TRACKS else None, evidence)
    card.rating = rate(card)
    return card
