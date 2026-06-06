from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bench.generate_sql import generate_benchmark_artifacts, procedure_name
from bench.run_benchmark import parse_plugin_settings, render_firebird_conf


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
            self.assertEqual(len(manifest.client_scripts), 2)
            self.assertIn("CREATE OR ALTER PROCEDURE BENCH_PROC_0001", (root / "bootstrap.sql").read_text(encoding="utf-8"))
            self.assertIn("EXECUTE PROCEDURE BENCH_PROC_", Path(manifest.client_scripts[0].path).read_text(encoding="utf-8"))


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


if __name__ == "__main__":
    unittest.main()
