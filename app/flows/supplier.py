"""S3 — supplier research: parallel qualification fan-out with retry/backoff.

Qualification gates (ALL must be true): accepting Medicare patients, requested
equipment in stock, serves the patient's address, accepts assignment. The last
one is a hard gate on purpose: a non-participating supplier can bill above the
Medicare-approved amount — for a no-Medigap patient that's exactly the cost
exposure we exist to prevent."""
from __future__ import annotations

import asyncio

from app import config
from app.agents import goals
from app.models import SupplierContact, SupplierState


class SupplierFlow:
    async def h_qualify_suppliers(self) -> None:
        targets = [s for s in self.case.suppliers
                   if s.state == SupplierState.NOT_CONTACTED][:config.MAX_SUPPLIERS_TO_CONTACT]

        # Parallel fan-out: a team of advocates dialing at once. Dial starts are
        # staggered by the demo pace so a live audience can follow (with real or
        # LLM calls the stagger is far shorter than a call, so it stays parallel).
        async def dial(i: int, sc: SupplierContact) -> None:
            if self.step_delay:
                await asyncio.sleep(i * self.step_delay)
            await self.qualify_one(sc)

        await asyncio.gather(*(dial(i, sc) for i, sc in enumerate(targets)))

    async def h_qualify_supplier(self, supplier_id: str) -> None:  # retry path
        sc = self.case.supplier_by_id(supplier_id)
        if sc and sc.state == SupplierState.NO_ANSWER:
            await self.qualify_one(sc)

    async def qualify_one(self, sc: SupplierContact) -> None:
        sc.state = SupplierState.CALLING
        sc.attempts += 1
        record = await self.place_call(goals.supplier_qualification_goal(self.case, sc))
        sc.call_ids.append(record.id)

        if not record.answered:
            if sc.attempts >= config.NO_ANSWER_MAX_RETRIES:
                sc.state = SupplierState.DISQUALIFIED
                sc.disqualify_reason = f"Unreachable after {sc.attempts} attempts"
                self.log("suppliers", f"✗ {sc.supplier.name}: disqualified — unreachable "
                                      f"({sc.attempts} attempts)")
            else:
                sc.state = SupplierState.NO_ANSWER
                self.schedule("qualify_supplier", delay_hours=config.NO_ANSWER_RETRY_HOURS,
                              supplier_id=sc.supplier.id)
                self.log("suppliers", f"… {sc.supplier.name}: no answer (attempt {sc.attempts}); "
                                      f"redial in {config.NO_ANSWER_RETRY_HOURS}h")
            return

        if not self.outcome_ok(record, f"supplier qualification ({sc.supplier.name})"):
            sc.state = SupplierState.NO_ANSWER      # treat as un-qualified; retry once
            self.schedule("qualify_supplier", delay_hours=2, supplier_id=sc.supplier.id)
            return

        o = record.outcome
        sc.accepting_medicare = o.get("accepting_medicare")
        sc.k0001_in_stock = o.get("k0001_in_stock")   # field = "requested code in stock"
        sc.accepts_assignment = o.get("accepts_assignment")
        sc.serves_address = o.get("serves_address")
        sc.delivery_window_days = o.get("delivery_window_days")

        required_true = [sc.accepting_medicare, sc.k0001_in_stock, sc.serves_address,
                         sc.accepts_assignment]
        if o.get("disqualify_reason") or not all(x is True for x in required_true):
            sc.state = SupplierState.DISQUALIFIED
            missing = [label for label, ok in (
                ("not accepting Medicare patients", sc.accepting_medicare),
                (f"{self.case.equipment_hcpcs} not in stock", sc.k0001_in_stock),
                ("outside service area", sc.serves_address),
                ("does not accept assignment", sc.accepts_assignment)) if ok is not True]
            sc.disqualify_reason = (o.get("disqualify_reason")
                                    or "; ".join(missing) or "Did not meet qualification criteria")
            self.log("suppliers", f"✗ {sc.supplier.name}: disqualified — {sc.disqualify_reason}")
        else:
            sc.state = SupplierState.QUALIFIED
            self.log("suppliers", f"✓ {sc.supplier.name}: QUALIFIED — delivery in "
                                  f"~{sc.delivery_window_days} business days")
        self.check_match_gate()
