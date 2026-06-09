from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from proc_usage.cli import main
from proc_usage.spool import SpoolRecord
from proc_usage.storage import SQLiteUsageStorage


class CliTests(unittest.TestCase):
    def test_top_supports_sql_kind_and_hour_filter(self) -> None:
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
                    )
                ]
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "top",
                        "--sqlite-db-path",
                        str(database_path),
                        "--kind",
                        "sql",
                        "--hour",
                        "2026-06-06T12:00Z",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("2026-06-06T12:00Z", output.getvalue())
            self.assertIn("SELECT", output.getvalue())
            self.assertIn("avg=5.00ms", output.getvalue())

    def test_show_supports_procedure_kind(self) -> None:
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
                        total_time_ms=18,
                        min_time_ms=8,
                        max_time_ms=10,
                    )
                ]
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "show",
                        "PROC_A",
                        "--sqlite-db-path",
                        str(database_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("2026-06-06T12:00Z", output.getvalue())
            self.assertIn("PROC_A", output.getvalue())
            self.assertIn("min=8ms", output.getvalue())

    def test_top_and_show_support_sql_text_kind(self) -> None:
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
                        total_time_ms=18,
                        min_time_ms=8,
                        max_time_ms=10,
                    )
                ]
            )

            top_output = io.StringIO()
            with redirect_stdout(top_output):
                exit_code = main(
                    [
                        "top",
                        "--sqlite-db-path",
                        str(database_path),
                        "--kind",
                        "sql-text",
                        "--hour",
                        "2026-06-06T12:00Z",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(fingerprint, top_output.getvalue())
            self.assertIn("sql=SELECT * FROM BENCH_DATA WHERE ID = 42", top_output.getvalue())

            show_output = io.StringIO()
            with redirect_stdout(show_output):
                exit_code = main(
                    [
                        "show",
                        fingerprint,
                        "--sqlite-db-path",
                        str(database_path),
                        "--kind",
                        "sql-text",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("SQL text:", show_output.getvalue())
            self.assertIn(sql_text, show_output.getvalue())
            self.assertIn("avg=9.00ms", show_output.getvalue())


if __name__ == "__main__":
    unittest.main()
