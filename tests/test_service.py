from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proc_usage.config import ServiceConfig
from proc_usage.service import ProcUsageService
from proc_usage.storage import SQLiteUsageStorage


class ProcUsageServiceTests(unittest.TestCase):
    def test_ingest_pending_files_updates_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            spool_dir = root / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            database_path = root / "stats.sqlite3"

            payloads = [
                {"ts": "2026-06-06T12:00:00.000Z", "db": "/db/main.fdb", "proc": "PROC_A", "delta": 2},
                {"ts": "2026-06-06T12:00:05.000Z", "db": "/db/main.fdb", "proc": "PROC_A", "delta": 3},
                {"ts": "2026-06-06T12:00:03.000Z", "db": "/db/main.fdb", "proc": "PROC_B", "delta": 1},
            ]
            lines = "\n".join(json.dumps(item) for item in payloads) + "\n"
            (spool_dir / "batch_001.jsonl").write_text(lines, encoding="utf-8")

            service = ProcUsageService.from_config(
                ServiceConfig(spool_dir=spool_dir, sqlite_db_path=database_path, poll_interval_sec=1)
            )
            service.initialize()

            processed = service.ingest_pending_files()
            self.assertEqual(processed, 1)

            storage = SQLiteUsageStorage(database_path)
            rows = storage.top_procedures(limit=10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["procedure"], "PROC_A")
            self.assertEqual(rows[0]["total_calls"], 5)
            self.assertEqual(rows[0]["last_seen_at"], "2026-06-06T12:00:05+00:00")
            self.assertEqual(rows[1]["procedure"], "PROC_B")
            self.assertEqual(rows[1]["total_calls"], 1)

            self.assertEqual(list(spool_dir.glob("*")), [])

    def test_processing_file_is_retried_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            spool_dir = root / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            database_path = root / "stats.sqlite3"

            payload = {"ts": "2026-06-06T12:00:00.000Z", "db": "/db/main.fdb", "proc": "PROC_A", "delta": 7}
            (spool_dir / "orphaned.processing").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            service = ProcUsageService.from_config(
                ServiceConfig(spool_dir=spool_dir, sqlite_db_path=database_path, poll_interval_sec=1)
            )
            service.initialize()
            processed = service.ingest_pending_files()

            self.assertEqual(processed, 1)

            storage = SQLiteUsageStorage(database_path)
            rows = storage.procedure_stats("PROC_A")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_calls"], 7)

    def test_permission_denied_on_rename_falls_back_to_in_place_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            spool_dir = root / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            database_path = root / "stats.sqlite3"

            payload = {"ts": "2026-06-06T12:00:00.000Z", "db": "/db/main.fdb", "proc": "PROC_A", "delta": 7}
            spool_file = spool_dir / "batch_001.jsonl"
            spool_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            service = ProcUsageService.from_config(
                ServiceConfig(spool_dir=spool_dir, sqlite_db_path=database_path, poll_interval_sec=1)
            )
            service.initialize()

            with patch("pathlib.Path.rename", side_effect=PermissionError("sticky directory")):
                processed = service.ingest_pending_files()
                self.assertEqual(processed, 1)

                processed_again = service.ingest_pending_files()
                self.assertEqual(processed_again, 0)

            storage = SQLiteUsageStorage(database_path)
            rows = storage.procedure_stats("PROC_A")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_calls"], 7)
            self.assertTrue(spool_file.exists())


if __name__ == "__main__":
    unittest.main()
