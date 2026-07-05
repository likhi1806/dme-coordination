"""The simulated WORLD: counterparty personas + hidden scenario scripts.

Only the simulation layer reads scenarios.yaml. The orchestrator never sees it —
it experiences no-answers, ghosting, and wrong orders exactly the way production
would: as call outcomes and inbound events."""
from __future__ import annotations

from typing import Any, Optional

import yaml

from app import config


class ScenarioWorld:
    def __init__(self, path=None):
        with open(path or config.DATA_DIR / "scenarios.yaml") as f:
            self.raw = yaml.safe_load(f)
        self._attempt_count: dict[str, int] = {}   # counterparty name -> calls so far

    def _entry(self, name: str) -> Optional[dict[str, Any]]:
        for section in ("suppliers", "pcp", "patient"):
            if name in self.raw.get(section, {}):
                return self.raw[section][name]
        return None

    def next_attempt(self, name: str) -> int:
        """Register a call attempt; returns 1-based attempt number."""
        self._attempt_count[name] = self._attempt_count.get(name, 0) + 1
        return self._attempt_count[name]

    def answers(self, name: str, attempt: int) -> bool:
        entry = self._entry(name)
        if not entry:
            return True  # unknown counterparties always answer, generic persona
        script = entry.get("attempts", ["answer"])
        # Past the scripted list, repeat the last behavior.
        behavior = script[min(attempt, len(script)) - 1]
        return behavior == "answer"

    def persona(self, name: str, attempt: int) -> str:
        entry = self._entry(name)
        if not entry:
            return (f"You are a staff member at {name}. Answer the phone naturally "
                    "and be generically helpful.")
        stages = entry.get("persona_stages")
        if stages:  # staged persona (PCP office changes story across calls)
            return stages[min(attempt, len(stages)) - 1]
        return entry["persona"]

    def world_effects(self, name: str, attempt: int) -> list[dict[str, Any]]:
        """Out-of-band consequences of a call — e.g. 'the office actually sends
        the order N sim-hours later'. Returned to whoever wires the world."""
        entry = self._entry(name) or {}
        hits = [dict(e) for e in entry.get("effects", []) if e.get("after_attempt") == attempt]
        for e in hits:
            e["counterparty"] = name
        return hits
