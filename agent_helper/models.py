"""Request and response bodies. Every text field has a hard length limit."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from .handles import HANDLE_PATTERN

LIMITS = {
    "message": 8000,
    "handle": 64,
    "handle_token": 128,
    "contact_hint": 500,
    "board_content": 4000,
    "board_topic": 100,
    "report_text": 8000,
    "operator_note": 2000,
}

# C0/C1 controls except tab, newline and carriage return; lone surrogates (not encodable as UTF-8);
# bidi overrides and invisible characters that make text look different from what it is.
# Zero-width (non-)joiners stay allowed: emoji sequences and several scripts need them.
_FORBIDDEN_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff"
    r"؜​‎‏‪-‮⁠⁦-⁩﻿]"
)


def _no_control_chars(value: str | None) -> str | None:
    if value is not None and _FORBIDDEN_CHARS.search(value):
        raise ValueError("control, bidi-override, invisible, and surrogate characters are not allowed")
    return value


def _text(max_length: int) -> object:
    return Annotated[str, Field(min_length=1, max_length=max_length), AfterValidator(_no_control_chars)]


def _optional(max_length: int) -> object:
    return Annotated[str | None, Field(max_length=max_length), AfterValidator(_no_control_chars)]


MessageText = _text(LIMITS["message"])
Handle = Annotated[str | None, Field(max_length=LIMITS["handle"], pattern=HANDLE_PATTERN)]
HandleToken = Annotated[str | None, Field(max_length=LIMITS["handle_token"])]
ContactHint = _optional(LIMITS["contact_hint"])
ReportText = _text(LIMITS["report_text"])
BoardContent = _text(LIMITS["board_content"])
BoardTopic = _optional(LIMITS["board_topic"])
OperatorNote = _optional(LIMITS["operator_note"])
HideReason = _text(LIMITS["operator_note"])


class RequestIn(BaseModel):
    message: MessageText
    handle: Handle = None
    handle_token: HandleToken = None
    contact_hint: ContactHint = None


class MessageIn(BaseModel):
    message: MessageText


class Created(BaseModel):
    id: str
    follow_up_token: str
    status_url: str
    note: str
    handle_token: str | None = None


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
    handle_token: HandleToken = None
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


RequestStatus = Literal["open", "answered", "closed"]


RequestStatus = Literal["open", "answered", "closed"]


class OperatorReplyIn(BaseModel):
    message: MessageText
    status: RequestStatus = "answered"


class ReportDecisionIn(BaseModel):
    status: ReportStatus
    note: OperatorNote = None


class OperatorBoardIn(BaseModel):
    content: BoardContent
    topic: BoardTopic = None


class HideIn(BaseModel):
    reason: HideReason


# --- agent directory and mailboxes (docs/decisions/0013) ---------------------------------------------

LIMITS |= {
    "profile_summary": 1000,
    "profile_item": 200,
    "profile_items": 20,
    "tag": 40,
    "contact_value": 300,
    "contacts": 10,
    "mail_subject": 200,
}

ProfileSummary = _text(LIMITS["profile_summary"])
ProfileItem = _text(LIMITS["profile_item"])
Tag = Annotated[str, Field(min_length=1, max_length=LIMITS["tag"], pattern=r"^[a-z0-9][a-z0-9-]*$")]
RequiredHandle = Annotated[str, Field(max_length=LIMITS["handle"], pattern=HANDLE_PATTERN)]
ContactKind = Literal["url", "http_api", "mcp", "a2a", "email", "other"]
MailKind = Literal["message", "handoff", "referral"]


class Contact(BaseModel):
    kind: ContactKind
    value: _text(LIMITS["contact_value"])  # type: ignore[valid-type]


class ProfileIn(BaseModel):
    summary: ProfileSummary  # type: ignore[valid-type]
    offers: list[ProfileItem] = Field(default_factory=list, max_length=LIMITS["profile_items"])  # type: ignore[valid-type]
    needs: list[ProfileItem] = Field(default_factory=list, max_length=LIMITS["profile_items"])  # type: ignore[valid-type]
    tags: list[Tag] = Field(default_factory=list, max_length=LIMITS["profile_items"])
    contact: list[Contact] = Field(default_factory=list, max_length=LIMITS["contacts"])
    accepts_messages: bool = True
    handle_token: HandleToken = None


class ProfileOut(BaseModel):
    handle: str
    summary: str
    offers: list[str]
    needs: list[str]
    tags: list[str]
    contact: list[Contact]
    accepts_messages: bool
    created_at: str
    updated_at: str


class MailIn(BaseModel):
    sender: RequiredHandle
    to: RequiredHandle
    message: MessageText  # type: ignore[valid-type]
    subject: _optional(LIMITS["mail_subject"]) = None  # type: ignore[valid-type]
    kind: MailKind = "message"
    in_reply_to: Annotated[int | None, Field(ge=1)] = None
    handle_token: HandleToken = None


class MailOut(BaseModel):
    id: int
    created_at: str
    sender: str
    to: str
    kind: MailKind
    subject: str | None
    body: str
    in_reply_to: int | None


class ReferralIn(BaseModel):
    to: RequiredHandle
    note: MessageText  # type: ignore[valid-type]
    include_request_text: bool = False
