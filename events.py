"""Event Store（SPEC §9）：仅追加写入，哈希链防篡改。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


def _digest(rec: dict) -> str:
    body = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class EventStore:
    def __init__(self, path: str | Path | None = None):
        self.records: list[dict] = []
        self._prev = GENESIS
        self._fh = open(path, "w", encoding="utf-8") if path else None

    def append(self, run_id: str, t: int, type: str, payload: dict) -> dict:
        rec = {"seq": len(self.records), "run_id": run_id, "t": t, "type": type,
               "payload": payload, "prev": self._prev}
        rec["hash"] = self._prev = _digest(rec)
        self.records.append(rec)
        if self._fh:
            self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def for_run(self, run_id: str) -> list[dict]:
        return [r for r in self.records if r["run_id"] == run_id]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    @staticmethod
    def verify(records: list[dict]) -> bool:
        prev = GENESIS
        for r in records:
            body = {k: v for k, v in r.items() if k != "hash"}
            # 从 JSONL 读回时 payload 已是纯 JSON，重算哈希结果一致
            if r["prev"] != prev or _digest(body) != r["hash"]:
                return False
            prev = r["hash"]
        return True

    @staticmethod
    def load(path: str | Path) -> list[dict]:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
