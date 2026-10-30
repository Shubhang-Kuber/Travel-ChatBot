"""Password hashing contract: bcrypt cost 12, 8..72 UTF-8 bytes, no truncation, no NUL."""

from __future__ import annotations

import pytest

from backend.auth.passwords import hash_password, hash_password_async, verify_password, verify_password_async

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def stored() -> str:
    return hash_password("correct horse battery")


def test_round_trip(stored: str) -> None:
    assert verify_password("correct horse battery", stored) is True


def test_wrong_password_rejected(stored: str) -> None:
    assert verify_password("correct horse batterY", stored) is False
    assert verify_password("", stored) is False


def test_cost_factor_12_and_bcrypt_2b(stored: str) -> None:
    assert stored.startswith("$2b$12$")
    assert len(stored) == 60


def test_hash_is_salted() -> None:
    assert hash_password("same password!") != hash_password("same password!")


def test_hash_does_not_contain_plaintext(stored: str) -> None:
    assert "correct horse battery" not in stored


@pytest.mark.parametrize("pw", ["1234567", "", "ééé"])  # 7, 0, 6 bytes
def test_too_short_rejected(pw: str) -> None:
    with pytest.raises(ValueError):
        hash_password(pw)


def test_min_is_bytes_not_chars() -> None:
    # 4 characters, 8 UTF-8 bytes: accepted.
    pw = "éééé"
    assert len(pw) == 4 and len(pw.encode("utf-8")) == 8
    assert verify_password(pw, hash_password(pw)) is True


def test_exactly_72_bytes_accepted() -> None:
    pw = "a" * 72
    assert verify_password(pw, hash_password(pw)) is True


def test_73_bytes_rejected_not_truncated() -> None:
    with pytest.raises(ValueError):
        hash_password("a" * 73)


def test_72_byte_limit_counts_utf8_bytes() -> None:
    euro24 = "€" * 24  # 24 chars, 72 bytes
    euro25 = "€" * 25  # 25 chars, 75 bytes
    assert verify_password(euro24, hash_password(euro24)) is True
    with pytest.raises(ValueError):
        hash_password(euro25)


def test_overlong_candidate_does_not_match_its_72_byte_prefix() -> None:
    # bcrypt would silently truncate at 72 bytes; the contract says no truncation.
    prefix = "b" * 72
    stored = hash_password(prefix)
    assert verify_password(prefix + "X", stored) is False
    assert verify_password(prefix + "X" * 1000, stored) is False


def test_non_ascii_round_trip() -> None:
    pw = "pässwörd-旅行-\U0001f30d"
    stored = hash_password(pw)
    assert verify_password(pw, stored) is True
    assert verify_password("passwort-旅行-\U0001f30d", stored) is False


def test_unicode_normalization_forms_are_distinct_passwords() -> None:
    # No silent normalization: NFC and NFD spellings are different byte strings.
    nfc, nfd = "café-secret", "café-secret"
    assert verify_password(nfd, hash_password(nfc)) is False


def test_nul_rejected_at_hash_and_never_matches() -> None:
    with pytest.raises(ValueError):
        hash_password("abc\x00defgh")
    stored = hash_password("abcdefgh")
    assert verify_password("abcdefgh\x00", stored) is False


def test_lone_surrogate_rejected_and_never_raises() -> None:
    with pytest.raises(ValueError):
        hash_password("\ud800abcdefgh")
    assert verify_password("\ud800abcdefgh", hash_password("abcdefghij")) is False


def test_unknown_user_hash_none_is_false() -> None:
    assert verify_password("anything at all", None) is False


@pytest.mark.parametrize("bad", ["", "not-a-hash", "$2b$12$short", "$2b$12$" + "é" * 53])
def test_malformed_stored_hash_fails_closed(bad: str) -> None:
    assert verify_password("correct horse battery", bad) is False


async def test_async_wrappers_match_sync() -> None:
    stored = await hash_password_async("async password 1")
    assert stored.startswith("$2b$12$")
    assert await verify_password_async("async password 1", stored) is True
    assert await verify_password_async("async password 2", stored) is False
