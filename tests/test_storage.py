from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from proc_usage.spool import SpoolRecord
from proc_usage.storage import SQLiteUsageStorage


class SQLiteUsageStorageTests(unittest.TestCase):
    def test_apply_deltas_merges_counts_and_last_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        db="/db/main.fdb",
                        proc="PROC_A",
                        delta=2,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 10, tzinfo=timezone.utc),
                        db="/db/main.fdb",
                        proc="PROC_A",
                        delta=4,
                    ),
                ]
            )

            rows = storage.procedure_stats("PROC_A")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_calls"], 6)
            self.assertEqual(rows[0]["last_seen_at"], "2026-06-06T12:00:10+00:00")

    def test_top_procedures_are_sorted_by_total_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path = Path(tmp_dir) / "stats.sqlite3"
            storage = SQLiteUsageStorage(database_path)
            storage.initialize()

            storage.apply_deltas(
                [
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        db="/db/main.fdb",
                        proc="PROC_A",
                        delta=2,
                    ),
                    SpoolRecord(
                        ts=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
                        db="/db/main.fdb",
                        proc="PROC_B",
                        delta=9,
                    ),
                ]
            )

            rows = storage.top_procedures(limit=2)
            self.assertEqual([row["procedure"] for row in rows], ["PROC_B", "PROC_A"])


if __name__ == "__main__":
    unittest.main()
