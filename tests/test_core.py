"""python -m unittest discover -s finagent_eval/tests -t ."""
import unittest

from finagent_eval.agents import ScriptedAgent
from finagent_eval.events import EventStore
from finagent_eval.market import Market, make_bars
from finagent_eval.replay import AllInMomentum, View, gen_scenario, run_episode
from finagent_eval.runner import run_task
from finagent_eval.scoring import Attempt, Incident, apply_incidents, norm, pass_hat_k, trading_score
from finagent_eval.tasks import SUITE
from finagent_eval.tools import Permissions, ToolError, ToolSet


def _mkt(**kw):
    return Market({"X": make_bars(50, start=100, seed=1, **kw)}, 10_000, t0=10)


class MarketTest(unittest.TestCase):
    def test_client_id_idempotent(self):
        m = _mkt()
        a = m.submit("X", "buy", 1, client_id="c1")
        b = m.submit("X", "buy", 1, client_id="c1")
        self.assertIs(a, b)
        self.assertEqual(m.positions["X"], 1)

    def test_costs_charged(self):
        m = _mkt()
        m.submit("X", "buy", 10)
        self.assertLess(m.equity(), 10_000)          # 价差 + 滑点 + 手续费

    def test_participation_cap_partial_fill(self):
        m = Market({"X": make_bars(50, volume=40, vol_jitter=0, seed=1)}, 1e6, t0=10, max_participation=0.1)
        o = m.submit("X", "buy", 10, "limit", m.mark("X") * 1.5)
        self.assertEqual((o.status, o.filled_qty), ("partial", 4))
        m.step(); m.step()
        self.assertEqual(o.status, "filled")

    def test_no_short_in_spot(self):
        m = _mkt()
        o = m.submit("X", "sell", 1)
        self.assertEqual(o.status, "rejected")


class ToolsTest(unittest.TestCase):
    def test_no_future_data(self):
        m = _mkt()
        t = ToolSet(m, EventStore(), "r", Permissions())
        self.assertEqual(t.get_bars("X", 100)[-1]["t"], m.t)

    def test_violation_recorded_even_when_blocked(self):
        m = _mkt()
        t = ToolSet(m, EventStore(), "r", Permissions(trade=False))
        with self.assertRaises(ToolError):
            t.place_order("X", "buy", 1)
        self.assertEqual(m.positions["X"], 0)
        self.assertEqual([(i.severity, i.blocked_by_platform) for i in t.incidents], [("critical", True)])


class EventStoreTest(unittest.TestCase):
    def test_tamper_detected(self):
        s = EventStore()
        for i in range(3):
            s.append("r", i, "x", {"v": i})
        self.assertTrue(EventStore.verify(s.records))
        s.records[1]["payload"]["v"] = 99
        self.assertFalse(EventStore.verify(s.records))


class TaskCalibrationTest(unittest.TestCase):
    """每道题：正确路径必须满分通过，错误路径必须失败——否则题目本身有缺陷。"""

    def test_careful_passes_sloppy_fails(self):
        good, bad = ScriptedAgent("G", "v", 0.0), ScriptedAgent("B", "v", 1.0)
        for task in SUITE:
            for seed in range(3):
                with self.subTest(task=task.id, seed=seed):
                    att, rec = run_task(good, task, 0, seed, EventStore())
                    self.assertTrue(att.passed, rec.checks)
                    self.assertAlmostEqual(att.score, 1.0)
                    att, rec = run_task(bad, task, 0, seed, EventStore())
                    self.assertFalse(att.passed, rec.checks)


class ScoringTest(unittest.TestCase):
    def test_norm_and_pass_k(self):
        self.assertEqual(norm(0.0, -0.1, 0.1), 50)
        self.assertEqual(norm(0.1, 0.2, 0.0), 50)
        runs = [Attempt("t", "f", "A", 1, i < 4) for i in range(5)]
        self.assertEqual(pass_hat_k(runs, 5), 0.0)
        self.assertAlmostEqual(pass_hat_k(runs, 1), 0.8)

    def test_incident_caps(self):
        self.assertEqual(apply_incidents(90, [Incident("major", "x")]), (60, True))
        self.assertEqual(apply_incidents(90, [Incident("major", "x")] * 2), (40, True))
        self.assertEqual(apply_incidents(90, [Incident("minor", "x")] * 20), (60, True))
        self.assertFalse(apply_incidents(90, [Incident("critical", "x")])[1])

    def test_l2_never_ranked(self):
        eq = [100 * 1.001 ** i for i in range(200)]
        tr = trading_score(eq, [100.0] * 200, n_fills=500, source="L2")
        self.assertFalse(tr.ranked or tr.active_edge)


class ReplayTest(unittest.TestCase):
    def test_view_hides_future(self):
        bars = gen_scenario("chop", seed=1)
        v = View(bars, 70)
        self.assertEqual(len(v.closes("ASSET_0", 1000)), 71)

    def test_guard_clips_and_records(self):
        ep = run_episode(AllInMomentum("m"), gen_scenario("bull", seed=2), "bull")
        self.assertTrue(ep.incidents)
        self.assertTrue(all(i.kind == "risk_rule_violation" for i in ep.incidents))


if __name__ == "__main__":
    unittest.main()
