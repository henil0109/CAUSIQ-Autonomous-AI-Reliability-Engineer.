"""The data-quality artifact substrate - loading, validating, and indexing a
static, committed fixture (P1.3).

Mirrors `test_dbt_substrate.py`'s discipline: determinism first, then the
expected shape, then rejection of malformed input.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from causiq.dq_substrate import (
    DEFAULT_DQ_FIXTURE_PATH,
    DqCheckIndex,
    DqFixture,
    load_dq_fixture,
)
from causiq.errors import ConfigurationError

pytestmark = pytest.mark.unit


def _digest_of(fixture: DqFixture) -> str:
    return hashlib.sha256(fixture.model_dump_json().encode("utf-8")).hexdigest()


def _valid_check(check_name: str = "check.example") -> dict[str, object]:
    return {
        "check_name": check_name,
        "dataset": "analytics.some_table",
        "results": [
            {
                "checked_at": "2026-09-01T00:00:00+00:00",
                "status": "pass",
                "measured_freshness_minutes": 10,
                "threshold_minutes": 60,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Deterministic loading (I6)
# --------------------------------------------------------------------------- #
def test_valid_fixture_loads() -> None:
    fixture = load_dq_fixture()
    assert isinstance(fixture, DqFixture)
    assert len(fixture.checks) >= 1
    names = {check.check_name for check in fixture.checks}
    assert "revenue_daily_freshness" in names


def test_loading_twice_produces_byte_identical_content() -> None:
    """No RANDOM()/NOW()/UUID()-equivalent anywhere in this path - the
    fixture is a static file, so two independent loads must be identical."""
    first = load_dq_fixture()
    second = load_dq_fixture()
    assert first == second
    assert _digest_of(first) == _digest_of(second)


def test_default_fixture_path_points_at_a_real_committed_file() -> None:
    assert DEFAULT_DQ_FIXTURE_PATH.is_file()
    assert DEFAULT_DQ_FIXTURE_PATH.name == "check_results.json"


def test_loading_does_not_write_or_modify_anything(tmp_path: Path) -> None:
    """Unlike `build_warehouse_db`, there is no build step here - loading is
    a pure read. Copy the real fixture into an isolated directory and prove
    its mtime and byte content are untouched by a load."""
    copy_path = tmp_path / "check_results.json"
    copy_path.write_bytes(DEFAULT_DQ_FIXTURE_PATH.read_bytes())
    before_mtime = copy_path.stat().st_mtime_ns
    before_bytes = copy_path.read_bytes()

    load_dq_fixture(copy_path)
    load_dq_fixture(copy_path)

    assert copy_path.stat().st_mtime_ns == before_mtime
    assert copy_path.read_bytes() == before_bytes


# --------------------------------------------------------------------------- #
# Rejection of malformed input
# --------------------------------------------------------------------------- #
def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_dq_fixture(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not read"):
        load_dq_fixture(tmp_path / "does_not_exist.json")


def test_wrong_top_level_shape_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wrong_shape.json"
    path.write_text(json.dumps({"not_checks": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="does not match the expected structure"):
        load_dq_fixture(path)


def test_empty_checks_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"checks": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_check_with_no_results_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no_results.json"
    path.write_text(
        json.dumps({"checks": [{"check_name": "c", "dataset": "d", "results": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    check = _valid_check()
    check["results"][0]["checked_at"] = "2026-09-01T00:00:00"  # type: ignore[index]
    path = tmp_path / "naive.json"
    path.write_text(json.dumps({"checks": [check]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_unknown_status_is_rejected(tmp_path: Path) -> None:
    check = _valid_check()
    check["results"][0]["status"] = "warn"  # type: ignore[index]
    path = tmp_path / "bad_status.json"
    path.write_text(json.dumps({"checks": [check]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_negative_measured_freshness_is_rejected(tmp_path: Path) -> None:
    check = _valid_check()
    check["results"][0]["measured_freshness_minutes"] = -1  # type: ignore[index]
    path = tmp_path / "bad_measured.json"
    path.write_text(json.dumps({"checks": [check]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_zero_threshold_is_rejected(tmp_path: Path) -> None:
    check = _valid_check()
    check["results"][0]["threshold_minutes"] = 0  # type: ignore[index]
    path = tmp_path / "bad_threshold.json"
    path.write_text(json.dumps({"checks": [check]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


def test_duplicate_check_name_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        json.dumps({"checks": [_valid_check("dup"), _valid_check("dup")]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate check_name"):
        load_dq_fixture(path)


def test_invalid_check_record_is_rejected(tmp_path: Path) -> None:
    """A check missing a required field (`dataset`) is an invalid record,
    not just an invalid value inside a valid shape."""
    path = tmp_path / "invalid_record.json"
    path.write_text(
        json.dumps({"checks": [{"check_name": "c", "results": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_dq_fixture(path)


# --------------------------------------------------------------------------- #
# The in-memory index
# --------------------------------------------------------------------------- #
def test_index_looks_up_a_known_check() -> None:
    index = DqCheckIndex.load()
    check = index.get("revenue_daily_freshness")
    assert check is not None
    assert check.check_name == "revenue_daily_freshness"
    assert len(check.results) >= 1


def test_index_returns_none_for_an_unknown_check_rather_than_raising() -> None:
    index = DqCheckIndex.load()
    assert index.get("no_such_check") is None


def test_index_reports_every_known_check_name() -> None:
    index = DqCheckIndex.load()
    names = index.check_names()
    assert "revenue_daily_freshness" in names
    assert "customer_ltv_freshness" in names
