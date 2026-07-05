"""Eval harness: measure the voice agent instead of vibe-checking it.

The trick: in a simulated call, the persona's hidden facts ARE the ground-truth
label. We generate N randomized supplier personas (seeded RNG → reproducible),
run the REAL production pipeline (conversation agent → transcript → extractor),
then score the extracted CallOutcome against the persona's known facts.

This is the regression suite you run on every prompt change: "did qualification
extraction accuracy drop?" — the same personas double as CI fixtures.

Usage:  .venv/bin/python evals/run_evals.py --n 10        (needs an LLM key)
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import eleanor_case
from app.agents.goals import supplier_qualification_goal
from app.llm.provider import USAGE, estimated_cost_usd, get_provider
from app.models import Supplier, SupplierContact
from app.voice.engine import VoiceEngine
from app.voice.telephony import SimulatedTelephony

QUIRKS = [
    "You are chatty and go off on small tangents before answering.",
    "You are rushed and give minimal answers unless pressed.",
    "You are new on the job and slightly unsure; you double-check before answering.",
    "You are friendly but mishear one question the first time and answer after clarification.",
    "You volunteer extra irrelevant details about other products.",
]


def make_truth(rng: random.Random) -> dict:
    truth = {
        "accepting_medicare": rng.random() < 0.75,
        "k0001_in_stock": rng.random() < 0.7,
        "accepts_assignment": rng.random() < 0.85,
        "serves_address": rng.random() < 0.9,
        "delivery_window_days": rng.randint(2, 10),
    }
    truth["qualified"] = all([truth["accepting_medicare"], truth["k0001_in_stock"],
                              truth["serves_address"]])
    return truth


def make_persona(truth: dict, rng: random.Random, i: int) -> str:
    return f"""You are a rep at Eval Supplier #{i}, a DME supplier. {rng.choice(QUIRKS)}
Your business facts (answer truthfully IF asked; never volunteer all at once):
- Accepting new Medicare patients: {truth['accepting_medicare']}
- Standard manual wheelchair (K0001) in stock: {truth['k0001_in_stock']}
- Accept Medicare assignment: {truth['accepts_assignment']}
- Deliver to the caller's patient address in Chicago: {truth['serves_address']}
- Earliest delivery: {truth['delivery_window_days']} business days
If you are not accepting Medicare patients or lack stock, say so plainly when asked."""


class EvalWorld:
    """Duck-types ScenarioWorld for a single one-off persona."""
    def __init__(self, persona: str):
        self._persona = persona
    def next_attempt(self, name): return 1
    def answers(self, name, attempt): return True
    def persona(self, name, attempt): return self._persona
    def world_effects(self, name, attempt): return []


def score(truth: dict, outcome: dict) -> dict[str, bool | None]:
    """None = not scoreable (agent legitimately ended early on a disqualifier)."""
    s: dict[str, bool | None] = {}
    disqualified = not truth["qualified"]
    for field in ("accepting_medicare", "k0001_in_stock", "accepts_assignment",
                  "serves_address"):
        got = outcome.get(field)
        if got is None and disqualified:
            s[field] = None      # unasked because call was cut short — acceptable
        else:
            s[field] = got == truth[field]
    days = outcome.get("delivery_window_days")
    s["delivery_window_days"] = (None if disqualified and days is None
                                 else days == truth["delivery_window_days"])
    # The decision-relevant bit: did we reach the right verdict?
    s["verdict"] = bool(outcome.get("disqualify_reason")) == disqualified
    return s


async def run_one(engine: VoiceEngine, llm, i: int, seed: int) -> tuple[dict, dict, dict]:
    rng = random.Random(seed * 1000 + i)
    truth = make_truth(rng)
    case = eleanor_case()
    sc = SupplierContact(supplier=Supplier(
        name=f"Eval Supplier #{i}", phone="(555) 000-0000", address="Chicago, IL"))
    world = EvalWorld(make_persona(truth, rng, i))
    telephony = SimulatedTelephony(llm, world)
    engine = VoiceEngine(telephony, llm)
    goal = supplier_qualification_goal(case, sc)
    record, _ = await engine.place_call(goal, now=__import__("datetime").datetime(2026, 7, 4))
    return truth, record.outcome, score(truth, record.outcome)


async def main(n: int, seed: int, concurrency: int) -> None:
    llm = get_provider()
    sem = asyncio.Semaphore(concurrency)

    async def bounded(i):
        async with sem:
            return await run_one(None, llm, i, seed)

    results = await asyncio.gather(*(bounded(i) for i in range(n)))

    fields = ["accepting_medicare", "k0001_in_stock", "accepts_assignment",
              "serves_address", "delivery_window_days", "verdict"]
    print(f"\n{'#':<4}" + "".join(f"{f[:14]:<16}" for f in fields) + "conf")
    totals = {f: [0, 0] for f in fields}  # correct, scoreable
    for i, (truth, outcome, s) in enumerate(results):
        row = f"{i:<4}"
        for f in fields:
            v = s[f]
            row += f"{'—' if v is None else '✓' if v else '✗ MISS':<16}"
            if v is not None:
                totals[f][1] += 1
                totals[f][0] += int(v)
        print(row + f"{outcome.get('confidence', 0):.2f}")
        if s["verdict"] is False:
            print(f"     truth={truth}")
            print(f"     extracted={outcome}")

    print("\n=== field accuracy (correct / scoreable) ===")
    for f in fields:
        c, t = totals[f]
        print(f"  {f:<24} {c}/{t}" + (f"  ({c/t:.0%})" if t else ""))
    print(f"\nLLM usage: {USAGE['llm_calls']} calls, "
          f"{USAGE['input_tokens'] + USAGE['output_tokens']:,} tokens, "
          f"est. ${estimated_cost_usd():.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="number of randomized personas")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    asyncio.run(main(args.n, args.seed, args.concurrency))
