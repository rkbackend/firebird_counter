from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bench.generate_sql import generate_benchmark_artifacts, procedure_name
from bench.run_benchmark import (
    FirebirdBenchmarkRunner,
    IterationResult,
    ModeResult,
    _build_parser,
    parse_plugin_settings,
    render_firebird_conf,
)


class BenchmarkGenerationTests(unittest.TestCase):
    def test_procedure_name_is_zero_padded(self) -> None:
        self.assertEqual(procedure_name(1), "BENCH_PROC_0001")
        self.assertEqual(procedure_name(1000), "BENCH_PROC_1000")

    def test_generate_benchmark_artifacts_writes_manifest_and_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest = generate_benchmark_artifacts(
                output_dir=root,
                database_path="localhost:/tmp/bench.fdb",
                client_count=2,
                procedure_count=10,
                rounds_per_client=3,
            )

            self.assertTrue((root / "bootstrap.sql").exists())
            self.assertTrue((root / "reset.sql").exists())
            self.assertTrue((root / "manifest.json").exists())
            self.assertEqual(manifest.expected_execute_procedure_calls, 2 * 3 * 7)
            self.assertEqual(manifest.expected_logical_operations, 2 * 3 * 10)
            self.assertEqual(len(manifest.client_scripts), 2)
            self.assertIn("CREATE OR ALTER PROCEDURE BENCH_PROC_0001", (root / "bootstrap.sql").read_text(encoding="utf-8"))
            client_script = Path(manifest.client_scripts[0].path).read_text(encoding="utf-8")
            self.assertIn("EXECUTE PROCEDURE BENCH_PROC_", client_script)
            self.assertIn("SELECT VALUE_INTEGER FROM BENCH_AUX", client_script)
            self.assertIn("UPDATE BENCH_AUX", client_script)
            self.assertIn("INSERT INTO BENCH_TRANSIENT", client_script)
            self.assertIn("DELETE FROM BENCH_TRANSIENT", client_script)


class BenchmarkConfigTests(unittest.TestCase):
    def test_render_firebird_conf_replaces_trace_settings_with_managed_block(self) -> None:
        source = "\n".join(
            [
                "#TracePlugin = fbtrace",
                "TracePlugin = ProcUsageTrace",
                "AuditTraceConfigFile = /etc/firebird/3.0/proc_usage_fbtrace.conf",
                "UdfAccess = None",
            ]
        )

        rendered = render_firebird_conf(
            base_content=source,
            enable_plugin=False,
            plugin_name="ProcUsageTrace",
            audit_trace_conf=Path("/etc/firebird/3.0/proc_usage_fbtrace.conf"),
        )

        self.assertIn("UdfAccess = None", rendered)
        self.assertIn("# BEGIN PROC_USAGE_BENCHMARK", rendered)
        self.assertNotIn("TracePlugin = ProcUsageTrace\nAuditTraceConfigFile", rendered)

    def test_parse_plugin_settings_reads_spool_dir_and_flush_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "plugins.conf"
            config_path.write_text(
                "\n".join(
                    [
                        "Config = ProcUsageTraceConfig {",
                        "  spool_dir = /tmp/firebird_proc_usage_spool",
                        "  debug_log_path = /tmp/proc_usage_trace_debug.log",
                        "  flush_interval_sec = 5",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )

            settings = parse_plugin_settings(config_path)
            self.assertEqual(settings.spool_dir, Path("/tmp/firebird_proc_usage_spool"))
            self.assertEqual(settings.debug_log_path, Path("/tmp/proc_usage_trace_debug.log"))
            self.assertEqual(settings.flush_interval_sec, 5)


class BenchmarkRunnerTests(unittest.TestCase):
    def _make_runner(self, root: Path) -> FirebirdBenchmarkRunner:
        spool_dir = root / "spool"
        firebird_conf = root / "firebird.conf"
        plugin_conf = root / "plugins.conf"
        audit_trace_conf = root / "fbtrace.conf"

        firebird_conf.write_text("UdfAccess = None\n", encoding="utf-8")
        plugin_conf.write_text(
            "\n".join(
                [
                    "Config = ProcUsageTraceConfig {",
                    f"  spool_dir = {spool_dir}",
                    "  flush_interval_sec = 2",
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        audit_trace_conf.write_text("database = *\n", encoding="utf-8")

        args = argparse.Namespace(
            mode="both",
            workspace_root=str(root / "workspace"),
            client_count=2,
            procedure_count=10,
            target_runtime_sec=20,
            calibration_rounds=10,
            min_rounds_per_client=5,
            iterations=2,
            user="sysdba",
            password="masterkey",
            service_name="firebird3.0",
            plugin_name="ProcUsageTrace",
            host="localhost",
            port=3050,
            firebird_conf=str(firebird_conf),
            plugin_conf=str(plugin_conf),
            audit_trace_conf=str(audit_trace_conf),
            sudo_command=None,
            skip_plugin_binary_check=True,
        )
        return FirebirdBenchmarkRunner(args)

    def test_read_observed_call_stats_uses_distinct_procedures_across_hour_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = self._make_runner(Path(tmp_dir))

            with sqlite3.connect(runner.sqlite_db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE procedure_usage_stats (
                        usage_hour TEXT NOT NULL,
                        database TEXT NOT NULL,
                        procedure TEXT NOT NULL,
                        total_calls INTEGER NOT NULL
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO procedure_usage_stats (usage_hour, database, procedure, total_calls)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        ("2026-06-09T10:00Z", "/db/main.fdb", "BENCH_PROC_0001", 7),
                        ("2026-06-09T11:00Z", "/db/main.fdb", "BENCH_PROC_0001", 5),
                        ("2026-06-09T10:00Z", "/db/main.fdb", "BENCH_PROC_0002", 9),
                        ("2026-06-09T10:00Z", "/db/main.fdb", "OTHER_PROC", 100),
                    ],
                )

            total_calls, distinct_procedures = runner._read_observed_call_stats()
            self.assertEqual(total_calls, 21)
            self.assertEqual(distinct_procedures, 2)

    def test_build_execution_plan_alternates_modes_in_both_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = self._make_runner(Path(tmp_dir))

            self.assertEqual(
                runner._build_execution_plan(),
                [
                    {"iteration": 1, "modes": ["without_plugin", "with_plugin"]},
                    {"iteration": 2, "modes": ["with_plugin", "without_plugin"]},
                ],
            )

    def test_build_execution_plan_keeps_single_mode_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = self._make_runner(Path(tmp_dir))
            runner.args.mode = "with_plugin"

            self.assertEqual(
                runner._build_execution_plan(),
                [
                    {"iteration": 1, "modes": ["with_plugin"]},
                    {"iteration": 2, "modes": ["with_plugin"]},
                ],
            )

    def test_build_report_adds_comparison_for_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = self._make_runner(Path(tmp_dir))

            without_plugin = ModeResult(
                mode="without_plugin",
                rounds_per_client=40,
                iterations=[
                    IterationResult(
                        iteration=1,
                        sql_runtime_sec=10.0,
                        ingest_runtime_sec=None,
                        spool_file_count=0,
                        expected_execute_procedure_calls=560,
                        observed_execute_procedure_calls=None,
                        distinct_procedures_observed=None,
                        valid=True,
                        note="ok",
                    )
                ],
                avg_sql_runtime_sec=10.0,
                min_sql_runtime_sec=9.5,
                avg_ingest_runtime_sec=None,
                min_ingest_runtime_sec=None,
            )
            with_plugin = ModeResult(
                mode="with_plugin",
                rounds_per_client=40,
                iterations=[
                    IterationResult(
                        iteration=1,
                        sql_runtime_sec=11.5,
                        ingest_runtime_sec=0.7,
                        spool_file_count=3,
                        expected_execute_procedure_calls=560,
                        observed_execute_procedure_calls=560,
                        distinct_procedures_observed=10,
                        valid=True,
                        note="ok",
                    )
                ],
                avg_sql_runtime_sec=11.5,
                min_sql_runtime_sec=11.0,
                avg_ingest_runtime_sec=0.7,
                min_ingest_runtime_sec=0.6,
            )

            report = runner._build_report(
                started_at=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
                calibration_runtime=2.5,
                benchmark_rounds=40,
                execution_plan=[
                    {"iteration": 1, "modes": ["without_plugin", "with_plugin"]},
                    {"iteration": 2, "modes": ["with_plugin", "without_plugin"]},
                ],
                results={
                    "without_plugin": without_plugin,
                    "with_plugin": with_plugin,
                },
            )

            self.assertEqual(report["benchmark_rounds_per_client"], 40)
            self.assertEqual(
                report["execution_plan"],
                [
                    {"iteration": 1, "modes": ["without_plugin", "with_plugin"]},
                    {"iteration": 2, "modes": ["with_plugin", "without_plugin"]},
                ],
            )
            self.assertEqual(report["client_count"], 2)
            self.assertEqual(report["procedure_count"], 10)
            self.assertEqual(report["comparison"]["sql_delta_sec"], 1.5)
            self.assertEqual(report["comparison"]["sql_delta_pct"], 15.0)
            self.assertEqual(report["comparison"]["ingest_delta_sec"], 0.7)
            self.assertIsNone(report["comparison"]["ingest_delta_pct"])
            self.assertEqual(report["plugin_settings"]["flush_interval_sec"], 2)

    def test_parser_uses_five_iterations_by_default(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--user", "sysdba", "--password", "masterkey"])

        self.assertEqual(args.iterations, 5)


if __name__ == "__main__":
    unittest.main()
