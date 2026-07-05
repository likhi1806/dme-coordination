"""S2 — the PCP written-order chase: request → promise → SLA check → nudge →
receive → validate (wrong code bounces back for correction).

This is the case's critical path: Medicare requires the signed Standard Written
Order before any supplier can deliver."""
from __future__ import annotations

from datetime import timedelta

from app import config, rules
from app.agents import goals
from app.models import PCPOrderState, WrittenOrder


class PCPFlow:
    async def h_request_pcp_order(self) -> None:
        await self._pcp_call(goals.pcp_order_request_goal(self.case), label="order request")

    async def _pcp_call(self, goal, label: str) -> None:
        t = self.case.pcp_track
        t.request_attempts += 1
        record = await self.place_call(goal)
        t.call_ids.append(record.id)
        if not record.answered:
            self.schedule("request_pcp_order", delay_hours=3)
            return
        if not self.outcome_ok(record, f"PCP {label}"):
            # Escalated, but don't dead-end the critical path: re-attempt so the
            # order keeps moving even if the human reviewer is slow.
            self.schedule("request_pcp_order", delay_hours=4)
            return
        o = record.outcome
        if o.get("office_had_no_record"):
            self.log("pcp", "⚠ PCP office had NO RECORD of the prior request — the classic "
                            "'fell in a hole'. Request re-placed on this call.")
        if o.get("order_promised"):
            hours = o.get("promised_within_hours") or 48
            t.state = PCPOrderState.REQUESTED
            t.promised_by = self.now() + timedelta(hours=hours)
            self.add_promise(self.case.pcp.practice,
                             f"Send signed written order ({label})", hours,
                             "pcp_sla_check", grace_hours=config.PCP_PROMISE_GRACE_HOURS,
                             attempt=t.request_attempts)
        else:
            self.escalate("PCP office would not commit to the written order",
                          f"Call {record.id}: no commitment obtained.",
                          "Advocate should contact the practice manager directly.")

    async def h_pcp_sla_check(self, promise_id: str, attempt: int) -> None:
        t = self.case.pcp_track
        p = self.promise(promise_id)
        # Stale timer guards: order already arrived, or a newer request supersedes this one.
        if t.state in (PCPOrderState.RECEIVED, PCPOrderState.VALID) or (p and p.fulfilled):
            return
        if t.request_attempts != attempt:
            return
        t.state = PCPOrderState.STALLED
        t.stall_count += 1
        self.log("pcp", f"⏰ SLA breach: written order promised by "
                        f"{t.promised_by:%a %b %d %H:%M} has NOT arrived (stall #{t.stall_count}).")
        if t.stall_count >= config.PCP_MAX_STALLS_BEFORE_ESCALATION:
            self.escalate("PCP order stalled repeatedly",
                          f"{t.stall_count} broken commitments from {self.case.pcp.practice}.",
                          "Advocate should call the practice manager / request escalation path.")
            return
        await self._pcp_call(goals.pcp_nudge_goal(self.case), label="nudge")

    async def h_inbound_order(self, hcpcs_code: str, description: str, signed_by: str) -> None:
        """Generic inbound handler — production shape: fax/portal/webhook intake."""
        t = self.case.pcp_track
        t.order = WrittenOrder(hcpcs_code=hcpcs_code, description=description,
                               signed_by=signed_by, received_at=self.now())
        t.state = PCPOrderState.RECEIVED
        for p in self.case.promises:
            if p.who == self.case.pcp.practice and not p.fulfilled:
                p.fulfilled = True
        self.log("pcp", f"📄 Written order received: {description} ({hcpcs_code}), "
                        f"signed by {signed_by}")

        ok, reason = rules.validate_written_order(self.case)
        if ok:
            t.state = PCPOrderState.VALID
            for c in self.case.checklist:
                if c.requirement.startswith("Standard Written Order"):
                    c.status, c.detail = "met", f"Received & validated {self.now():%b %d %H:%M}."
            self.log("pcp", f"✓ Order validated: code matches required equipment "
                            f"({self.case.equipment_hcpcs}).")
            self.check_match_gate()
        else:
            t.state = PCPOrderState.INVALID
            t.invalid_reason = reason
            self.log("pcp", f"✗ Order INVALID — {reason}")
            await self._pcp_call(goals.pcp_correction_goal(self.case, reason), label="correction")
