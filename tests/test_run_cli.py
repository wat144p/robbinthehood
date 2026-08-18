"""
run.py's crash safety.

Between `store.start_run()` and `store.finish_run()`, an ordinary run has no
exception handling at all — if anything in that stretch raises, or if the
process gets killed by GitHub Actions' timeout mid-flight, the run's database
row is left with `finished_at = NULL` forever, indistinguishable from "still
running" and with no clue what happened. This is exactly what happened on this
project's first real scheduled run.

Nothing here can catch an external SIGKILL (nothing in Python can — that is a
genuine limitation, not something to test around). What it can and must do is
catch an ordinary Python exception, record it in the run's own notes, and
still propagate the failure so CI shows the run as failed rather than green.
"""

from __future__ import annotations

import sqlite3

import pytest

import run as run_module


@pytest.fixture
def crash_db(tmp_path):
    return str(tmp_path / "crash_test.db")


@pytest.fixture(autouse=True)
def no_real_discovery(monkeypatch):
    """These tests run against the real config.yaml (no --config override),
    which points source discovery at the real discovered_sources.yaml. Since
    discovery isn't what's under test here, skip it — otherwise every test
    run bumps that file's timestamp and dirties the working tree for no
    reason."""
    monkeypatch.setattr(run_module.SourceDiscovery, "should_run", lambda self, **kw: False)


def run_row(db_path: str) -> sqlite3.Row:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id, finished_at, notes FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    connection.close()
    return row


class TestCrashSafety:
    def test_a_crash_is_recorded_not_left_silently_incomplete(
        self, crash_db, monkeypatch
    ):
        """The bug this guards against: a run that started but never finished
        must say WHY in the database, not stay NULL forever."""

        def exploding_evaluate_all(*args, **kwargs):
            raise RuntimeError("simulated crash for testing")

        monkeypatch.setattr(run_module, "evaluate_all", exploding_evaluate_all)

        with pytest.raises(RuntimeError, match="simulated crash"):
            run_module.main([
                "--once", "--dry-run", "--offline", "--source", "rss",
                "--db", crash_db,
            ])

        row = run_row(crash_db)
        assert row["finished_at"] is not None
        assert "CRASHED" in row["notes"]
        assert "RuntimeError" in row["notes"]

    def test_the_crash_still_propagates_so_ci_sees_a_failure(
        self, crash_db, monkeypatch
    ):
        """Recording the crash must never swallow it — a green CI run over a
        genuine crash would be worse than the original bug."""

        def exploding_evaluate_all(*args, **kwargs):
            raise ValueError("a different simulated crash")

        monkeypatch.setattr(run_module, "evaluate_all", exploding_evaluate_all)

        with pytest.raises(ValueError, match="a different simulated crash"):
            run_module.main([
                "--once", "--dry-run", "--offline", "--source", "rss",
                "--db", crash_db,
            ])

    def test_the_database_is_still_closed_after_a_crash(self, crash_db, monkeypatch):
        """Leaving the SQLite connection open (and its WAL uncheckpointed)
        after every crash would eventually corrupt or bloat the file that
        gets committed back to the repo."""

        def exploding_evaluate_all(*args, **kwargs):
            raise RuntimeError("simulated crash for testing")

        monkeypatch.setattr(run_module, "evaluate_all", exploding_evaluate_all)

        with pytest.raises(RuntimeError):
            run_module.main([
                "--once", "--dry-run", "--offline", "--source", "rss",
                "--db", crash_db,
            ])

        # If close() never ran, WAL sidecar files would still be sitting next
        # to the main db file instead of having been checkpointed into it.
        import pathlib
        assert not pathlib.Path(crash_db + "-wal").exists()

    def test_a_normal_run_still_completes_and_records_no_crash(
        self, crash_db
    ):
        """The regression this refactor could have introduced: a totally
        ordinary run must still finish normally, with an empty (not
        crash-flavoured) notes field."""
        exit_code = run_module.main([
            "--once", "--dry-run", "--offline", "--source", "rss",
            "--db", crash_db,
        ])

        assert exit_code in (0, 1)   # 1 only if every source failed — still not a crash
        row = run_row(crash_db)
        assert row["finished_at"] is not None
        assert "CRASHED" not in (row["notes"] or "")
