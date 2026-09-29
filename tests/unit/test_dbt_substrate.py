"""The dbt artifact substrate - loading, validating, and indexing a static,
committed fixture (P1.2).

Mirrors `test_artifact_substrate.py`'s discipline for the Airflow substrate:
determinism first, then the expected shape, then rejection of malformed
input.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from causiq.dbt_substrate import (
    DEFAULT_DBT_FIXTURE_PATH,
    DbtFixture,
    DbtModelIndex,
    load_dbt_fixture,
)
from causiq.errors import ConfigurationError

pytestmark = pytest.mark.unit


def _digest_of(fixture: DbtFixture) -> str:
    return hashlib.sha256(fixture.model_dump_json().encode("utf-8")).hexdigest()


def _valid_model(unique_id: str = "model.pkg.m") -> dict[str, object]:
    return {
        "unique_id": unique_id,
        "name": "m",
        "results": [
            {
                "invocation_id": "inv_1",
                "generated_at": "2026-09-01T00:00:00+00:00",
                "status": "success",
                "execution_time": 1.0,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Deterministic loading (I6)
# --------------------------------------------------------------------------- #
def test_valid_fixture_loads() -> None:
    fixture = load_dbt_fixture()
    assert isinstance(fixture, DbtFixture)
    assert len(fixture.models) >= 1
    unique_ids = {model.unique_id for model in fixture.models}
    assert "model.revenue_analytics.daily_revenue_pipeline" in unique_ids


def test_loading_twice_produces_byte_identical_content() -> None:
    """No RANDOM()/NOW()/UUID()-equivalent anywhere in this path - the
    fixture is a static file, so two independent loads must be identical."""
    first = load_dbt_fixture()
    second = load_dbt_fixture()
    assert first == second
    assert _digest_of(first) == _digest_of(second)


def test_default_fixture_path_points_at_a_real_committed_file() -> None:
    assert DEFAULT_DBT_FIXTURE_PATH.is_file()
    assert DEFAULT_DBT_FIXTURE_PATH.name == "run_results.json"


def test_loading_does_not_write_or_modify_anything(tmp_path: Path) -> None:
    """Unlike `build_warehouse_db`, there is no build step here - loading is
    a pure read. Copy the real fixture into an isolated directory and prove
    its mtime and byte content are untouched by a load."""
    copy_path = tmp_path / "run_results.json"
    copy_path.write_bytes(DEFAULT_DBT_FIXTURE_PATH.read_bytes())
    before_mtime = copy_path.stat().st_mtime_ns
    before_bytes = copy_path.read_bytes()

    load_dbt_fixture(copy_path)
    load_dbt_fixture(copy_path)

    assert copy_path.stat().st_mtime_ns == before_mtime
    assert copy_path.read_bytes() == before_bytes


# --------------------------------------------------------------------------- #
# Rejection of malformed input
# --------------------------------------------------------------------------- #
def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_dbt_fixture(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not read"):
        load_dbt_fixture(tmp_path / "does_not_exist.json")


def test_wrong_top_level_shape_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wrong_shape.json"
    path.write_text(json.dumps({"not_models": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="does not match the expected structure"):
        load_dbt_fixture(path)


def test_empty_models_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"models": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


def test_model_with_no_results_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no_results.json"
    path.write_text(
        json.dumps({"models": [{"unique_id": "model.pkg.m", "name": "m", "results": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    model = _valid_model()
    model["results"][0]["generated_at"] = "2026-09-01T00:00:00"  # type: ignore[index]
    path = tmp_path / "naive.json"
    path.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


def test_unknown_status_is_rejected(tmp_path: Path) -> None:
    model = _valid_model()
    model["results"][0]["status"] = "not_a_real_status"  # type: ignore[index]
    path = tmp_path / "bad_status.json"
    path.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


def test_negative_execution_time_is_rejected(tmp_path: Path) -> None:
    model = _valid_model()
    model["results"][0]["execution_time"] = -1.0  # type: ignore[index]
    path = tmp_path / "bad_time.json"
    path.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


def test_duplicate_unique_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        json.dumps({"models": [_valid_model("model.pkg.dup"), _valid_model("model.pkg.dup")]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate unique_id"):
        load_dbt_fixture(path)


def test_invalid_model_record_is_rejected(tmp_path: Path) -> None:
    """A model missing a required field (`name`) is an invalid record, not
    just an invalid value inside a valid shape."""
    path = tmp_path / "invalid_record.json"
    path.write_text(
        json.dumps({"models": [{"unique_id": "model.pkg.m", "results": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_dbt_fixture(path)


# --------------------------------------------------------------------------- #
# The in-memory index
# --------------------------------------------------------------------------- #
def test_index_looks_up_a_known_model() -> None:
    index = DbtModelIndex.load()
    model = index.get("model.revenue_analytics.daily_revenue_pipeline")
    assert model is not None
    assert model.unique_id == "model.revenue_analytics.daily_revenue_pipeline"
    assert len(model.results) >= 1


def test_index_returns_none_for_an_unknown_model_rather_than_raising() -> None:
    index = DbtModelIndex.load()
    assert index.get("model.pkg.no_such_model") is None


def test_index_reports_every_known_unique_id() -> None:
    index = DbtModelIndex.load()
    ids = index.unique_ids()
    assert "model.revenue_analytics.daily_revenue_pipeline" in ids
    assert "model.revenue_analytics.customer_ltv" in ids
    assert "model.revenue_analytics.stg_orders" in ids
