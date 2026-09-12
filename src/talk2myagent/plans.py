from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_RECORDING_REQUEST_HINTS = (
    "could",
    "can",
    "may",
    "might",
    "would",
    "please",
    "ask",
    "permission",
    "consent",
    "authorize",
)


def sanitize_opening_text(value: str) -> str:
    """Remove explicit recording/transcription permission requests from spoken openings."""
    lowered = value.lower()
    if "record" not in lowered or "transcrib" not in lowered:
        return value.strip()

    # Split on major sentence/phrase boundaries, including commas, to remove only the
    # problematic request tail while keeping the valid opening intact.
    parts = re.split(r"(?<=[,.;!?])\s*", value)
    cleaned: list[str] = []
    removed = False
    for part in parts:
        text = part.strip()
        if not text:
            continue
        lower = text.lower()
        if (
            "record" in lower
            and "transcrib" in lower
            and any(hint in lower for hint in _RECORDING_REQUEST_HINTS)
        ):
            removed = True
            continue
        cleaned.append(text)

    if not cleaned:
        raise ValueError("Opening and greetings must not ask for recording/transcription consent.")

    text = " ".join(cleaned).strip().strip(",")
    text = re.sub(r"\s{2,}", " ", text)

    if removed and ("record" not in text.lower() or "transcrib" not in text.lower()):
        return text

    if (
        "record" in lowered
        and "transcrib" in lowered
        and any(hint in lowered for hint in _RECORDING_REQUEST_HINTS)
    ):
        raise ValueError("Opening and greetings must not ask for recording/transcription consent.")

    return text


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

    @field_validator("opening")
    @classmethod
    def _sanitize_opening(cls, value: str) -> str:
        return sanitize_opening_text(value)

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

    @field_validator("greeting")
    @classmethod
    def _sanitize_greeting(cls, value: str) -> str:
        return sanitize_opening_text(value)

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
        facts={
            "order_id": order_id,
            "item": item,
            "reason": reason,
            "action": action,
            **({"account_email": "alex.demo@example.com"} if is_demo else {}),
        },
        opening=(
            f"Hi, this is Ansh's personal assistant, calling in regards to {action} "
            f"the {item}, order {order_id}."
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
