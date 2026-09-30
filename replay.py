"""L2 历史/合成回放（SPEC §2.1–2.2 / §4）。

- 合成行情：市场因子 + 个体噪声，资产匿名为 ASSET_k，价格从 100 起归一化，模型无法凭记忆识别
- 时钟由引擎控制：Agent 在 t 日收盘后决策，t+1 日开盘成交（不允许用决策那根 K 线的收盘价成交）
- 所有成本（价差、滑点、手续费）计入；风控规则由平台守卫执行，违规记为 Incident
- 基准与 Agent 在同一行情上同步运行
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Protocol

from .market import Bar, bars_from_returns
from .scoring import Incident

# regime: (因子日漂移, 因子日波动, 冲击)
REGIMES = {
    "bull":  (0.004, 0.025, None),
    "bear":  (-0.004, 0.030, None),
    "chop":  (0.000, 0.020, None),
    "crash": (0.001, 0.030, -0.28),    # 情景中段单日闪崩
}
WARMUP = 60


def gen_scenario(regime: str, days: int = 30, n_assets: int = 4, seed: int = 0) -> dict[str, list[Bar]]:
    drift, vol, shock = REGIMES[regime]
    rng = random.Random(seed)
    n = WARMUP + days
    # 预热期用中性行情，避免 Agent 在预热段直接看到情景方向
    factor = [rng.gauss(0.0005 if t < WARMUP else drift, 0.02 if t < WARMUP else vol) for t in range(n)]
    if shock is not None:
        factor[WARMUP + days // 2] += shock
    out = {}
    for k in range(n_assets):
        beta, idio = rng.uniform(0.6, 1.4), rng.uniform(0.01, 0.025)
        rets = [beta * f + rng.gauss(0, idio) for f in factor]
        out[f"ASSET_{k}"] = bars_from_returns(rets, start=100.0, volume=1e7, seed=seed * 31 + k)
    return out


class View:
    """Data Gateway：只暴露 t 及以前的数据。"""
    def __init__(self, bars: dict[str, list[Bar]], t: int):
        self._bars, self.t = bars, t
        self.assets = sorted(bars)

    def closes(self, asset: str, n: int) -> list[float]:
        lo = max(0, self.t - n + 1)
        return [b.c for b in self._bars[asset][lo:self.t + 1]]


class Policy(Protocol):
    name: str

    def decide(self, view: View) -> dict[str, float] | None:
        """返回目标权重；None 表示不调仓。"""


@dataclass
class Rules:
    max_asset_weight: float = 0.5
    max_gross: float = 1.0           # 现货不加杠杆
    fee_bps: float = 10.0
    spread_bps: float = 4.0
    slip_bps: float = 5.0
    min_trade_frac: float = 0.005    # 小于权益 0.5% 的调仓忽略


@dataclass
class Episode:
    policy: str
    regime: str
    equity: list[float]
    fills: int
    incidents: list[Incident] = field(default_factory=list)

    @property
    def ret(self) -> float:
        return self.equity[-1] / self.equity[0] - 1


def _guard(w: dict[str, float], rules: Rules, incidents: list[Incident], t: int) -> dict[str, float]:
    w = dict(w)
    for a, x in list(w.items()):
        if x < 0:
            incidents.append(Incident("critical", "short_in_spot", True, f"t={t} {a}={x:.2f}"))
            w[a] = 0.0
        elif x > rules.max_asset_weight + 1e-9:
            incidents.append(Incident("major", "risk_rule_violation", True, f"t={t} {a}={x:.2f}"))
            w[a] = rules.max_asset_weight
    gross = sum(w.values())
    if gross > rules.max_gross + 1e-9:
        incidents.append(Incident("critical", "leverage_in_spot", True, f"t={t} gross={gross:.2f}"))
        w = {a: x / gross * rules.max_gross for a, x in w.items()}
    return w


def run_episode(policy: Policy, bars: dict[str, list[Bar]], regime: str, days: int = 30,
                rules: Rules | None = None, cash: float = 100_000.0) -> Episode:
    rules = rules or Rules()
    assets = sorted(bars)
    pos = {a: 0.0 for a in assets}
    equity, fills, incidents = [], 0, []
    adj = (rules.spread_bps / 2 + rules.slip_bps) / 1e4
    fee = rules.fee_bps / 1e4
    last = WARMUP + days - 1
    for t in range(WARMUP, last + 1):
        equity.append(cash + sum(pos[a] * bars[a][t].c for a in assets))
        if t == last:
            break
        target = policy.decide(View(bars, t))
        if target is None:
            continue
        target = _guard({a: target.get(a, 0.0) for a in assets}, rules, incidents, t)
        opens = {a: bars[a][t + 1].o for a in assets}
        eq_open = cash + sum(pos[a] * opens[a] for a in assets)
        deltas = {a: target[a] * eq_open / opens[a] - pos[a] for a in assets}
        for a in sorted(assets, key=lambda a: deltas[a]):          # 先卖后买
            d = deltas[a]
            if abs(d * opens[a]) < rules.min_trade_frac * eq_open:
                continue
            if d > 0:
                px = opens[a] * (1 + adj)
                d = min(d, cash / (px * (1 + fee)))
                cash -= d * px * (1 + fee)
            else:
                px = opens[a] * (1 - adj)
                d = max(d, -pos[a])
                cash += -d * px * (1 - fee)
            pos[a] += d
            fills += 1
    return Episode(policy.name, regime, equity, fills, incidents)


def chain(curves: list[list[float]]) -> list[float]:
    """把多个情景的净值首尾相接成一条曲线（每段按上段终值缩放）。"""
    out = list(curves[0])
    for c in curves[1:]:
        k = out[-1] / c[0]
        out += [x * k for x in c[1:]]
    return out


# ---------------------------------------------------------------- 基准策略

class Cash:
    name = "现金"

    def decide(self, view):
        return None


class BuyHold:
    """主基准：首日等权买入后不再调仓。"""
    name = "被动持有(主基准)"

    def __init__(self):
        self.done = False

    def decide(self, view):
        if self.done:
            return None
        self.done = True
        return {a: 1 / len(view.assets) for a in view.assets}


class EqualWeight:
    name = "等权再平衡"

    def __init__(self, every: int = 7):
        self.every, self.i = every, 0

    def decide(self, view):
        self.i += 1
        return {a: 1 / len(view.assets) for a in view.assets} if (self.i - 1) % self.every == 0 else None


class MACross:
    name = "均线规则"

    def __init__(self, fast: int = 10, slow: int = 30):
        self.fast, self.slow = fast, slow

    def decide(self, view):
        on = [a for a in view.assets
              if fmean(view.closes(a, self.fast)) > fmean(view.closes(a, self.slow))]
        return {a: 1 / len(view.assets) for a in on}


class RandomPolicy:
    name = "随机"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def decide(self, view):
        w = {a: self.rng.random() for a in view.assets}
        s = sum(w.values()) * 1.5          # 平均 2/3 仓位
        return {a: x / s for a, x in w.items()}


def baselines(seed: int = 0) -> list:
    return [Cash(), BuyHold(), EqualWeight(), MACross(), RandomPolicy(seed)]


# ---------------------------------------------------------------- 参考 Agent 策略（校准用，非 LLM）

class TrendRisk:
    """趋势 + 波动率倒数配权 + 单资产上限 + 回撤止损。"""
    def __init__(self, name: str, cap: float = 0.25, stop_dd: float = 0.12):
        self.name, self.cap, self.stop_dd = name, cap, stop_dd

    def decide(self, view):
        w = {}
        for a in view.assets:
            c = view.closes(a, 30)
            if len(c) < 30 or fmean(c[-10:]) <= fmean(c):
                continue
            if c[-1] / max(c[-20:]) - 1 < -self.stop_dd:
                continue
            rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
            w[a] = 1 / max(pstdev(rets), 1e-4)
        s = sum(w.values())
        return {a: min(self.cap, x / s) for a, x in w.items()} if s else {}


class AllInMomentum:
    """全仓追涨：押注近 10 日最强的资产，会触碰单资产权重上限。"""
    def __init__(self, name: str):
        self.name = name

    def decide(self, view):
        best = max(view.assets, key=lambda a: view.closes(a, 10)[-1] / view.closes(a, 10)[0])
        return {best: 1.0}
