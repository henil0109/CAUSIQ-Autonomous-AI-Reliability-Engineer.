"""Clock and identifier behaviour - the determinism foundations (invariant I6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from causiq.clock import Clock, FrozenClock, SystemClock
from causiq.ids import (
    FixedIdGenerator,
    IdGenerator,
    Uuid4IdGenerator,
    format_evidence_id,
    format_hypothesis_id,
    is_evidence_id,
)
from tests.conftest import T0

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
def test_both_implementations_satisfy_the_port() -> None:
    """ADR-0004: a port with only one implementation is a smell."""
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FrozenClock(), Clock)


def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC


def test_system_clock_monotonic_moves_forward() -> None:
    clock = SystemClock()
    assert clock.monotonic() <= clock.monotonic()


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(T0)
    assert clock.now() == T0
    assert clock.now() == T0
    assert clock.monotonic() == 0.0


def test_frozen_clock_advances_both_scales() -> None:
    clock = FrozenClock(T0)
    clock.advance(timedelta(seconds=90))
    assert clock.now() == T0 + timedelta(seconds=90)
    assert clock.monotonic() == pytest.approx(90.0)


def test_ticking_clock_gives_ordered_timestamps() -> None:
    clock = FrozenClock(T0, step=timedelta(seconds=1))
    stamps = [clock.now() for _ in range(3)]
    assert stamps == [T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)]


def test_frozen_clock_normalises_to_utc() -> None:
    from datetime import timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    clock = FrozenClock(datetime(2026, 9, 7, 17, 30, tzinfo=ist))
    assert clock.now() == datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_frozen_clock_rejects_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 9, 7))  # noqa: DTZ001 - the point of the test


def test_frozen_clock_has_a_default_start() -> None:
    assert FrozenClock().now().tzinfo is UTC


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #
def test_evidence_ids_are_zero_padded_and_sort_in_collection_order() -> None:
    ids = [format_evidence_id(n) for n in (1, 2, 10)]
    assert ids == ["ev_001", "ev_002", "ev_010"]
    assert sorted(ids) == ids


def test_evidence_id_sequence_must_be_positive() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        format_evidence_id(0)


def test_hypothesis_ids_follow_the_same_shape() -> None:
    assert format_hypothesis_id(3) == "hyp_003"
    with pytest.raises(ValueError, match=">= 1"):
        format_hypothesis_id(-1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ev_001", True),
        ("ev_1", True),
        ("ev_", False),
        ("ev_abc", False),
        ("run_001", False),
        ("", False),
    ],
)
def test_evidence_id_shape_check(value: str, expected: bool) -> None:
    """A shape check only - resolution against a ledger is what actually matters."""
    assert is_evidence_id(value) is expected


def test_both_generators_satisfy_the_port() -> None:
    assert isinstance(Uuid4IdGenerator(), IdGenerator)
    assert isinstance(FixedIdGenerator(), IdGenerator)


def test_uuid_generator_produces_distinct_prefixed_ids() -> None:
    generator = Uuid4IdGenerator()
    first, second = generator.new_run_id(), generator.new_run_id()
    assert first != second
    assert first.startswith("run_")


def test_fixed_generator_is_deterministic() -> None:
    """Two fresh generators produce the same sequence - the basis of AC-10."""
    a = FixedIdGenerator()
    b = FixedIdGenerator()
    assert [a.new_run_id() for _ in range(3)] == ["run_0001", "run_0002", "run_0003"]
    assert [b.new_run_id() for _ in range(3)] == ["run_0001", "run_0002", "run_0003"]


def test_fixed_generator_start_is_configurable() -> None:
    generator = FixedIdGenerator(start=7)
    assert generator.new_run_id() == "run_0007"
