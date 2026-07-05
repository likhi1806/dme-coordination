"""Facade: one import surface for all call goals.

The goals live in per-counterparty modules (supplier_goals, pcp_goals,
patient_goals) with shared pieces in base.py. Import from here:

    from app.agents import goals
    goals.supplier_qualification_goal(case, sc)
"""
from app.agents.base import BASE_RULES, CONFIDENCE_FIELD, CallGoal  # noqa: F401
from app.agents.pcp_goals import (PCP_SCHEMA, pcp_correction_goal,  # noqa: F401
                                  pcp_nudge_goal, pcp_order_request_goal)
from app.agents.patient_goals import patient_update_goal  # noqa: F401
from app.agents.supplier_goals import (supplier_chase_goal,  # noqa: F401
                                       supplier_confirmation_goal,
                                       supplier_qualification_goal)
