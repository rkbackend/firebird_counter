from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from proc_usage.spool import SpoolRecord
from proc_usage.storage import SQLiteUsageStorage


class SQLiteUsageStorageTests(unittest.TestCase):
    def test_apply_deltas_merges_procedure_counts_and_timing_per_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        kind="procedure",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name="PROC_A",
                        count=2,
                        total_time_ms=15,
                        min_time_ms=5,
                        max_time_ms=10,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 10, tzinfo=timezone.utc),
                        kind="procedure",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name="PROC_A",
                        count=3,
                        total_time_ms=24,
                        min_time_ms=4,
                        max_time_ms=11,
                    ),
                ]
            )

            rows = storage.procedure_stats("PROC_A")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usage_hour"], "2026-06-06T12:00Z")
            self.assertEqual(rows[0]["total_calls"], 5)
            self.assertEqual(rows[0]["total_time_ms"], 39)
            self.assertEqual(rows[0]["min_time_ms"], 4)
            self.assertEqual(rows[0]["max_time_ms"], 11)
            self.assertAlmostEqual(rows[0]["avg_time_ms"], 7.8)
            self.assertEqual(rows[0]["last_seen_at"], "2026-06-06T12:00:10+00:00")

    def test_same_name_in_different_hours_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 59, 0, tzinfo=timezone.utc),
                        kind="procedure",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name="PROC_A",
                        count=2,
                        total_time_ms=14,
                        min_time_ms=6,
                        max_time_ms=8,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 13, 1, 0, tzinfo=timezone.utc),
                        kind="procedure",
                        hour="2026-06-06T13:00Z",
                        db="/db/main.fdb",
                        name="PROC_A",
                        count=3,
                        total_time_ms=27,
                        min_time_ms=7,
                        max_time_ms=11,
                    ),
                ]
            )

            rows = storage.procedure_stats("PROC_A")
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["usage_hour"] for row in rows], ["2026-06-06T13:00Z", "2026-06-06T12:00Z"])

    def test_sql_stats_are_stored_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        kind="sql",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name="SELECT",
                        count=4,
                        total_time_ms=20,
                        min_time_ms=3,
                        max_time_ms=8,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 5, tzinfo=timezone.utc),
                        kind="sql",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name="SELECT",
                        count=1,
                        total_time_ms=12,
                        min_time_ms=12,
                        max_time_ms=12,
                    ),
                ]
            )

            rows = storage.sql_stats("SELECT", usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usage_hour"], "2026-06-06T12:00Z")
            self.assertEqual(rows[0]["total_calls"], 5)
            self.assertEqual(rows[0]["total_time_ms"], 32)
            self.assertEqual(rows[0]["min_time_ms"], 3)
            self.assertEqual(rows[0]["max_time_ms"], 12)
            self.assertAlmostEqual(rows[0]["avg_time_ms"], 6.4)

    def test_sql_text_stats_are_stored_by_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()
            sql_text = "SELECT * FROM BENCH_DATA WHERE ID = 42"
            fingerprint = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        kind="sql_text",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name=sql_text,
                        count=2,
                        total_time_ms=30,
                        min_time_ms=10,
                        max_time_ms=20,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 5, tzinfo=timezone.utc),
                        kind="sql_text",
                        hour="2026-06-06T12:00Z",
                        db="/db/main.fdb",
                        name=sql_text,
                        count=1,
                        total_time_ms=25,
                        min_time_ms=25,
                        max_time_ms=25,
                    ),
                ]
            )

            rows = storage.top_usage(kind="sql-text", usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], fingerprint)
            self.assertEqual(rows[0]["sql_text"], sql_text)
            self.assertEqual(rows[0]["total_calls"], 3)
            self.assertEqual(rows[0]["total_time_ms"], 55)
            self.assertEqual(rows[0]["min_time_ms"], 10)
            self.assertEqual(rows[0]["max_time_ms"], 25)
            self.assertAlmostEqual(rows[0]["avg_time_ms"], 55 / 3)

            stats_rows = storage.usage_stats(kind="sql-text", name=fingerprint, usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(stats_rows), 1)
            self.assertEqual(stats_rows[0]["sql_text"], sql_text)
            self.assertEqual(storage.sql_text_by_fingerprint(fingerprint), sql_text)


if __name__ == "__main__":
    unittest.main()
