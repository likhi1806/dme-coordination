"""S4 — match, confirm, ghost-detect, fail over.

The match gate is the join point of the two parallel tracks: written order
VALID ∧ ≥1 supplier QUALIFIED. After confirmation, the supplier's callback
commitment becomes a promise timer — ghost detection is just that timer
expiring — and failover walks down the ranked backup list."""
from __future__ import annotations

from datetime import timedelta

from app import config
from app.agents import goals
from app.models import CasePhase, PCPOrderState, Promise, SupplierContact, SupplierState


class MatchFlow:
    def check_match_gate(self) -> None:
        """The join point: order VALID ∧ ≥1 supplier QUALIFIED → match."""
        if self.case.phase != CasePhase.COORDINATING:
            return
        if self.case.pcp_track.state != PCPOrderState.VALID:
            return
        ranked = self.case.qualified_ranked()
        if not ranked:
            return
        self.set_phase(CasePhase.READY_TO_MATCH)
        self.log("match", f"Match gate open: valid order + {len(ranked)} qualified supplier(s). "
                          f"Best candidate: {ranked[0].supplier.name} "
                          f"({ranked[0].delivery_window_days}d). Holding "
                          f"{max(0, len(ranked) - 1)} backup(s) against ghosting.")
        self.schedule("confirm_supplier", supplier_id=ranked[0].supplier.id)

    async def h_confirm_supplier(self, supplier_id: str, attempt: int = 1) -> None:
        sc = self.case.supplier_by_id(supplier_id)
        if sc is None or sc.state not in (SupplierState.QUALIFIED, SupplierState.CONFIRMING):
            return
        sc.state = SupplierState.CONFIRMING
        record = await self.place_call(goals.supplier_confirmation_goal(self.case, sc))
        sc.call_ids.append(record.id)
        o = record.outcome
        # A no-answer is not a refusal — give confirmation the same redial
        # courtesy as qualification before burning the best candidate. A busy
        # line is not a ghost; failing over on one missed pickup is too eager.
        if not record.answered and attempt < config.NO_ANSWER_MAX_RETRIES:
            self.log("match", f"{sc.supplier.name}: no answer on confirmation "
                              f"(attempt {attempt}); redial in {config.NO_ANSWER_RETRY_HOURS}h.")
            self.schedule("confirm_supplier", delay_hours=config.NO_ANSWER_RETRY_HOURS,
                          supplier_id=supplier_id, attempt=attempt + 1)
            return
        if not record.answered or not self.outcome_ok(record, "supplier confirmation") \
           or not o.get("order_confirmed"):
            self.log("match", f"{sc.supplier.name} did not confirm — trying next candidate.")
            sc.state = SupplierState.DISQUALIFIED
            sc.disqualify_reason = "Failed to confirm order"
            self._failover()
            return

        sc.state = SupplierState.CONFIRMED
        self.case.matched_supplier_id = sc.supplier.id
        days = o.get("delivery_in_days") or sc.delivery_window_days or 5
        sc.confirmed_delivery_date = self.now() + timedelta(days=days)
        self.set_phase(CasePhase.MATCHED)
        for c in self.case.checklist:
            if c.requirement.startswith("Supplier enrolled"):
                assign = "accepts assignment" if sc.accepts_assignment else "assignment NOT confirmed"
                c.status, c.detail = "met", f"{sc.supplier.name} — {assign}, order confirmed."
        self.log("match", f"🤝 MATCHED: {sc.supplier.name} confirmed {self.case.equipment_desc} "
                          f"({self.case.equipment_hcpcs}), delivery "
                          f"~{sc.confirmed_delivery_date:%a %b %d}.")

        # OUR most consequential commitment: Medicare's WOPD rule means the
        # supplier cannot bill until it holds the signed SWO. Track it as a
        # promise like any other — an untracked handoff is exactly the failure
        # mode ("we never got it") the system exists to prevent.
        self.transmit_swo(sc)

        next_step = o.get("next_step_promise") or "Call back to schedule the delivery window"
        due_h = o.get("next_step_due_hours") or 24
        self.add_promise(sc.supplier.name, next_step, due_h,
                         "supplier_promise_check", supplier_id=sc.supplier.id)

        self.schedule("patient_callback", milestone="supplier_matched",
                      summary=f"{sc.supplier.name} will deliver the "
                              f"{self.case.equipment_desc.lower()} around "
                              f"{sc.confirmed_delivery_date:%A, %B %d}. They'll call to "
                              "schedule the exact window; we're tracking it.")

    def transmit_swo(self, sc: SupplierContact) -> None:
        """Send the signed written order to the confirmed supplier and track
        receipt as a promise — WOPD means they can't bill without it."""
        order = self.case.pcp_track.order
        code = order.hcpcs_code if order else self.case.equipment_hcpcs
        p = Promise(who="Care team",
                    what=f"Signed written order ({code}) transmitted to {sc.supplier.name}; "
                         "confirm receipt before delivery",
                    due_at=self.now() + timedelta(hours=4))
        self.case.promises.append(p)
        self.log("match", f"📤 Signed written order ({code}) sent to {sc.supplier.name}; "
                          "Medicare requires it on file before billing. Awaiting receipt confirmation.")
        self.schedule("swo_receipt_confirmed", delay_hours=3,
                      promise_id=p.id, supplier_id=sc.supplier.id)

    async def h_swo_receipt_confirmed(self, promise_id: str, supplier_id: str) -> None:
        p = self.promise(promise_id)
        sc = self.case.supplier_by_id(supplier_id)
        if not p or p.fulfilled or not sc:
            return
        p.fulfilled = True
        self.log("match", f"✅ {sc.supplier.name} confirmed receipt of the signed written "
                          "order — clear to bill Medicare on delivery.")

    async def h_supplier_promise_check(self, promise_id: str, supplier_id: str) -> None:
        """Ghost detection = an expired promise. Chase once, then fail over."""
        p = self.promise(promise_id)
        sc = self.case.supplier_by_id(supplier_id)
        if not p or p.fulfilled or not sc or sc.state != SupplierState.CONFIRMED:
            return
        self.log("match", f"⏰ {sc.supplier.name} missed commitment: “{p.what}”. Chasing.")
        record = await self.place_call(goals.supplier_chase_goal(self.case, sc, p.what))
        sc.call_ids.append(record.id)
        o = record.outcome
        if record.answered and self.outcome_ok(record, "supplier chase") and o.get("still_committed"):
            due_h = o.get("next_step_due_hours") or 12
            self.add_promise(sc.supplier.name, o.get("next_step_promise") or "Follow through",
                             due_h, "supplier_promise_check", supplier_id=sc.supplier.id)
            return
        # Silent or bailed → GHOSTED, fail over to the next qualified candidate.
        sc.state = SupplierState.GHOSTED
        self.case.matched_supplier_id = None
        self.log("match", f"👻 {sc.supplier.name} GHOSTED after confirming. Failing over.")
        self.escalate(f"Supplier ghosted after confirmation: {sc.supplier.name}",
                      f"Confirmed delivery then missed “{p.what}” and did not respond to a "
                      "chase call. Auto-failover to backup candidate initiated.",
                      "FYI + consider flagging this supplier in the directory.")
        self._failover()

    def _failover(self) -> None:
        ranked = self.case.qualified_ranked()
        if ranked:
            self.set_phase(CasePhase.READY_TO_MATCH)
            self.log("match", f"Failover: next candidate is {ranked[0].supplier.name} "
                              f"({ranked[0].delivery_window_days}d).")
            self.schedule("confirm_supplier", supplier_id=ranked[0].supplier.id)
        else:
            self.set_phase(CasePhase.NEEDS_HUMAN)
            self.escalate("No qualified suppliers remain",
                          "All candidates disqualified, unreachable, or ghosted.",
                          "Widen the search radius / directory refresh.")

    async def h_inbound_supplier_callback(self, supplier_name: str) -> None:
        """Supplier proactively calls back to schedule the window (inbound event)."""
        sc = next((s for s in self.case.suppliers if s.supplier.name == supplier_name), None)
        if not sc or sc.state != SupplierState.CONFIRMED:
            return
        for p in self.case.promises:
            if p.who == supplier_name and not p.fulfilled:
                p.fulfilled = True
        self.set_phase(CasePhase.DELIVERY_SCHEDULED)
        self.log("match", f"📅 {supplier_name} called back and scheduled the delivery window "
                          f"(morning of {sc.confirmed_delivery_date:%a %b %d}). Promise kept.")
        self.schedule("patient_callback", milestone="delivery_scheduled",
                      summary=f"The {self.case.equipment_desc.lower()} arrives the morning of "
                              f"{sc.confirmed_delivery_date:%A, %B %d} from {supplier_name}. "
                              "Someone should be home. Medicare rents the equipment (up to 13 "
                              "months, then it's owned); the supplier bills Medicare and the "
                              "patient owes about 20% coinsurance per rental month — nothing upfront.")
