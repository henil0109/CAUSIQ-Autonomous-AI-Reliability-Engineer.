"""The Airflow artifact substrate - loading, validating, and indexing a
static, committed fixture (P1.1).

`load_airflow_fixture`/`AirflowDagIndex` are the artifact-fixture
counterpart to `causiq.evidence_substrate`'s DuckDB build path, tested here
the same way `test_evidence_substrate.py` tests that one: determinism first,
then the expected shape.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from causiq.artifact_substrate import (
    DEFAULT_AIRFLOW_FIXTURE_PATH,
    AirflowDagIndex,
    AirflowFixture,
    load_airflow_fixture,
)
from causiq.errors import ConfigurationError

pytestmark = pytest.mark.unit


def _digest_of(fixture: AirflowFixture) -> str:
    return hashlib.sha256(fixture.model_dump_json().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic loading (I6)
# --------------------------------------------------------------------------- #
def test_valid_fixture_loads() -> None:
    fixture = load_airflow_fixture()
    assert isinstance(fixture, AirflowFixture)
    assert len(fixture.dags) >= 1
    dag_ids = {dag.dag_id for dag in fixture.dags}
    assert "daily_revenue_pipeline" in dag_ids


def test_loading_twice_produces_byte_identical_content() -> None:
    """No RANDOM()/NOW()/UUID()-equivalent anywhere in this path - the
    fixture is a static file, so two independent loads must be identical."""
    first = load_airflow_fixture()
    second = load_airflow_fixture()
    assert first == second
    assert _digest_of(first) == _digest_of(second)


def test_default_fixture_path_points_at_a_real_committed_file() -> None:
    assert DEFAULT_AIRFLOW_FIXTURE_PATH.is_file()
    assert DEFAULT_AIRFLOW_FIXTURE_PATH.name == "dag_runs.json"


def test_loading_does_not_write_or_modify_anything(tmp_path: Path) -> None:
    """Unlike `build_warehouse_db`, there is no build step here - loading is
    a pure read. Copy the real fixture into an isolated directory and prove
    its mtime and byte content are untouched by a load."""
    copy_path = tmp_path / "dag_runs.json"
    copy_path.write_bytes(DEFAULT_AIRFLOW_FIXTURE_PATH.read_bytes())
    before_mtime = copy_path.stat().st_mtime_ns
    before_bytes = copy_path.read_bytes()

    load_airflow_fixture(copy_path)
    load_airflow_fixture(copy_path)

    assert copy_path.stat().st_mtime_ns == before_mtime
    assert copy_path.read_bytes() == before_bytes


# --------------------------------------------------------------------------- #
# Rejection of malformed input
# --------------------------------------------------------------------------- #
def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_airflow_fixture(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not read"):
        load_airflow_fixture(tmp_path / "does_not_exist.json")


def test_wrong_top_level_shape_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wrong_shape.json"
    path.write_text(json.dumps({"not_dags": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="does not match the expected structure"):
        load_airflow_fixture(path)


def test_empty_dags_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"dags": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_airflow_fixture(path)


def test_dag_run_with_no_tasks_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no_tasks.json"
    path.write_text(
        json.dumps(
            {
                "dags": [
                    {
                        "dag_id": "d",
                        "runs": [
                            {
                                "dag_run_id": "d__1",
                                "execution_date": "2026-09-01T00:00:00+00:00",
                                "start_date": "2026-09-01T00:00:00+00:00",
                                "end_date": "2026-09-01T00:05:00+00:00",
                                "state": "success",
                                "tasks": [],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_airflow_fixture(path)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "naive.json"
    path.write_text(
        json.dumps(
            {
                "dags": [
                    {
                        "dag_id": "d",
                        "runs": [
                            {
                                "dag_run_id": "d__1",
                                "execution_date": "2026-09-01T00:00:00",
                                "start_date": "2026-09-01T00:00:00+00:00",
                                "end_date": "2026-09-01T00:05:00+00:00",
                                "state": "success",
                                "tasks": [
                                    {
                                        "task_id": "t",
                                        "state": "success",
                                        "start_date": "2026-09-01T00:00:00+00:00",
                                        "end_date": "2026-09-01T00:05:00+00:00",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_airflow_fixture(path)


def test_unknown_task_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad_state.json"
    path.write_text(
        json.dumps(
            {
                "dags": [
                    {
                        "dag_id": "d",
                        "runs": [
                            {
                                "dag_run_id": "d__1",
                                "execution_date": "2026-09-01T00:00:00+00:00",
                                "start_date": "2026-09-01T00:00:00+00:00",
                                "end_date": "2026-09-01T00:05:00+00:00",
                                "state": "not_a_real_state",
                                "tasks": [
                                    {
                                        "task_id": "t",
                                        "state": "success",
                                        "start_date": "2026-09-01T00:00:00+00:00",
                                        "end_date": "2026-09-01T00:05:00+00:00",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_airflow_fixture(path)


def test_duplicate_dag_id_is_rejected(tmp_path: Path) -> None:
    one_run = {
        "dag_run_id": "d__1",
        "execution_date": "2026-09-01T00:00:00+00:00",
        "start_date": "2026-09-01T00:00:00+00:00",
        "end_date": "2026-09-01T00:05:00+00:00",
        "state": "success",
        "tasks": [
            {
                "task_id": "t",
                "state": "success",
                "start_date": "2026-09-01T00:00:00+00:00",
                "end_date": "2026-09-01T00:05:00+00:00",
            }
        ],
    }
    path = tmp_path / "duplicate.json"
    path.write_text(
        json.dumps(
            {"dags": [{"dag_id": "d", "runs": [one_run]}, {"dag_id": "d", "runs": [one_run]}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate dag_id"):
        load_airflow_fixture(path)


# --------------------------------------------------------------------------- #
# The in-memory index
# --------------------------------------------------------------------------- #
def test_index_looks_up_a_known_dag() -> None:
    index = AirflowDagIndex.load()
    dag = index.get("daily_revenue_pipeline")
    assert dag is not None
    assert dag.dag_id == "daily_revenue_pipeline"
    assert len(dag.runs) >= 1


def test_index_returns_none_for_an_unknown_dag_rather_than_raising() -> None:
    index = AirflowDagIndex.load()
    assert index.get("no_such_dag") is None


def test_index_reports_every_known_dag_id() -> None:
    index = AirflowDagIndex.load()
    ids = index.dag_ids()
    assert "daily_revenue_pipeline" in ids
    assert "customer_ltv_pipeline" in ids
