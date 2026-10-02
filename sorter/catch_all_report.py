"""Lightweight, application-session counts of acknowledged Catch-All drops."""
from __future__ import annotations

import csv
import io
import threading
from collections import Counter
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv_text(value: str) -> str:
    # Prevent server-supplied names from becoming spreadsheet formulas.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


class CatchAllReport:
    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.RLock()
        self._counts = Counter()
        self.started_at = _utc_now()
        self._subscriptions = (
            ("run/dropped", self.record),
            ("run/counters_reset", self.reset),
            ("run/slot_counter_reset", self.reset_slot),
        )
        for topic, handler in self._subscriptions:
            bus.subscribe(topic, handler)

    def record(self, payload):
        if not isinstance(payload, dict) or payload.get("acknowledged") is not True or payload.get("slot") != 0:
            return
        key = (
            str(payload.get("sorter_connection") or "Unknown"),
            str(payload.get("model") or "Unknown"),
            str(payload.get("label") or "Unknown / no prediction"),
            str(payload.get("reason") or "unassigned"),
        )
        with self._lock:
            self._counts[key] += 1

    def snapshot(self):
        with self._lock:
            rows = [
                {"sorter_connection": key[0], "model": key[1], "classification": key[2], "reason": key[3], "count": count}
                for key, count in sorted(self._counts.items(), key=lambda x: (-x[1], x[0]))
            ]
            return {"started_at": self.started_at, "count": sum(self._counts.values()), "rows": rows}

    def csv_bytes(self):
        state = self.snapshot()
        exported_at = _utc_now()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("sorter_connection", "model", "predicted_classification", "catch_all_reason", "count", "period_start_utc", "exported_at_utc"))
        for row in state["rows"]:
            writer.writerow(tuple(_csv_text(row[k]) for k in ("sorter_connection", "model", "classification", "reason")) + (row["count"], state["started_at"], exported_at))
        return output.getvalue().encode("utf-8-sig")

    def reset(self, _payload=None):
        with self._lock:
            self._counts.clear()
            self.started_at = _utc_now()

    def reset_slot(self, payload):
        try:
            slot = int(payload)
        except (TypeError, ValueError):
            return
        if slot == 0:
            self.reset()

    def shutdown(self):
        for topic, handler in self._subscriptions:
            self.bus.unsubscribe(topic, handler)
