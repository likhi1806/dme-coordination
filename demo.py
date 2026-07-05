"""Headless end-to-end demo: runs Eleanor's case to completion and prints the
timeline as it happens. Same engine the dashboard uses.

    python demo.py              # run the case
    python demo.py --transcripts  # also dump every call transcript at the end
"""
import asyncio
import sys

from app import config
from app.bootstrap import build

MODE_BANNER = {
    "offline": "MODE: offline — no LLM key found, running scripted dialogues.\n"
               "      Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env for real "
               "LLM conversations.",
    "simulated": f"MODE: simulated calls — live LLM conversations via {config.LLM_PROVIDER}.",
    "vapi": "MODE: vapi — placing REAL outbound phone calls.",
}


async def main() -> None:
    mode = "hard" if "--hard" in sys.argv else "happy"
    print(MODE_BANNER.get(config.VOICE_PROVIDER, ""))
    print(f"SCENARIO: {mode}" + (" (adversarial — everything fails gracefully)"
                                 if mode == "hard" else "") + "\n")
    orch = build(mode=mode)
    orch.start()

    printed = 0

    def flush_events() -> int:
        nonlocal printed
        for e in orch.case.events[printed:]:
            print(f"[{e.at:%b %d %H:%M}] {e.kind:<10} {e.message}")
        printed = len(orch.case.events)
        return printed

    flush_events()
    # Drive the loop step-by-step so events print live.
    steps = 0
    while steps < 300 and orch.case.phase.value != "DONE":
        item = orch.scheduler.pop_due(orch.now())
        if item:
            await orch.handle(item)
            steps += 1
            flush_events()
            continue
        nxt = orch.scheduler.peek_next_time()
        if nxt is None:
            break
        orch.clock.advance_to(nxt)

    print("\n" + "=" * 72)
    print(f"FINAL PHASE: {orch.case.phase.value}")
    print(f"Calls placed: {len(orch.store.calls)} | "
          f"Escalations: {len(orch.store.escalations)} "
          f"({len(orch.store.open_escalations())} open)")
    matched = orch.case.supplier_by_id(orch.case.matched_supplier_id or "")
    if matched:
        print(f"Matched supplier: {matched.supplier.name}, "
              f"delivery {matched.confirmed_delivery_date:%a %b %d}")
    if config.VOICE_PROVIDER != "offline":
        from app.llm.provider import USAGE, estimated_cost_usd
        print(f"LLM cost this case: {USAGE['llm_calls']} calls, "
              f"{USAGE['input_tokens'] + USAGE['output_tokens']:,} tokens, "
              f"est. ${estimated_cost_usd():.3f}")

    if "--transcripts" in sys.argv:
        for rec in orch.store.calls.values():
            print(f"\n--- {rec.call_type.value} → {rec.counterparty} "
                  f"({'answered' if rec.answered else 'no answer'}) ---")
            for t in rec.transcript:
                print(f"  {t.speaker}: {t.text}")
            if rec.outcome:
                print(f"  outcome: {rec.outcome}")


if __name__ == "__main__":
    asyncio.run(main())
