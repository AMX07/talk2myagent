from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=100)
    phone_number: str
    phone_source: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=2000)
    customer_name: str = Field(min_length=1, max_length=100)
    facts: dict[str, str] = Field(default_factory=dict)
    opening: str = Field(min_length=1, max_length=1000)
    dialogue: dict[str, str] = Field(min_length=1)
    allowed_actions: list[str] = Field(min_length=1)
    stop_conditions: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    is_demo: bool = False

    @field_validator("phone_number")
    @classmethod
    def phone(cls, value: str) -> str:
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
            raise ValueError("Use an E.164 phone number, e.g. +18882804331, without extensions.")
        return value

    def fingerprint(self) -> str:
        data = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()[:16]


class TestScenario(BaseModel):
    """A developer's task and expected outcome, without a dialable destination."""

    model_config = ConfigDict(extra="forbid")
    user_request: str = Field(min_length=1, max_length=4000)
    recipient_role: str = Field(min_length=1, max_length=200)
    customer_name: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=2000)
    facts: dict[str, str] = Field(default_factory=dict)
    greeting: str = Field(min_length=1, max_length=600)
    dialogue: dict[str, str] = Field(min_length=1)
    allowed_actions: list[str] = Field(min_length=1)
    stop_conditions: list[str] = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)

    def call_plan(self) -> CallPlan:
        return CallPlan(
            company=self.recipient_role[:100],
            phone_number="+12025550123",
            phone_source="Role-play sentinel; dialing is disabled by the engine.",
            customer_name=self.customer_name,
            objective=self.objective,
            facts=self.facts,
            opening=self.greeting,
            dialogue=self.dialogue,
            allowed_actions=self.allowed_actions,
            stop_conditions=self.stop_conditions,
            success_criteria=self.success_criteria,
            is_demo=True,
        )


class CriterionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_index: int = Field(ge=0)
    verdict: Literal["met", "not_met", "unknown"]
    evidence_seq: list[int] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=1000)


def amazon_plan(
    action: Literal["return", "cancel"],
    customer_name: str,
    order_id: str,
    item: str,
    reason: str,
    phone_number: str,
    phone_source: str,
    is_demo: bool = False,
) -> CallPlan:
    request = f"Please help {action} the {item}, order {order_id}. The reason is: {reason}."
    return CallPlan(
        company="Amazon US",
        phone_number=phone_number,
        phone_source=phone_source,
        objective=f"{action.title()} {item}; obtain the confirmation and next steps.",
        customer_name=customer_name,
        facts={"order_id": order_id, "item": item, "reason": reason, "action": action},
        opening=(
            f"Hello, I'm an AI assistant calling on behalf of {customer_name}. "
            "May I record and transcribe this conversation to help them follow up?"
        ),
        dialogue={
            "after_recording_consent": request,
            "order_id": f"The order number is {order_id}.",
            "reason": reason,
            "verification": "The account holder can complete verification directly. Please hold.",
            "if_cancellation_unavailable": (
                "Please explain the return options, fees, deadline, and refund method. "
                "Do not initiate a different action yet."
            ),
            "confirm": (
                "Please confirm the exact item, any fees, refund method, and next steps "
                "before proceeding."
            ),
            "close": "Please give me the case or confirmation number. Thank you for your help.",
        },
        allowed_actions=[
            f"Request {action} of this exact item only",
            "Ask for confirmation and next steps",
        ],
        stop_conditions=[
            "Account holder must complete identity verification",
            "Any fee, purchase, subscription, or different item/action is proposed",
            "Recording is declined: stop recording; let the user decide how to continue",
            "The representative requires a human account holder",
        ],
        success_criteria=[
            f"Representative explicitly confirms {action} status for the specified item",
            "Record reference number, refund details, and any remaining user actions",
        ],
        is_demo=is_demo,
    )
