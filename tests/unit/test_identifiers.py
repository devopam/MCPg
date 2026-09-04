"""Adversarial tests for the shared SQL identifier quoting helper.

`quote_identifier` replaces the per-module `[A-Za-z_][A-Za-z0-9_]*`
validators that used to double as MCPg's injection defense. The bar here
is that value passed by callers can never break out of the quoted
identifier, while every name PostgreSQL supports through delimited quoting
still round-trips.
"""

import pytest

from mcpg.identifiers import (
    MAX_IDENTIFIER_BYTES,
    IdentifierError,
    ensure_identifier,
    quote_identifier,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Plain identifiers are quoted but otherwise unchanged.
        ("public", '"public"'),
        ("my_table", '"my_table"'),
        # The issue #329 case and its siblings — names that need delimited
        # quoting, all preserved literally and case-sensitively.
        ("adm-pgbench", '"adm-pgbench"'),
        ("My Schema", '"My Schema"'),
        ("MixedCase", '"MixedCase"'),
        ("naïve", '"naïve"'),
        # Reserved words are fine once quoted.
        ("select", '"select"'),
        ("order", '"order"'),
    ],
)
def test_quote_identifier_preserves_valid_names(name: str, expected: str) -> None:
    assert quote_identifier(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # A single embedded double quote is doubled.
        ('weird"name', '"weird""name"'),
        # The classic break-out attempt: doubling neutralises it, so the
        # whole thing stays one identifier rather than closing early.
        ('a";DROP TABLE t;--', '"a"";DROP TABLE t;--"'),
        # Several quotes, including adjacent ones.
        ('""', '""""""'),
        ('a"b"c', '"a""b""c"'),
    ],
)
def test_quote_identifier_escapes_embedded_quotes(name: str, expected: str) -> None:
    quoted = quote_identifier(name)
    assert quoted == expected
    # The escaped form has balanced, even quoting: stripping the outer pair
    # and collapsing doubled quotes recovers the original name exactly.
    inner = quoted[1:-1]
    assert inner.replace('""', "\x00").count('"') == 0
    assert inner.replace('""', '"') == name


@pytest.mark.parametrize("bad", ["", "\x00", "abc\x00def"])
def test_quote_identifier_rejects_empty_and_nul(bad: str) -> None:
    with pytest.raises(IdentifierError, match="invalid identifier name"):
        quote_identifier(bad)


def test_quote_identifier_rejects_overlong_name() -> None:
    too_long = "a" * (MAX_IDENTIFIER_BYTES + 1)
    with pytest.raises(IdentifierError, match="63-byte"):
        quote_identifier(too_long)
    # Exactly at the limit is fine.
    assert quote_identifier("a" * MAX_IDENTIFIER_BYTES) == '"' + "a" * MAX_IDENTIFIER_BYTES + '"'


def test_quote_identifier_counts_bytes_not_codepoints() -> None:
    # A 4-byte emoji: 16 of them = 64 bytes > 63, rejected even though it is
    # only 16 code points.
    assert len(("😀" * 16).encode("utf-8")) == 64
    with pytest.raises(IdentifierError, match="63-byte"):
        quote_identifier("😀" * 16)


def test_kind_appears_in_error_message() -> None:
    with pytest.raises(IdentifierError, match="invalid schema name"):
        quote_identifier("", "schema")
    with pytest.raises(IdentifierError, match="invalid column name"):
        ensure_identifier("x" * 100, "column")


def test_ensure_identifier_passes_names_through_unquoted() -> None:
    assert ensure_identifier("adm-pgbench") == "adm-pgbench"
    assert ensure_identifier("public") == "public"
