"""Handle rules: who may use which name (docs/decisions/0010-handle-registry.md)."""

from __future__ import annotations

import re

OPERATOR_HANDLE = "operator"

# Letters, digits and a few separators only, so that look-alike characters from other scripts
# cannot be used to imitate a registered handle.
HANDLE_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._ -]{0,62}[A-Za-z0-9])?$"

# Characters that are easily mistaken for one another collapse to one form.
_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "i": "l", "3": "e", "5": "s", "8": "b"})
_SEPARATORS = re.compile(r"[._ -]+")

# A handle whose skeleton contains one of these is reserved for the operator.
_RESERVED_PARTS = ("operator", "admin", "agenthelper", "moderator", "official", "verified")
# A handle whose skeleton equals one of these is reserved.
_RESERVED_EXACT = ("system", "root", "staff", "support", "owner", "service", "anonymous")


def skeleton(handle: str) -> str:
    """The form under which a handle is registered. Two handles with the same skeleton are the same handle."""
    s = _SEPARATORS.sub("", handle.lower()).translate(_LOOKALIKES)
    return s.replace("rn", "m").replace("vv", "w")


_RESERVED_PART_SKELETONS = tuple(skeleton(p) for p in _RESERVED_PARTS)
_RESERVED_EXACT_SKELETONS = frozenset(skeleton(p) for p in _RESERVED_EXACT)


def is_reserved(handle: str) -> bool:
    s = skeleton(handle)
    return s in _RESERVED_EXACT_SKELETONS or any(p in s for p in _RESERVED_PART_SKELETONS)
