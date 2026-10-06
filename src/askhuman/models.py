"""Shared wire models. Approval is an explicit boolean, never inferred from prose."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Kind = Literal[
    "ask",
    "decision",
    "approval",
    "clarification",
    "expertise",
    "verification",
    "exception",
    "notification",
]
Status = Literal["pending", "answered", "expired", "cancelled", "notified"]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=8000)
    kind: Kind = "ask"
    context: str = Field(default="", max_length=24000)
    options: list[ShortText] = Field(default_factory=list, max_length=20)
    recipient: str | None = Field(default=None, min_length=1, max_length=120)
    urgency: Literal["low", "normal", "high"] = "normal"
    timeout_seconds: int = Field(default=86400, ge=1, le=604800, strict=True)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.options)) != len(self.options):
            raise ValueError("Options must be unique")
        if self.kind == "approval":
            if self.options and self.options != ["Approve", "Reject"]:
                raise ValueError("Approval options are always Approve and Reject")
            self.options = ["Approve", "Reject"]
        if self.kind == "decision" and len(self.options) < 2:
            raise ValueError("Decisions require at least two options")
        if self.kind == "notification" and self.options:
            raise ValueError("Notifications do not accept options")
        return self


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: str = Field(default="", max_length=24000)
    selected_option: str | None = Field(default=None, max_length=500)
    approved: StrictBool | None = None
    respondent: str = Field(min_length=1, max_length=120)


class Answer(AnswerInput):
    request_id: str
    timestamp: datetime
    source: Literal["admin", "reply_link"]


class Delivery(BaseModel):
    channel: str
    status: Literal["pending", "sending", "delivered", "failed", "skipped"]
    attempts: int
    error: str | None = None


class Request(Question):
    id: str
    status: Status
    created_at: datetime
    expires_at: datetime
    response: Answer | None = None
    deliveries: list[Delivery] = Field(default_factory=list)
