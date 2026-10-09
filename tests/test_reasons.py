"""The taxonomy is a contract with the client: closed set, and every code actionable."""

import pytest

from certgen.reasons import REASON_CODES, REASONS, REQUIRED_FIELDS, validate

CLOSED_TAXONOMY = {
    "INVALID_NAME",
    "INVALID_EMAIL",
    "INVALID_DATE",
    "DUPLICATE_IN_BATCH",
    "MISSING_FIELD",
    "UNSUPPORTED_CHARACTERS",
    "RENDER_FAILED",
}


def good_payload():
    return {"name": "Ada Lovelace", "email": "ada@example.com", "course": "Analysis", "date": "2026-01-01"}


def code_of(payload, issued=frozenset()):
    found = validate(payload, issued)
    return found[0] if found else None


def test_taxonomy_is_closed():
    assert REASON_CODES == CLOSED_TAXONOMY


def test_every_code_is_repairable_and_described():
    for code, spec in REASONS.items():
        assert spec["repairable"] is True, code
        assert spec["msg"], code
        assert spec["points_at"] in (None, *REQUIRED_FIELDS, "email"), code


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda p: p.update(name=""), "INVALID_NAME"),
        (lambda p: p.update(name="---"), "INVALID_NAME"),
        (lambda p: p.update(name="   "), "INVALID_NAME"),
        (lambda p: p.update(email="not-an-email"), "INVALID_EMAIL"),
        (lambda p: p.update(email=""), "INVALID_EMAIL"),
        (lambda p: p.update(email=None), "INVALID_EMAIL"),
        (lambda p: p.update(email="a@b"), "INVALID_EMAIL"),
        (lambda p: p.pop("name"), "MISSING_FIELD"),
        (lambda p: p.pop("course"), "MISSING_FIELD"),
        (lambda p: p.pop("date"), "MISSING_FIELD"),
        (lambda p: p.update(course=""), "MISSING_FIELD"),
        (lambda p: p.update(date="   "), "MISSING_FIELD"),
        (lambda p: p.update(date="14/02/2026"), "INVALID_DATE"),
        (lambda p: p.update(date="2026-02-30"), "INVALID_DATE"),
        (lambda p: p.update(date="20260214"), "INVALID_DATE"),
        (lambda p: p.update(name="李雷"), "UNSUPPORTED_CHARACTERS"),
        (lambda p: p.update(name="Владимир"), "UNSUPPORTED_CHARACTERS"),
        (lambda p: p.update(course="Rust 🦀"), "UNSUPPORTED_CHARACTERS"),
        (lambda p: p.update(email="ada@example.com"), "DUPLICATE_IN_BATCH"),
        (lambda p: p.update(email="ADA@example.com"), "DUPLICATE_IN_BATCH"),
    ],
)
def test_each_code_is_reachable(mutate, expected):
    payload = good_payload()
    mutate(payload)
    assert code_of(payload, frozenset({"ada@example.com"})) == expected


@pytest.mark.parametrize("field", ["name", "course", "date"])
def test_missing_field_names_the_field(field):
    payload = good_payload()
    payload.pop(field)
    assert validate(payload, frozenset()) == ("MISSING_FIELD", f"{field} is required")


def test_unsupported_characters_names_the_field():
    payload = good_payload()
    payload["course"] = "物理"
    code, detail = validate(payload, frozenset())
    assert code == "UNSUPPORTED_CHARACTERS"
    assert detail.startswith("course")


def test_latin_accents_are_drawable():
    payload = good_payload()
    payload["name"] = "Zoë Ångström-Núñez"
    assert validate(payload, frozenset()) is None


def test_valid_payload_passes():
    assert validate(good_payload(), frozenset()) is None


def test_validate_does_not_mutate_the_payload():
    payload = good_payload()
    before = dict(payload)
    validate(payload, frozenset())
    assert payload == before


def test_unknown_fields_are_not_rejected():
    payload = good_payload()
    payload["grade"] = "A+"
    assert validate(payload, frozenset()) is None