"""The rejection taxonomy: one closed dict, read by both the worker and the API.

A client reads a reason code and learns which input to fix: `points_at` names the field when
the code always means one field, and `reason_detail` names it when the code can mean several.
Every code is repairable by PATCHing that field.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

REASONS: dict[str, dict[str, Any]] = {
    "INVALID_NAME": {
        "repairable": True,
        "points_at": "name",
        "msg": "name is empty or contains no letters",
    },
    "INVALID_EMAIL": {
        "repairable": True,
        "points_at": "email",
        "msg": "email failed syntactic validation",
    },
    "INVALID_DATE": {
        "repairable": True,
        "points_at": "date",
        "msg": "date is not an ISO 8601 calendar date (YYYY-MM-DD)",
    },
    "DUPLICATE_IN_BATCH": {
        "repairable": True,
        "points_at": "email",
        "msg": "another recipient in this job was already issued this email",
    },
    "MISSING_FIELD": {
        "repairable": True,
        "points_at": None,
        "msg": "a field required by the template is absent or blank; see reason_detail",
    },
    "UNSUPPORTED_CHARACTERS": {
        "repairable": True,
        "points_at": None,
        "msg": "a field has characters the certificate font cannot draw; see reason_detail",
    },
    "RENDER_FAILED": {
        "repairable": True,
        "points_at": None,
        "msg": "renderer raised; see reason_detail",
    },
}

REASON_CODES = frozenset(REASONS)

# The single template's fields. An absent or blank one is MISSING_FIELD; a present but
# unusable one gets the specific code. Adding a field to the certificate starts here.
REQUIRED_FIELDS = ("name", "course", "date")

# The fields drawn on the certificate.
DRAWN_FIELDS = ("name", "course", "date")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _drawable(text: str) -> bool:
    # Note: the template uses the PDF base-14 Helvetica, whose glyphs cover cp1252
    # (Latin-1 plus a few extras). Anything outside it would print as black boxes, so it is
    # rejected rather than issued wrong. Embed a Unicode TTF in render.py to widen this.
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _is_calendar_date(text: str) -> bool:
    # fromisoformat alone also takes 20260214 and week dates; the regex pins YYYY-MM-DD and
    # fromisoformat rejects impossible days like 2026-02-30.
    if not _DATE_RE.fullmatch(text):
        return False
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def validate(
    payload: dict[str, Any], issued_emails: frozenset[str] | set[str]
) -> tuple[str, str | None] | None:
    """Return `(reason_code, reason_detail)` for this payload, or None when it can be issued.

    `issued_emails` holds the lowercased emails already issued in this job, so the first
    valid occurrence of an email issues and later ones reject.
    """
    for field in REQUIRED_FIELDS:
        value = payload.get(field)
        # A blank name gets its own, more specific code below.
        if value is None or (field != "name" and not _present(value)):
            return "MISSING_FIELD", f"{field} is required"

    if not any(ch.isalpha() for ch in str(payload["name"])):
        return "INVALID_NAME", None

    for field in DRAWN_FIELDS:
        if not _drawable(str(payload[field])):
            return "UNSUPPORTED_CHARACTERS", f"{field} has characters outside Latin-1"

    if not _is_calendar_date(str(payload["date"]).strip()):
        return "INVALID_DATE", None

    email = str(payload.get("email") or "").strip()
    if not _EMAIL_RE.fullmatch(email):
        return "INVALID_EMAIL", None

    if email.lower() in issued_emails:
        return "DUPLICATE_IN_BATCH", None

    return None