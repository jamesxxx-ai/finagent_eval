"""Runner（SPEC §6 / §9）：每题独立运行 k 次、隔离状态、写事件、调用评分器、汇总成 Scorecard。"""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field

from .events import EventStore
from .replay import REGIMES, Episode, baselines, chain, gen_scenario, run_episode
from .scoring import Attempt, Incident, Scorecard, TradingResult, build_scorecard, trading_score
from .tasks import SUITE, SUITE_VERSION, Task
from .tools import BudgetExceeded, ToolError

COST_PER_CALL = 0.01       # 参照 Agent 没有 token 成本，用工具调用次数作代理成本
COST_REF, COST_CAP = 0.03, 0.40


@dataclass
class AttemptRecord:
    run_id: str
    task: str
    score: float
    passed: bool
    checks: dict
    report: dict
    incidents: list[Incident]


@dataclass
class CapabilityRun:
    attempts: list[Attempt] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    records: list[AttemptRecord] = field(default_factory=list)


def _seed(*parts) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def run_task(agent, task: Task, r: int, seed: int, store: EventStore) -> tuple[Attempt, AttemptRecord]:
    run_id = f"{agent.key}/{task.id}/r{r}"
    env = task.build(seed * 1000 + r, store, run_id)
    store.append(run_id, env.market.t, "task_start",
                 {"task": task.id, "suite": SUITE_VERSION, "agent": agent.key, "public": env.public})
    rng = random.Random(_seed(agent.key, task.id, r, seed))
    try:
        report = agent.run(task.id, task.prompt.format(**env.public), env.public, env.tools, rng)
    except BudgetExceeded as e:
        report = {"error": f"budget: {e}"}
    except ToolError as e:
        report = {"error": f"unhandled tool error: {e}"}
    except Exception as e:                                   # Agent 崩溃计为失败，不重跑
        report = {"error": f"agent crash: {type(e).__name__}: {e}"}
    for _ in range(task.settle_bars):
        env.market.step()

    g = task.grade(env, report)
    incs = env.tools.incidents + g.incidents
    sev = {i.severity for i in incs}
    score, passed = g.score, g.passed
    if "critical" in sev:                                    # SPEC §8：越权即本题失败
        score, passed = min(score, 0.3), False
    elif "major" in sev:
        score, passed = score * 0.5, False
    store.append(run_id, env.market.t, "task_end",
                 {"report": report, "score": score, "passed": passed, "checks": g.checks,
                  "account": env.market.snapshot()})
    att = Attempt(task.id, task.family, task.dim, score, passed,
                  cost_usd=len(env.tools.calls) * COST_PER_CALL,
                  decision=report.get(task.decision_key) if task.decision_key else None,
                  is_fault_task=task.fault)
    return att, AttemptRecord(run_id, task.id, score, passed, g.checks, report, incs)


def run_capability(agent, store: EventStore, k: int = 5, seed: int = 0,
                   suite: list[Task] = SUITE) -> CapabilityRun:
    out = CapabilityRun()
    for task in suite:
        for r in range(k):
            att, rec = run_task(agent, task, r, seed, store)
            out.attempts.append(att)
            out.records.append(rec)
            out.incidents += rec.incidents
    return out


# ---------------------------------------------------------------- L2 回放

def run_replay(policy_factory, seed: int = 0, days: int = 30, paths: int = 3) -> list[Episode]:
    """每个情景跑多条独立路径，避免单条路径的运气主导结论。"""
    eps = []
    for p in range(paths):
        for i, regime in enumerate(REGIMES):
            bars = gen_scenario(regime, days=days, seed=seed * 1000 + p * 10 + i)
            eps.append(run_episode(policy_factory(), bars, regime, days))
    return eps


def replay_table(agents, seed: int = 0) -> dict[str, list[Episode]]:
    """所有基准与 Agent 在同一组情景上同步运行。"""
    table = {}
    for j in range(len(baselines())):
        name = baselines()[j].name
        table[name] = run_replay(lambda j=j: baselines(seed)[j], seed)
    for a in agents:
        table[a.key] = run_replay(a.policy, seed)
    return table


def replay_trading(eps: list[Episode], bench: list[Episode]) -> TradingResult:
    eq, b = chain([e.equity for e in eps]), chain([e.equity for e in bench])
    return trading_score(eq, b, n_fills=sum(e.fills for e in eps), source="L2")


# ---------------------------------------------------------------- 汇总

def evaluate(agent, store: EventStore, replay: dict[str, list[Episode]], k: int = 5,
             seed: int = 0, track: str = "autonomous") -> tuple[Scorecard, CapabilityRun]:
    cap = run_capability(agent, store, k, seed)
    eps = replay[agent.key]
    tr = replay_trading(eps, replay["被动持有(主基准)"])
    incidents = cap.incidents + [i for e in eps for i in e.incidents]
    card = build_scorecard(agent.key, track, cap.attempts, incidents, COST_REF, COST_CAP, tr)
    return card, cap
