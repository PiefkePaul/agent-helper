"""The capability catalog (docs/decisions/0014).

Entries come from two places: the catalog file shipped with the code (or `CAPABILITIES_FILE`), and entries
the operator adds or overrides at run time through the admin API, kept in the database. A database entry
replaces a file entry with the same id.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .models import Tag, _optional, _text

log = logging.getLogger("agent_helper.catalog")

Availability = Literal["available", "on_request", "human_in_the_loop", "planned", "not_available"]
Category = Literal["information", "tool", "compute", "service", "human", "physical", "communication", "meta"]
AccessKind = Literal["http", "mcp_tool", "request", "url"]
CapabilityId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")]

AVAILABILITY_MEANING = {
    "available": "Works now, automatically, without a human.",
    "human_in_the_loop": "Works now; a human operator reads and acts, with no guaranteed response time.",
    "on_request": "Possible case by case. Ask with a request; nothing is promised in advance.",
    "planned": "Not available yet; intended to be built. Vote on the matching capability request to raise it.",
    "not_available": "Not offered. You may still ask for it as a capability request.",
}


class Access(BaseModel):
    kind: AccessKind
    value: _text(300)  # type: ignore[valid-type]


class CapabilityIn(BaseModel):
    title: _text(200)  # type: ignore[valid-type]
    summary: _text(1000)  # type: ignore[valid-type]
    category: Category
    availability: Availability
    access: list[Access] = Field(default_factory=list, max_length=10)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    response_time: _optional(200) = None  # type: ignore[valid-type]
    cost: _optional(200) = "free"  # type: ignore[valid-type]
    limits: _optional(1000) = None  # type: ignore[valid-type]


class Capability(CapabilityIn):
    id: CapabilityId
    source: Literal["catalog", "operator"] = "catalog"


def load_file_entries(settings: Settings) -> list[Capability]:
    if settings.capabilities_file:
        raw = json.loads(Path(settings.capabilities_file).read_text(encoding="utf-8"))
    else:
        raw = json.loads(resources.files("agent_helper").joinpath("capabilities.json").read_text(encoding="utf-8"))
    entries = []
    for item in raw.get("capabilities", []):
        try:
            entries.append(Capability.model_validate(item))
        except ValidationError as exc:
            log.warning("skipping invalid catalog entry %r: %d problem(s)", item.get("id"), exc.error_count())
    return entries


def _matches(
    entry: Capability, words: list[str], category: str | None, availability: str | None, tag: str | None
) -> bool:
    if category and entry.category != category:
        return False
    if availability and entry.availability != availability:
        return False
    if tag and tag not in entry.tags:
        return False
    text = " ".join([entry.id, entry.title, entry.summary, *entry.tags]).lower()
    return all(w in text for w in words)


class Catalog:
    def __init__(self, file_entries: list[Capability], store: Any) -> None:
        self.file_entries = file_entries
        self.store = store

    def entries(self) -> list[Capability]:
        merged = {e.id: e for e in self.file_entries}
        for item in self.store.list_operator_capabilities():
            merged[item["id"]] = Capability.model_validate(item | {"source": "operator"})
        return list(merged.values())

    def get(self, cap_id: str) -> Capability | None:
        return next((e for e in self.entries() if e.id == cap_id), None)

    def search(
        self,
        query: str | None = None,
        category: str | None = None,
        availability: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        words = (query or "").lower().split()[:8]
        found = [e.model_dump() for e in self.entries() if _matches(e, words, category, availability, tag)]
        return {
            "note": "What this service can do, honestly labelled. Missing something? Ask for it: "
            "POST /v1/capability-requests (or vote on an existing request).",
            "availability_meaning": AVAILABILITY_MEANING,
            "capabilities": found,
        }
