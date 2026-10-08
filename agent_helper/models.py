"""Request and response bodies. Every text field has a hard length limit."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

LIMITS = {
    "message": 8000,
    "handle": 100,
    "contact_hint": 500,
    "board_content": 4000,
    "board_topic": 100,
    "report_text": 8000,
    "operator_note": 2000,
}

_FORBIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _no_control_chars(value: str | None) -> str | None:
    if value is not None and _FORBIDDEN_CONTROL.search(value):
        raise ValueError("control characters other than tab and newline are not allowed")
    return value


def _text(max_length: int) -> object:
    return Annotated[str, Field(min_length=1, max_length=max_length), AfterValidator(_no_control_chars)]


def _optional(max_length: int) -> object:
    return Annotated[str | None, Field(max_length=max_length), AfterValidator(_no_control_chars)]


MessageText = _text(LIMITS["message"])
Handle = _optional(LIMITS["handle"])
ContactHint = _optional(LIMITS["contact_hint"])
ReportText = _text(LIMITS["report_text"])
BoardContent = _text(LIMITS["board_content"])
BoardTopic = _optional(LIMITS["board_topic"])
OperatorNote = _optional(LIMITS["operator_note"])
HideReason = _text(LIMITS["operator_note"])


class RequestIn(BaseModel):
    message: MessageText
    handle: Handle = None
    contact_hint: ContactHint = None


class MessageIn(BaseModel):
    message: MessageText


class Created(BaseModel):
    id: str
    follow_up_token: str
    status_url: str
    note: str


class ConversationMessage(BaseModel):
    sender: Literal["agent", "operator"]
    created_at: str
    body: str


class RequestOut(BaseModel):
    id: str
    status: Literal["open", "answered", "closed"]
    created_at: str
    handle: str | None
    contact_hint: str | None
    messages: list[ConversationMessage]


ReportKind = Literal["bug", "feature", "capability", "security", "other"]
ReportStatus = Literal["quarantined", "accepted", "rejected", "duplicate"]


class ReportIn(BaseModel):
    kind: ReportKind = "other"
    text: ReportText


class ReportOut(BaseModel):
    id: str
    created_at: str
    kind: str
    body: str
    status: ReportStatus
    operator_note: str | None


class BoardIn(BaseModel):
    content: BoardContent
    author: Handle = None
    topic: BoardTopic = None


class BoardEntry(BaseModel):
    seq: int
    created_at: str
    author: str | None
    topic: str | None
    content: str | None
    hidden: bool
    hidden_reason: str | None
    payload_sha256: str
    prev_hash: str
    entry_hash: str


class BoardHead(BaseModel):
    seq: int
    entry_hash: str


class OperatorReplyIn(BaseModel):
    message: MessageText
    status: Literal["open", "answered", "closed"] = "answered"


class ReportDecisionIn(BaseModel):
    status: ReportStatus
    note: OperatorNote = None


class HideIn(BaseModel):
    reason: HideReason
