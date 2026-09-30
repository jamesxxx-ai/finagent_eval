"""Market Simulator（SPEC §4.1 / §9）：撮合、费用、成交量参与率、部分成交、幂等订单。

价格由平台账本计算，Agent 只能通过工具读到当前时刻及以前的数据。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def bars_from_returns(rets: list[float], start: float = 100.0, volume: float = 1e6,
                      vol_jitter: float = 0.4, seed: int = 0) -> list[Bar]:
    rng = random.Random(seed)
    bars, px = [], start
    for t, r in enumerate(rets):
        o, c = px, max(1e-12, px * (1 + r))
        wick = abs(r) / 3 + 0.002
        h = max(o, c) * (1 + rng.uniform(0, wick))
        l = min(o, c) * (1 - rng.uniform(0, wick))
        v = volume * (1 + rng.uniform(-vol_jitter, vol_jitter))
        bars.append(Bar(t, o, h, l, c, v))
        px = c
    return bars


def make_bars(n: int, start: float = 100.0, drift: float = 0.0, vol: float = 0.02,
              volume: float = 1e6, vol_jitter: float = 0.4, seed: int = 0) -> list[Bar]:
    rng = random.Random(seed)
    rets = [rng.gauss(drift, vol) for _ in range(n)]
    return bars_from_returns(rets, start, volume, vol_jitter, seed + 1)


def _floor_lot(q: float, lot: float) -> float:
    return round(math.floor(q / lot + 1e-6) * lot, 10)


@dataclass
class Order:
    id: str
    seq: int
    client_id: str
    asset: str
    side: str
    qty: float
    type: str
    limit_px: float | None
    t_submit: int
    status: str = "open"          # open / partial / filled / cancelled / rejected
    filled_qty: float = 0.0
    avg_px: float = 0.0
    fees: float = 0.0
    reason: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_open(self) -> bool:
        return self.status in ("open", "partial")

    def to_dict(self) -> dict:
        return {**asdict(self), "remaining": self.remaining}


class Market:
    def __init__(self, bars: dict[str, list[Bar]], cash: float, t0: int = 0, *,
                 fee_bps: float = 10.0, spread_bps: float = 4.0, slip_bps: float = 3.0,
                 max_participation: float = 0.1, lot: float = 1e-8):
        self.bars, self.t, self.cash = bars, t0, cash
        self.fee_bps, self.spread_bps, self.slip_bps = fee_bps, spread_bps, slip_bps
        self.max_participation, self.lot = max_participation, lot
        self.positions: dict[str, float] = defaultdict(float)
        self.orders: dict[str, Order] = {}
        self.by_client: dict[str, str] = {}
        self.fills: list[dict] = []
        self._vol_used: dict[tuple[str, int], float] = defaultdict(float)
        self.seq = 0

    # ---- 估值
    def bar(self, asset: str, t: int | None = None) -> Bar:
        return self.bars[asset][self.t if t is None else t]

    def mark(self, asset: str) -> float:
        return self.bar(asset).c

    def equity(self) -> float:
        return self.cash + sum(q * self.mark(a) for a, q in self.positions.items())

    def snapshot(self) -> dict:
        pos = {a: q for a, q in self.positions.items() if abs(q) > self.lot / 2}
        return {"as_of": self.t, "cash": self.cash, "positions": pos, "equity": self.equity(),
                "open_orders": [o.to_dict() for o in self.orders.values() if o.is_open]}

    # ---- 订单
    def submit(self, asset: str, side: str, qty: float, type: str = "market",
               limit_px: float | None = None, client_id: str | None = None) -> Order:
        if client_id and client_id in self.by_client:          # 幂等：同一 client id 返回原单
            return self.orders[self.by_client[client_id]]
        self.seq += 1
        oid = f"O{self.seq:05d}"
        client_id = client_id or f"auto-{oid}"
        o = Order(oid, self.seq, client_id, asset, side, _floor_lot(qty, self.lot), type, limit_px, self.t)
        self.orders[oid], self.by_client[client_id] = o, oid
        if asset not in self.bars:
            o.status, o.reason = "rejected", "unknown asset"
        elif side not in ("buy", "sell") or o.qty <= 0:
            o.status, o.reason = "rejected", "invalid side/qty"
        elif type == "limit" and limit_px is None:
            o.status, o.reason = "rejected", "limit order without price"
        else:
            self._fill(o, self.mark(asset))
        return o

    def cancel(self, oid: str) -> Order:
        o = self.orders[oid]
        if o.is_open:
            o.status = "cancelled"
        return o

    def step(self) -> None:
        """推进一根 K 线；未完成订单按新 K 线开盘价继续撮合。"""
        self.t += 1
        for o in list(self.orders.values()):
            if o.is_open:
                self._fill(o, self.bar(o.asset).o)

    def _fill(self, o: Order, ref: float) -> None:
        bar = self.bar(o.asset)
        cap = self.max_participation * bar.v - self._vol_used[(o.asset, self.t)]
        adj = (self.spread_bps / 2 + self.slip_bps) / 1e4
        px = ref * (1 + adj) if o.side == "buy" else ref * (1 - adj)
        if o.type == "limit" and ((o.side == "buy" and px > o.limit_px) or
                                  (o.side == "sell" and px < o.limit_px)):
            return
        fee_rate = self.fee_bps / 1e4
        q = min(o.remaining, cap)
        if o.side == "buy":
            q = min(q, self.cash / (px * (1 + fee_rate)))
        else:
            q = min(q, self.positions[o.asset])               # 现货不可卖空
        q = _floor_lot(q, self.lot)
        if q <= 0:
            if cap > self.lot:                                # 不是成交量不足，而是余额/持仓不足
                o.status, o.reason = ("cancelled" if o.filled_qty else "rejected"), "insufficient balance"
            return
        fee = q * px * fee_rate
        if o.side == "buy":
            self.cash -= q * px + fee
            self.positions[o.asset] += q
        else:
            self.cash += q * px - fee
            self.positions[o.asset] -= q
        o.avg_px = (o.avg_px * o.filled_qty + px * q) / (o.filled_qty + q)
        o.filled_qty = round(o.filled_qty + q, 10)
        o.fees += fee
        o.status = "filled" if o.remaining <= self.lot / 2 else "partial"
        self._vol_used[(o.asset, self.t)] += q
        self.fills.append({"t": self.t, "order": o.id, "asset": o.asset, "side": o.side,
                           "qty": q, "px": px, "fee": fee})
