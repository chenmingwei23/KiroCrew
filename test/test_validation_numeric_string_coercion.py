"""Defensive repair: an integer argument that reached a string-typed field as a
NUMBER is converted back to its string form; a float is NOT.

Some agent runtimes defer a tool's schema and, when they later marshal the
model's ``arguments``, re-type a top-level argument whose value looks like a
number into a JSON number — even when the loaded schema declared that field a
string. By the time the call reaches ``validate_field`` the type has already
been lost upstream, so a string-only ``FieldSpec`` sees an ``int``/``float``.

``validate_field`` repairs ONLY the ``int`` case (``str(42) == "42"`` is exact).
A ``float`` is left to fail loudly: ``str(float)`` is the shortest round-tripping
form, which is NOT the author's original text when a trailing/precision digit is
dropped (``"1790284307.156620"`` -> ``1790284307.15662`` -> ``"1790284307.15662"``,
a different but still-schema-valid value), so converting it would silently send a
Slack reply to the wrong thread. A loud ``expected str`` is strictly safer.
"""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    FieldSpec,
    ToolSchema,
    ValidationError,
    validate_field,
    validate_tool_args,
)

# A string-only field (the shape the deferred-tool bug mangles).
_STR_SPEC = FieldSpec("reply_to", str, max_len=64)


# ── Converted cases: an INTEGER reaching a string field becomes its string form ──


@pytest.mark.parametrize(
    "number, expected",
    [
        (42, "42"),
        (0, "0"),
        (7, "7"),
        # Large numeric ids round-trip exactly as ints.
        (1234567890123456789, "1234567890123456789"),
    ],
)
def test_integer_to_string_field_is_converted(number: int, expected: str) -> None:
    result = validate_field(number, _STR_SPEC)
    assert result == expected
    assert isinstance(result, str)


def test_integer_id_round_trips_through_validate_tool_args() -> None:
    """End-to-end: the whole-schema path converts an integer id back to a
    string so a string-typed field reaches its tool intact."""
    schema = ToolSchema(
        tool_name="post_message",
        fields=[FieldSpec("thread_id", str, max_len=64)],
    )
    cleaned = validate_tool_args({"thread_id": 1234567890123456789}, schema)
    assert cleaned["thread_id"] == "1234567890123456789"
    assert isinstance(cleaned["thread_id"], str)


def test_converted_value_still_runs_the_string_rules() -> None:
    """The repaired string is validated like any other: max_len still applies."""
    short = FieldSpec("code", str, max_len=4)
    assert validate_field(1234, short) == "1234"
    with pytest.raises(ValidationError):
        # 12345 -> "12345" is 5 chars, over the cap.
        validate_field(12345, short)


# ── A FLOAT on a string field is rejected, not silently converted ──


@pytest.mark.parametrize("number", [1790284307.156629, 1790284307.15662, 3.5, 3.0, 1000.0])
def test_float_on_a_string_field_is_rejected(number: float) -> None:
    """``str(float)`` is lossy relative to the author's original text, so a
    float is left to fail loudly rather than become a silently-wrong value."""
    with pytest.raises(ValidationError):
        validate_field(number, _STR_SPEC)


def test_non_finite_float_on_a_string_field_is_rejected() -> None:
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            validate_field(bad, _STR_SPEC)


# ── Untouched cases: everything that is NOT "an integer on a string-only field" ──


def test_real_string_on_string_field_is_unchanged() -> None:
    assert validate_field("1790284307.156629", _STR_SPEC) == "1790284307.156629"
    assert validate_field("hello", _STR_SPEC) == "hello"


def test_number_on_a_numeric_field_is_left_a_number() -> None:
    """A field that legitimately wants a number must keep getting one."""
    int_spec = FieldSpec("count", int, min_val=0, max_val=1000)
    assert validate_field(42, int_spec) == 42
    assert isinstance(validate_field(42, int_spec), int)

    num_spec = FieldSpec("ratio", (int, float))
    assert validate_field(3.5, num_spec) == 3.5
    assert isinstance(validate_field(3.5, num_spec), float)
    # A field that accepts BOTH str and number is not string-only, so a number
    # stays a number there too.
    either_spec = FieldSpec("mixed", (str, int))
    assert validate_field(7, either_spec) == 7
    assert isinstance(validate_field(7, either_spec), int)


def test_bool_on_a_string_field_is_still_rejected() -> None:
    """bool is an int subclass but is NOT a numeric id; it must not become
    ``"True"``/``"False"`` — it falls through to the type error."""
    with pytest.raises(ValidationError):
        validate_field(True, _STR_SPEC)
    with pytest.raises(ValidationError):
        validate_field(False, _STR_SPEC)


def test_non_scalar_on_a_string_field_is_unchanged() -> None:
    """A list / dict / None on a string field is not an int, so the repair
    leaves it for the normal checks (which reject the wrong-typed ones)."""
    with pytest.raises(ValidationError):
        validate_field([1, 2, 3], _STR_SPEC)
    with pytest.raises(ValidationError):
        validate_field({"a": 1}, _STR_SPEC)
    # None on an optional field returns the default, as before.
    assert validate_field(None, _STR_SPEC) is None


def test_number_inside_a_list_item_is_not_coerced() -> None:
    """The bug is top-level only; list-item string validation is a separate
    path and a number item there is still a type error, unchanged by this fix.
    (Mirrors the ticket note that array-nested values are not coerced.)"""
    list_spec = FieldSpec("ids", list, item_type=str, item_max_len=64)
    with pytest.raises(ValidationError):
        validate_field([42], list_spec)
