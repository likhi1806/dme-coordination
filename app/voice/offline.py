"""Offline scripted engine — no LLM, no network.

Drop-in for VoiceEngine (same place_call signature) returning pre-scripted
outcomes AND canned conversations that mirror data/scenarios.yaml. Three jobs:
  1. tests/test_flow.py — deterministic zero-LLM proof of the whole FSM
  2. VOICE_PROVIDER=offline — demo fallback if the API/network dies live
  3. feeds the dashboard's live-call view (turns stream with turn_delay pacing)
"""
from __future__ import annotations

import asyncio

from app.models import CallRecord, TranscriptTurn
from app.voice.telephony import CallResult

OK = {"confidence": 0.95, "answered_meaningfully": True}


def qualified(days: int) -> dict:
    return {**OK, "accepting_medicare": True, "k0001_in_stock": True,
            "accepts_assignment": True, "serves_address": True,
            "delivery_window_days": days, "disqualify_reason": ""}


AGENT = "agent"

# Per counterparty: their Nth call = "NA" (no answer) or
# (outcome, world_effects, [(speaker, line), ...]).
SCRIPT: dict[str, list] = {
    "Windy City Medical Supply": [
        "NA", "NA",
        (qualified(5), [], [
            ("them", "Windy City Medical Supply, Dana speaking."),
            (AGENT, "Hi Dana — I'm a care coordinator calling for a Medicare patient in "
                    "Chicago who needs a standard manual wheelchair, code K0001. "
                    "Are you taking new Medicare patients right now?"),
            ("them", "We are, yes. Sorry if you had trouble reaching us — it's been hectic."),
            (AGENT, "No problem. Do you have the K0001 in stock, and do you accept Medicare "
                    "assignment?"),
            ("them", "In stock, and yes, we take assignment. Earliest delivery in the city "
                     "would be about five business days."),
            (AGENT, "Perfect — her written order is being finalized, so we may call back to "
                    "confirm. Thanks, Dana."),
        ]),
    ],
    "Lakeshore Home Health Equipment": [
        ({**OK, "accepting_medicare": True, "k0001_in_stock": False, "serves_address": True,
          "delivery_window_days": None,
          "disqualify_reason": "K0001 out of stock, no restock date"}, [], [
            ("them", "Lakeshore Home Health, this is Marcus."),
            (AGENT, "Hi Marcus — calling for a Medicare patient who needs a standard manual "
                    "wheelchair, K0001. Do you have it in stock?"),
            ("them", "Ah — we're actually out of the standard manual right now. Supply chain. "
                     "No restock date I can promise. I could do a lightweight K0004 upgrade?"),
            (AGENT, "She's approved for the K0001 specifically, so a different code would be a "
                    "coverage problem. When you say no restock date — nothing this month?"),
            ("them", "Honestly, I can't commit to anything. Sorry."),
            (AGENT, "Understood, thanks for being straight about it. Take care."),
        ]),
    ],
    "Chicago Mobility Solutions": [
        (qualified(2), [], [
            ("them", "Chicago Mobility Solutions, Rita here!"),
            (AGENT, "Hi Rita — care coordination calling, for a Medicare patient in Chicago "
                    "needing a standard manual wheelchair, K0001. Taking new Medicare patients?"),
            ("them", "Absolutely! K0001 is in stock, we take assignment, and we could deliver "
                     "in two business days anywhere in the city."),
            (AGENT, "That's excellent. Her written order is in progress — I'll likely call back "
                    "shortly to confirm the order. Thanks, Rita!"),
        ]),
        ({**OK, "order_confirmed": True, "delivery_in_days": 2,
          "next_step_promise": "Call back within 1 day to schedule the delivery window",
          "next_step_due_hours": 24}, [], [
            ("them", "Chicago Mobility, Rita!"),
            (AGENT, "Hi Rita, following up as promised — the signed written order for Eleanor "
                    "Martinez is in hand: standard manual wheelchair, K0001. Can you confirm "
                    "the order and a delivery date?"),
            ("them", "Wonderful! Confirmed — we'll have it to her in two business days."),
            (AGENT, "Great. What's the next step for scheduling the exact window — and by when?"),
            ("them", "I'll call you back within a day to lock the delivery window. Promise!"),
            (AGENT, "Noted — talk tomorrow, Rita. Thanks!"),
        ]),
        "NA",  # the chase call — Rita has vanished
    ],
    "Prairie DME Partners": [
        ({**OK, "accepting_medicare": False, "k0001_in_stock": None, "serves_address": None,
          "disqualify_reason": "Not accepting new Medicare patients"}, [], [
            ("them", "Prairie DME, front desk."),
            (AGENT, "Hi — calling for a Medicare patient who needs a standard manual "
                    "wheelchair, K0001. Are you accepting new Medicare patients?"),
            ("them", "Not right now — intake's full until next quarter. You'd want to try "
                     "someone else."),
            (AGENT, "Understood, thanks for the quick answer."),
        ]),
    ],
    "Great Lakes Medical Equipment": [
        (qualified(3), [], [
            ("them", "Great Lakes Medical Equipment, Priya speaking."),
            (AGENT, "Hi Priya — care coordination calling, for a Chicago Medicare patient needing "
                    "a standard manual wheelchair, K0001. Are you taking new Medicare patients, "
                    "and is the K0001 in stock?"),
            ("them", "Yes to both. We accept assignment, and we deliver to Chicago — earliest "
                     "would be three business days."),
            (AGENT, "Excellent. Her written order is being finalized; we may call back to "
                    "confirm. Thank you, Priya."),
        ]),
        ({**OK, "order_confirmed": True, "delivery_in_days": 3,
          "next_step_promise": "Call back within 1 day to schedule the delivery window",
          "next_step_due_hours": 24},
         [{"after_attempt": 2, "supplier_callback_after_hours": 18,
           "counterparty": "Great Lakes Medical Equipment"}], [
            ("them", "Great Lakes Medical, Priya."),
            (AGENT, "Hi Priya — the previous supplier fell through, and we have Eleanor "
                    "Martinez's signed order: standard manual wheelchair, K0001. Can you "
                    "confirm the order and a delivery date?"),
            ("them", "Confirmed. Delivery in three business days, and I'll call you back "
                     "within one day to fix the exact morning window."),
            (AGENT, "Perfect — confirmed delivery plus a callback within a day. Thanks, Priya."),
        ]),
    ],
    "South Side Medical Supply Co": [
        (qualified(6), [], [
            ("them", "South Side Medical. Joe."),
            (AGENT, "Hi Joe — Medicare patient in Chicago needs a standard manual wheelchair, "
                    "K0001. You taking new Medicare patients, and is it in stock?"),
            ("them", "Yeah, we take Medicare, got the chair. Six business days for delivery, "
                     "best I can do."),
            (AGENT, "Good to know — you may hear back from us. Thanks, Joe."),
        ]),
    ],
    "Sunrise Family Medicine": [
        ({**OK, "order_promised": True, "promised_within_hours": 48,
          "office_had_no_record": False}, [], [
            ("them", "Sunrise Family Medicine, this is Kayla."),
            (AGENT, "Hi Kayla — calling about Eleanor Martinez, seen by Dr. Chen three days "
                    "ago. There's a verbal order in her chart for a standard manual "
                    "wheelchair. Medicare needs the signed Standard Written Order before any "
                    "supplier can deliver — it should say K0001. Could Dr. Chen sign and send it?"),
            ("them", "I see the visit… yep, verbal order's noted. I'll get it in Dr. Chen's "
                     "queue — should go out within two days."),
            (AGENT, "Thank you — so signed and sent within two days. I'll follow up if it "
                    "hasn't arrived by then."),
        ]),
        ({**OK, "order_promised": True, "promised_within_hours": 24,
          "office_had_no_record": True},
         [{"after_attempt": 2, "deliver_order_after_hours": 20,
           "counterparty": "Sunrise Family Medicine",
           "order": {"hcpcs_code": "K0006", "description": "Heavy-duty wheelchair",
                     "signed_by": "Dr. Sarah Chen"}}], [
            ("them", "Sunrise Family Medicine, Kayla."),
            (AGENT, "Hi Kayla — following up on the written order for Eleanor Martinez, the "
                    "standard manual wheelchair, K0001. It was promised by yesterday and "
                    "hasn't arrived."),
            ("them", "Hmm… I'm not finding any request in the queue. It never made it to "
                     "Dr. Chen. I'm so sorry — let me take it down again right now."),
            (AGENT, "Thanks — to confirm: standard manual wheelchair, code K0001, signed by "
                    "Dr. Chen. When will it go out?"),
            ("them", "It'll be signed and sent within one day, I'll walk it over myself."),
            (AGENT, "Appreciated, Kayla. I'll check back tomorrow."),
        ]),
        ({**OK, "order_promised": True, "promised_within_hours": 24,
          "office_had_no_record": False},
         [{"after_attempt": 3, "deliver_order_after_hours": 5,
           "counterparty": "Sunrise Family Medicine",
           "order": {"hcpcs_code": "K0001", "description": "Standard manual wheelchair",
                     "signed_by": "Dr. Sarah Chen"}}], [
            ("them", "Sunrise Family Medicine, Kayla speaking."),
            (AGENT, "Hi Kayla — we received the order for Eleanor Martinez, but it's written "
                    "for a heavy-duty wheelchair, K0006. She needs the standard manual, "
                    "K0001. Billed as-is, the claim would be denied."),
            ("them", "Oh no — you're right, it was keyed wrong. I'll have Dr. Chen sign a "
                     "corrected order for the standard manual, K0001, and resend within a day."),
            (AGENT, "Thank you — corrected K0001 order within a day. I'll watch for it."),
        ]),
    ],
    "Eleanor Martinez": [
        ({**OK, "patient_understood": True, "cost_explained": True,
          "patient_concerns": "anxious about out-of-pocket cost"}, [], [
            ("them", "Hello?"),
            (AGENT, "Hi Mrs. Martinez, this is your care coordinator about the wheelchair. "
                    "Good news: Medicare covers it. We're getting the written order from "
                    "Dr. Chen and lining up suppliers — you don't need to call anyone."),
            ("them", "Oh, that's a relief. And… what will it cost me? I don't have one of "
                     "those extra plans."),
            (AGENT, "It's a rental — Medicare pays the supplier most of the cost each month, "
                    "you'd owe about 20% per month, and after 13 months the chair is yours. "
                    "They bill Medicare directly — never pay the full price upfront."),
            ("them", "So about 20% a month. Okay. Thank you, dear."),
        ]),
        "NA",
        ({**OK, "patient_understood": True, "cost_explained": True, "patient_concerns": ""}, [], [
            ("them", "Hello?"),
            (AGENT, "Hi Mrs. Martinez — sorry we missed you earlier. Update: a supplier is "
                    "confirmed for your wheelchair and delivery is being scheduled. "
                    "Remember, it's a monthly rental — you owe about 20% each month, "
                    "and it's yours after 13 months."),
            ("them", "Wonderful! I'll be home all week."),
        ]),
        ({**OK, "patient_understood": True, "cost_explained": True, "patient_concerns": ""}, [], [
            ("them", "Hello?"),
            (AGENT, "Mrs. Martinez, quick update — we've switched to a more reliable supplier, "
                    "Great Lakes Medical, delivery in about three days. Everything else stays "
                    "the same: they bill Medicare monthly, you owe about 20% per month."),
            ("them", "As long as it arrives, that's fine by me. Thank you."),
        ]),
        ({**OK, "patient_understood": True, "cost_explained": True, "patient_concerns": ""}, [], [
            ("them", "Hello?"),
            (AGENT, "Great news, Mrs. Martinez — your wheelchair arrives Saturday morning from "
                    "Great Lakes Medical. Someone should be home. You'll get a bill for about "
                    "about 20% coinsurance for each rental month, nothing upfront."),
            ("them", "Saturday morning — I'll be here. Thank you so much for handling all this."),
        ]),
    ],
}


# --- HARD MODE: every supplier path fails; the case must degrade gracefully ---
# into the advocate queue (NEEDS_HUMAN), proving the FSM handles arbitrary
# failure compositions — not one rigged happy path.
SCRIPT_HARD: dict[str, list] = {
    "Windy City Medical Supply": ["NA", "NA", "NA"],   # retry policy exhausts
    "Lakeshore Home Health Equipment": [
        ({**OK, "accepting_medicare": True, "k0001_in_stock": False, "serves_address": True,
          "delivery_window_days": None,
          "disqualify_reason": "K0001 out of stock, no restock date"}, [], [
            ("them", "Lakeshore Home Health, Marcus."),
            (AGENT, "Hi Marcus — Medicare patient in Chicago needs a standard manual "
                    "wheelchair, K0001. In stock?"),
            ("them", "We're out of the standard manual, no restock date. Sorry."),
            (AGENT, "Understood — thanks for the straight answer."),
        ]),
    ],
    "Chicago Mobility Solutions": [
        ({**OK, "accepting_medicare": False, "k0001_in_stock": None, "serves_address": None,
          "disqualify_reason": "Not accepting new Medicare patients"}, [], [
            ("them", "Chicago Mobility Solutions."),
            (AGENT, "Hi — are you accepting new Medicare patients? Standard manual "
                    "wheelchair, K0001."),
            ("them", "Intake's frozen indefinitely, sorry. Try someone else."),
            (AGENT, "Will do — thanks."),
        ]),
    ],
    "Prairie DME Partners": [
        ({**OK, "accepting_medicare": True, "k0001_in_stock": True, "serves_address": False,
          "disqualify_reason": "Does not deliver into Chicago proper"}, [], [
            ("them", "Prairie DME Partners."),
            (AGENT, "Hi — Medicare patient in Chicago needs a K0001 standard manual "
                    "wheelchair. Do you deliver to Chicago?"),
            ("them", "We stock it and take Medicare, but we only deliver Berwyn and "
                     "Cicero — not into the city."),
            (AGENT, "Ah, outside your delivery area then. Thanks anyway."),
        ]),
    ],
    "Great Lakes Medical Equipment": [
        (qualified(3), [], [
            ("them", "Great Lakes Medical, Priya."),
            (AGENT, "Hi Priya — Medicare patient in Chicago, standard manual wheelchair, "
                    "K0001. Taking new patients, in stock?"),
            ("them", "Yes and yes — assignment too. Three business days to Chicago."),
            (AGENT, "Great — we may call back to confirm once her order is signed."),
        ]),
        ({**OK, "order_confirmed": True, "delivery_in_days": 3,
          "next_step_promise": "Call back within 1 day to schedule the delivery window",
          "next_step_due_hours": 24}, [], [   # note: NO callback effect — she ghosts
            ("them", "Great Lakes, Priya."),
            (AGENT, "Priya, the signed K0001 order for Eleanor Martinez is in hand — "
                    "can you confirm the order and delivery?"),
            ("them", "Confirmed! Three business days, and I'll call you back within a "
                     "day to set the exact window."),
            (AGENT, "Perfect — talk tomorrow."),
        ]),
        "NA", "NA",
    ],
    "South Side Medical Supply Co": [
        (qualified(6), [], [
            ("them", "South Side Medical. Joe."),
            (AGENT, "Hi Joe — K0001 standard manual wheelchair for a Chicago Medicare "
                    "patient. Stock and delivery?"),
            ("them", "Got it in stock, take Medicare. Six days out."),
            (AGENT, "Noted — you may hear back from us."),
        ]),
        ({**OK, "order_confirmed": False,
          "other_notes": "Would not commit — delivery driver quit"}, [], [
            ("them", "South Side, Joe."),
            (AGENT, "Joe, we have the signed K0001 order for Eleanor Martinez — can you "
                    "confirm the order and a delivery date?"),
            ("them", "Ah… look, my delivery guy quit yesterday. I can't promise anything "
                     "this month. I'd hold off on me."),
            (AGENT, "Appreciate the honesty — we'll make other arrangements."),
        ]),
    ],
    "Sunrise Family Medicine": [
        ({**OK, "order_promised": True, "promised_within_hours": 48,
          "office_had_no_record": False}, [], [
            ("them", "Sunrise Family Medicine, Kayla."),
            (AGENT, "Hi Kayla — Eleanor Martinez needs her signed Standard Written Order "
                    "for a standard manual wheelchair, K0001, from Dr. Chen. Medicare "
                    "requires it before delivery."),
            ("them", "I see the chart — we're slammed, but it'll go out within two days."),
            (AGENT, "Two days, noted — I'll follow up if it hasn't arrived."),
        ]),
        ({**OK, "order_promised": True, "promised_within_hours": 24,
          "office_had_no_record": False},
         [{"after_attempt": 2, "deliver_order_after_hours": 18,
           "counterparty": "Sunrise Family Medicine",
           "order": {"hcpcs_code": "K0001", "description": "Standard manual wheelchair",
                     "signed_by": "Dr. Sarah Chen"}}], [
            ("them", "Sunrise Family Medicine, Kayla."),
            (AGENT, "Hi Kayla — following up: Eleanor's written order was promised by "
                    "yesterday and hasn't arrived."),
            ("them", "I'm sorry — Dr. Chen was out. It'll be signed and sent within a "
                     "day, I'll see to it personally."),
            (AGENT, "Thanks, Kayla — I'll watch for it tomorrow."),
        ]),
    ],
    "Eleanor Martinez": [
        ({**OK, "patient_understood": True, "cost_explained": True, "patient_concerns": ""}, [], [
            ("them", "Hello?"),
            (AGENT, "Hi Mrs. Martinez — Medicare covers your wheelchair; we're getting "
                    "Dr. Chen's written order and contacting suppliers. You'll owe about "
                    "about 20% each rental month; the supplier bills Medicare directly."),
            ("them", "Alright — thank you, dear."),
        ]),
        "NA", "NA",
        ({**OK, "patient_understood": True, "cost_explained": True,
          "patient_concerns": "worried after missed calls"}, [], [
            ("them", "Hello? Oh — sorry, I was at my sister's."),
            (AGENT, "No trouble at all, Mrs. Martinez. A supplier confirmed your "
                    "wheelchair; we're pinning down the delivery window and will call "
                    "with the date. Still a monthly rental — about 20% each month, nothing upfront."),
            ("them", "Thank you for keeping on it."),
        ]),
    ],
}

SCRIPTS = {"happy": SCRIPT, "hard": SCRIPT_HARD}


class ScriptedEngine:
    """Duck-types VoiceEngine.place_call(goal, now, on_turn, turn_delay)."""

    def __init__(self, script: dict | None = None) -> None:
        self.script = script or SCRIPT
        self.attempts: dict[str, int] = {}

    async def place_call(self, goal, now, on_turn=None, turn_delay: float = 0.0):
        name = goal.counterparty_name
        self.attempts[name] = self.attempts.get(name, 0) + 1
        script = self.script.get(name, [({**OK}, [], [])])
        step = script[min(self.attempts[name], len(script)) - 1]

        if step == "NA":
            if turn_delay:                       # let the dashboard show "ringing…"
                await asyncio.sleep(min(turn_delay * 2, 2.5))
            rec = CallRecord(call_type=goal.call_type, counterparty=name,
                             phone=goal.phone, answered=False, at=now)
            return rec, CallResult(answered=False)

        outcome, effects, lines = step
        transcript: list[TranscriptTurn] = []
        for speaker, text in lines:
            turn = TranscriptTurn(speaker=AGENT if speaker == AGENT else name, text=text)
            transcript.append(turn)
            if on_turn:
                on_turn(turn)
            if turn_delay:
                await asyncio.sleep(turn_delay)

        rec = CallRecord(call_type=goal.call_type, counterparty=name, phone=goal.phone,
                         answered=True, at=now, outcome=outcome,
                         confidence=outcome["confidence"], transcript=transcript)
        return rec, CallResult(answered=True, transcript=transcript,
                               world_effects=effects)
