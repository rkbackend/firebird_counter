from __future__ import annotations

import hashlib
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
                {
                    "ts": "2026-06-06T12:00:00.000Z",
                    "kind": "procedure",
                    "hour": "2026-06-06T12:00Z",
                    "db": "/db/main.fdb",
                    "name": "PROC_A",
                    "count": 2,
                    "total_time_ms": 14,
                    "min_time_ms": 6,
                    "max_time_ms": 8,
                },
                {
                    "ts": "2026-06-06T12:00:05.000Z",
                    "kind": "procedure",
                    "hour": "2026-06-06T12:00Z",
                    "db": "/db/main.fdb",
                    "name": "PROC_A",
                    "count": 3,
                    "total_time_ms": 27,
                    "min_time_ms": 7,
                    "max_time_ms": 11,
                },
                {
                    "ts": "2026-06-06T12:00:03.000Z",
                    "kind": "sql",
                    "hour": "2026-06-06T12:00Z",
                    "db": "/db/main.fdb",
                    "name": "SELECT",
                    "count": 5,
                    "total_time_ms": 20,
                    "min_time_ms": 2,
                    "max_time_ms": 7,
                },
                {
                    "ts": "2026-06-06T12:00:04.000Z",
                    "kind": "sql_text",
                    "hour": "2026-06-06T12:00Z",
                    "db": "/db/main.fdb",
                    "name": "SELECT * FROM BENCH_DATA WHERE ID = 42",
                    "count": 2,
                    "total_time_ms": 18,
                    "min_time_ms": 8,
                    "max_time_ms": 10,
                },
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
            procedure_rows = storage.top_procedures(limit=10, usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(procedure_rows), 1)
            self.assertEqual(procedure_rows[0]["usage_hour"], "2026-06-06T12:00Z")
            self.assertEqual(procedure_rows[0]["name"], "PROC_A")
            self.assertEqual(procedure_rows[0]["total_calls"], 5)
            self.assertEqual(procedure_rows[0]["total_time_ms"], 41)
            self.assertEqual(procedure_rows[0]["min_time_ms"], 6)
            self.assertEqual(procedure_rows[0]["max_time_ms"], 11)
            self.assertEqual(procedure_rows[0]["last_seen_at"], "2026-06-06T12:00:05+00:00")

            sql_rows = storage.top_sql(limit=10, usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(sql_rows), 1)
            self.assertEqual(sql_rows[0]["name"], "SELECT")
            self.assertEqual(sql_rows[0]["total_calls"], 5)

            sql_text_rows = storage.top_usage(kind="sql-text", limit=10, usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(sql_text_rows), 1)
            self.assertEqual(
                sql_text_rows[0]["name"],
                hashlib.sha256("SELECT * FROM BENCH_DATA WHERE ID = 42".encode("utf-8")).hexdigest(),
            )
            self.assertEqual(sql_text_rows[0]["sql_text"], "SELECT * FROM BENCH_DATA WHERE ID = 42")
            self.assertEqual(sql_text_rows[0]["total_calls"], 2)

            self.assertEqual(list(spool_dir.glob("*")), [])

    def test_processing_file_is_retried_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            spool_dir = root / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            database_path = root / "stats.sqlite3"

            payload = {
                "ts": "2026-06-06T12:00:00.000Z",
                "kind": "procedure",
                "hour": "2026-06-06T12:00Z",
                "db": "/db/main.fdb",
                "name": "PROC_A",
                "count": 7,
                "total_time_ms": 49,
                "min_time_ms": 7,
                "max_time_ms": 7,
            }
            (spool_dir / "orphaned.processing").write_text(json.dumps(payload) + "\n", encoding="utf-8")

            service = ProcUsageService.from_config(
                ServiceConfig(spool_dir=spool_dir, sqlite_db_path=database_path, poll_interval_sec=1)
            )
            service.initialize()
            processed = service.ingest_pending_files()

            self.assertEqual(processed, 1)

            storage = SQLiteUsageStorage(database_path)
            rows = storage.procedure_stats("PROC_A", usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_calls"], 7)
            self.assertEqual(rows[0]["total_time_ms"], 49)

    def test_permission_denied_on_rename_falls_back_to_in_place_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            spool_dir = root / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            database_path = root / "stats.sqlite3"

            payload = {
                "ts": "2026-06-06T12:00:00.000Z",
                "kind": "procedure",
                "hour": "2026-06-06T12:00Z",
                "db": "/db/main.fdb",
                "name": "PROC_A",
                "count": 7,
                "total_time_ms": 42,
                "min_time_ms": 6,
                "max_time_ms": 6,
            }
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
            rows = storage.procedure_stats("PROC_A", usage_hour="2026-06-06T12:00Z")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_calls"], 7)
            self.assertTrue(spool_file.exists())


if __name__ == "__main__":
    unittest.main()
