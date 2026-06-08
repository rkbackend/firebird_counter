from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bench.generate_sql import GeneratedBenchmark, generate_benchmark_artifacts
else:
    from .generate_sql import GeneratedBenchmark, generate_benchmark_artifacts


DEFAULT_FIREBIRD_CONF = Path("/etc/firebird/3.0/firebird.conf")
DEFAULT_PLUGIN_CONF = Path("/etc/firebird/3.0/plugins.conf")
DEFAULT_AUDIT_TRACE_CONF = Path("/etc/firebird/3.0/proc_usage_fbtrace.conf")
DEFAULT_PLUGIN_NAME = "ProcUsageTrace"
DEFAULT_SERVICE_NAME = "firebird3.0"


@dataclass
class PluginSettings:
    spool_dir: Path
    flush_interval_sec: int
    debug_log_path: Optional[Path]


@dataclass
class IterationResult:
    iteration: int
    sql_runtime_sec: float
    ingest_runtime_sec: Optional[float]
    spool_file_count: int
    expected_execute_procedure_calls: int
    observed_execute_procedure_calls: Optional[int]
    distinct_procedures_observed: Optional[int]
    valid: bool
    note: str


@dataclass
class ModeResult:
    mode: str
    rounds_per_client: int
    iterations: list[IterationResult]
    avg_sql_runtime_sec: float
    min_sql_runtime_sec: float
    avg_ingest_runtime_sec: Optional[float]
    min_ingest_runtime_sec: Optional[float]


def parse_plugin_settings(plugin_conf_path: Path) -> PluginSettings:
    spool_dir: Optional[Path] = None
    debug_log_path: Optional[Path] = None
    flush_interval_sec = 1

    for raw_line in plugin_conf_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = (item.strip() for item in line.split("=", 1))
        if key == "spool_dir" and value:
            spool_dir = Path(value)
        elif key == "debug_log_path" and value:
            debug_log_path = Path(value)
        elif key == "flush_interval_sec" and value:
            flush_interval_sec = int(value)

    if spool_dir is None:
        raise ValueError(f"Не удалось найти spool_dir в {plugin_conf_path}")

    return PluginSettings(
        spool_dir=spool_dir,
        flush_interval_sec=flush_interval_sec,
        debug_log_path=debug_log_path,
    )


def render_firebird_conf(base_content: str, enable_plugin: bool, plugin_name: str, audit_trace_conf: Path) -> str:
    filtered_lines: list[str] = []
    for line in base_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("TracePlugin") or stripped.startswith("#TracePlugin"):
            continue
        if stripped.startswith("AuditTraceConfigFile") or stripped.startswith("#AuditTraceConfigFile"):
            continue
        filtered_lines.append(line)

    filtered_lines.extend(
        [
            "",
            "# BEGIN PROC_USAGE_BENCHMARK",
        ]
    )
    if enable_plugin:
        filtered_lines.append(f"TracePlugin = {plugin_name}")
        filtered_lines.append(f"AuditTraceConfigFile = {audit_trace_conf}")
    else:
        filtered_lines.append("# Trace plugin временно отключен для benchmark baseline")
        filtered_lines.append("# AuditTraceConfigFile временно отключен для benchmark baseline")
    filtered_lines.append("# END PROC_USAGE_BENCHMARK")
    return "\n".join(filtered_lines) + "\n"


def _run_command(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture_output,
        input=input_text,
    )


def _build_sudo_prefix(sudo_command: Optional[str]) -> list[str]:
    if not sudo_command:
        return []
    return shlex.split(sudo_command)


class FirebirdBenchmarkRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo_root = Path(__file__).resolve().parents[1]
        self.python_executable = sys.executable
        self.workspace_root = Path(args.workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.results_dir = self.repo_root / "bench" / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.firebird_conf_path = Path(args.firebird_conf)
        self.plugin_conf_path = Path(args.plugin_conf)
        self.audit_trace_conf = Path(args.audit_trace_conf)
        self.service_name = args.service_name
        self.plugin_name = args.plugin_name
        self.host = args.host
        self.port = args.port
        self.sudo_prefix = _build_sudo_prefix(args.sudo_command)
        self.plugin_settings = parse_plugin_settings(self.plugin_conf_path)
        self.sqlite_db_path = self.workspace_root / "proc_usage_benchmark.sqlite3"
        self.service_config_path = self.workspace_root / "python_service.json"
        self.benchmark_db_file = Path("/tmp") / f"firebird_benchmark_{self.run_id}.fdb"
        self.connection_database = f"{self.host}/{self.port}:{self.benchmark_db_file}"
        self.original_firebird_conf = self.firebird_conf_path.read_text(encoding="utf-8")

    def preflight(self) -> None:
        if shutil.which("isql-fb") is None:
            raise RuntimeError("isql-fb не найден в PATH")
        if not self.firebird_conf_path.exists():
            raise RuntimeError(f"firebird.conf не найден: {self.firebird_conf_path}")
        if not self.plugin_conf_path.exists():
            raise RuntimeError(f"plugins.conf не найден: {self.plugin_conf_path}")
        if not self.audit_trace_conf.exists():
            raise RuntimeError(f"trace config не найден: {self.audit_trace_conf}")
        if self.args.mode in {"with_plugin", "both"} and not self.args.skip_plugin_binary_check:
            plugin_binary = Path("/usr/lib/x86_64-linux-gnu/firebird/3.0/plugins/libproc_usage_trace.so")
            if not plugin_binary.exists():
                raise RuntimeError(f"Плагин не найден: {plugin_binary}")
        if self.args.user is None or self.args.password is None:
            raise RuntimeError("Нужны --user и --password для подключения к Firebird")

    def run(self) -> dict[str, object]:
        self.preflight()
        started_at = datetime.now(tz=timezone.utc)
        calibration_rounds = self.args.calibration_rounds
        benchmark_rounds = calibration_rounds
        results: dict[str, ModeResult] = {}

        try:
            self._ensure_mode(enable_plugin=False)
            self._recreate_database(rounds_per_client=calibration_rounds)
            calibration_runtime = self._measure_sql_runtime(
                rounds_per_client=calibration_rounds,
                label="calibration",
            )
            benchmark_rounds = self._estimate_rounds(calibration_runtime)

            modes = [self.args.mode] if self.args.mode != "both" else ["without_plugin", "with_plugin"]
            for mode in modes:
                enable_plugin = mode == "with_plugin"
                results[mode] = self._run_mode(mode=mode, enable_plugin=enable_plugin, rounds_per_client=benchmark_rounds)
        finally:
            self._restore_firebird_conf()

        latest = self._build_report(
            started_at=started_at,
            calibration_runtime=calibration_runtime,
            benchmark_rounds=benchmark_rounds,
            results=results,
        )
        timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        report_paths = self._write_report_files(timestamp=timestamp, report=latest)
        latest["report_paths"] = report_paths
        self._print_report(latest)
        return latest

    def _estimate_rounds(self, calibration_runtime: float) -> int:
        if calibration_runtime <= 0:
            return self.args.calibration_rounds
        scaled = math.ceil((self.args.target_runtime_sec / calibration_runtime) * self.args.calibration_rounds * 0.9)
        return max(self.args.min_rounds_per_client, scaled)

    def _run_mode(self, mode: str, enable_plugin: bool, rounds_per_client: int) -> ModeResult:
        self._ensure_mode(enable_plugin=enable_plugin)
        artifacts = self._write_artifacts(rounds_per_client=rounds_per_client)
        iterations: list[IterationResult] = []

        for iteration in range(1, self.args.iterations + 1):
            self._prepare_iteration(artifacts=artifacts)
            self._run_warmup(artifacts=artifacts)
            self._prepare_iteration(artifacts=artifacts)

            sql_runtime_sec = self._run_client_scripts(artifacts=artifacts, label=f"{mode}-iter-{iteration}")
            ingest_runtime_sec: Optional[float] = None
            spool_file_count = self._count_spool_files()
            observed_calls: Optional[int] = None
            distinct_procs: Optional[int] = None
            valid = True
            note = "ok"

            if enable_plugin:
                time.sleep(self.plugin_settings.flush_interval_sec * 2)
                spool_file_count = self._count_spool_files()
                ingest_runtime_sec = self._run_ingest_once()
                observed_calls, distinct_procs = self._read_observed_call_stats()
                if observed_calls != artifacts.expected_execute_procedure_calls:
                    valid = False
                    note = (
                        "mismatch between expected and observed execute procedure calls: "
                        f"{artifacts.expected_execute_procedure_calls} != {observed_calls}"
                    )
                elif spool_file_count <= 0:
                    valid = False
                    note = "plugin mode finished without spool files"
            else:
                if spool_file_count != 0:
                    valid = False
                    note = "baseline mode produced spool files"

            iterations.append(
                IterationResult(
                    iteration=iteration,
                    sql_runtime_sec=sql_runtime_sec,
                    ingest_runtime_sec=ingest_runtime_sec,
                    spool_file_count=spool_file_count,
                    expected_execute_procedure_calls=artifacts.expected_execute_procedure_calls,
                    observed_execute_procedure_calls=observed_calls,
                    distinct_procedures_observed=distinct_procs,
                    valid=valid,
                    note=note,
                )
            )

        sql_values = [item.sql_runtime_sec for item in iterations]
        ingest_values = [item.ingest_runtime_sec for item in iterations if item.ingest_runtime_sec is not None]
        return ModeResult(
            mode=mode,
            rounds_per_client=rounds_per_client,
            iterations=iterations,
            avg_sql_runtime_sec=sum(sql_values) / len(sql_values),
            min_sql_runtime_sec=min(sql_values),
            avg_ingest_runtime_sec=(sum(ingest_values) / len(ingest_values)) if ingest_values else None,
            min_ingest_runtime_sec=min(ingest_values) if ingest_values else None,
        )

    def _write_artifacts(self, rounds_per_client: int, label: str = "main") -> GeneratedBenchmark:
        output_dir = self.workspace_root / f"{label}_artifacts_{rounds_per_client}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return generate_benchmark_artifacts(
            output_dir=output_dir,
            database_path=self.connection_database,
            client_count=self.args.client_count,
            procedure_count=self.args.procedure_count,
            rounds_per_client=rounds_per_client,
        )

    def _run_warmup(self, artifacts: GeneratedBenchmark) -> None:
        warmup_rounds = max(2, min(10, max(1, artifacts.rounds_per_client // 10)))
        warmup_artifacts = self._write_artifacts(rounds_per_client=warmup_rounds, label="warmup")
        self._run_client_scripts(artifacts=warmup_artifacts, label="warmup")

    def _prepare_iteration(self, artifacts: GeneratedBenchmark) -> None:
        self._cleanup_spool_dir()
        self._reset_benchmark_db(artifacts=artifacts)
        self._reset_sqlite()

    def _reset_sqlite(self) -> None:
        self.sqlite_db_path.unlink(missing_ok=True)
        self.service_config_path.write_text(
            json.dumps(
                {
                    "spool_dir": str(self.plugin_settings.spool_dir),
                    "sqlite_db_path": str(self.sqlite_db_path),
                    "poll_interval_sec": 1,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _run_command(
            [
                self.python_executable,
                "-m",
                "proc_usage",
                "init-db",
                "--config",
                str(self.service_config_path),
            ],
            cwd=self.repo_root,
        )

    def _count_spool_files(self) -> int:
        if not self.plugin_settings.spool_dir.exists():
            return 0
        patterns = ("*.jsonl", "*.processing")
        return sum(1 for pattern in patterns for _ in self.plugin_settings.spool_dir.glob(pattern))

    def _read_observed_call_stats(self) -> tuple[int, int]:
        with sqlite3.connect(self.sqlite_db_path) as connection:
            total_calls = connection.execute(
                """
                SELECT COALESCE(SUM(total_calls), 0)
                FROM procedure_usage_stats
                WHERE procedure LIKE 'BENCH_PROC_%'
                """
            ).fetchone()[0]
            distinct_procedures = connection.execute(
                """
                SELECT COUNT(*)
                FROM procedure_usage_stats
                WHERE procedure LIKE 'BENCH_PROC_%'
                """
            ).fetchone()[0]
        return int(total_calls), int(distinct_procedures)

    def _measure_sql_runtime(self, rounds_per_client: int, label: str) -> float:
        artifacts = self._write_artifacts(rounds_per_client=rounds_per_client, label=label)
        self._prepare_iteration(artifacts=artifacts)
        return self._run_client_scripts(artifacts=artifacts, label=label)

    def _recreate_database(self, rounds_per_client: int) -> None:
        artifacts = self._write_artifacts(rounds_per_client=rounds_per_client, label="bootstrap")
        self._safe_unlink(self.benchmark_db_file)
        self._cleanup_spool_dir()
        bootstrap_command = self._isql_command(connect_database=False)
        _run_command(
            bootstrap_command,
            cwd=self.repo_root,
            input_text=Path(artifacts.bootstrap_sql).read_text(encoding="utf-8"),
        )

    def _reset_benchmark_db(self, artifacts: GeneratedBenchmark) -> None:
        _run_command(
            self._isql_command(),
            cwd=self.repo_root,
            input_text=Path(artifacts.reset_sql).read_text(encoding="utf-8"),
        )

    def _run_client_scripts(self, artifacts: GeneratedBenchmark, label: str) -> float:
        started_at = time.monotonic()
        processes: list[tuple[ClientScriptSpec, subprocess.Popen[str]]] = []
        try:
            for script_spec in artifacts.client_scripts:
                process = subprocess.Popen(
                    self._isql_command(),
                    cwd=self.repo_root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append((script_spec, process))

            failures: list[str] = []
            for script_spec, process in processes:
                stdout, stderr = process.communicate(Path(script_spec.path).read_text(encoding="utf-8"))
                if process.returncode != 0:
                    failures.append(
                        f"client {script_spec.client_id} failed with exit {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                    )
                elif "Statement failed" in stdout or "Statement failed" in stderr:
                    failures.append(f"client {script_spec.client_id} reported Firebird error\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")

            if failures:
                raise RuntimeError(f"SQL workload '{label}' failed:\n\n" + "\n\n".join(failures[:3]))
        finally:
            for _, process in processes:
                if process.poll() is None:
                    process.kill()

        return time.monotonic() - started_at

    def _run_ingest_once(self) -> float:
        started_at = time.monotonic()
        _run_command(
            [
                self.python_executable,
                "-m",
                "proc_usage",
                "ingest-once",
                "--config",
                str(self.service_config_path),
            ],
            cwd=self.repo_root,
        )
        return time.monotonic() - started_at

    def _isql_command(self, *, connect_database: bool = True) -> list[str]:
        command = ["isql-fb", "-quiet"]
        if connect_database:
            command.append(self.connection_database)
        command.extend(
            [
                "-user",
                self.args.user,
                "-password",
                self.args.password,
            ]
        )
        return command

    def _cleanup_spool_dir(self) -> None:
        self.plugin_settings.spool_dir.mkdir(parents=True, exist_ok=True)
        for path in list(self.plugin_settings.spool_dir.glob("*.jsonl")) + list(self.plugin_settings.spool_dir.glob("*.processing")):
            self._safe_unlink(path)

    def _ensure_mode(self, enable_plugin: bool) -> None:
        rendered = render_firebird_conf(
            base_content=self.original_firebird_conf,
            enable_plugin=enable_plugin,
            plugin_name=self.plugin_name,
            audit_trace_conf=self.audit_trace_conf,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate = Path(tmp_dir) / "firebird.conf"
            candidate.write_text(rendered, encoding="utf-8")
            self._copy_with_privileges(candidate, self.firebird_conf_path)
        self._restart_firebird_service()

    def _restore_firebird_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate = Path(tmp_dir) / "firebird.conf"
            candidate.write_text(self.original_firebird_conf, encoding="utf-8")
            self._copy_with_privileges(candidate, self.firebird_conf_path)
        self._restart_firebird_service()

    def _copy_with_privileges(self, source: Path, destination: Path) -> None:
        command = [*self.sudo_prefix, "install", "-m", "0644", str(source), str(destination)]
        _run_command(command, cwd=self.repo_root)

    def _safe_unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            if not self.sudo_prefix:
                raise
            _run_command([*self.sudo_prefix, "rm", "-f", str(path)], cwd=self.repo_root)

    def _restart_firebird_service(self) -> None:
        restart_commands = [
            [*self.sudo_prefix, "systemctl", "restart", self.service_name],
            [*self.sudo_prefix, "service", self.service_name, "restart"],
        ]
        last_error: Optional[RuntimeError] = None
        for command in restart_commands:
            try:
                _run_command(command, cwd=self.repo_root)
                return
            except subprocess.CalledProcessError as exc:
                last_error = RuntimeError(
                    f"Не удалось перезапустить {self.service_name} командой {' '.join(command)}:\n{exc.stdout}\n{exc.stderr}"
                )
        if last_error is not None:
            raise last_error

    def _build_report(
        self,
        *,
        started_at: datetime,
        calibration_runtime: float,
        benchmark_rounds: int,
        results: dict[str, ModeResult],
    ) -> dict[str, object]:
        without_plugin = results.get("without_plugin")
        with_plugin = results.get("with_plugin")
        comparison: Dict[str, Optional[float]] = {
            "sql_delta_sec": None,
            "sql_delta_pct": None,
            "ingest_delta_sec": None,
            "ingest_delta_pct": None,
        }
        if without_plugin is not None and with_plugin is not None:
            comparison["sql_delta_sec"] = with_plugin.avg_sql_runtime_sec - without_plugin.avg_sql_runtime_sec
            comparison["sql_delta_pct"] = (
                (comparison["sql_delta_sec"] / without_plugin.avg_sql_runtime_sec) * 100.0
                if without_plugin.avg_sql_runtime_sec
                else None
            )
            if with_plugin.avg_ingest_runtime_sec is not None:
                comparison["ingest_delta_sec"] = with_plugin.avg_ingest_runtime_sec

        plugin_settings_payload = {
            "spool_dir": str(self.plugin_settings.spool_dir),
            "flush_interval_sec": self.plugin_settings.flush_interval_sec,
            "debug_log_path": str(self.plugin_settings.debug_log_path) if self.plugin_settings.debug_log_path else None,
        }

        return {
            "started_at": started_at.isoformat(),
            "target_runtime_sec": self.args.target_runtime_sec,
            "calibration_rounds": self.args.calibration_rounds,
            "calibration_runtime_sec": calibration_runtime,
            "benchmark_rounds_per_client": benchmark_rounds,
            "client_count": self.args.client_count,
            "procedure_count": self.args.procedure_count,
            "iterations": self.args.iterations,
            "workspace_root": str(self.workspace_root),
            "plugin_settings": plugin_settings_payload,
            "results": {mode: asdict(result) for mode, result in results.items()},
            "comparison": comparison,
        }

    def _write_report_files(self, *, timestamp: str, report: dict[str, object]) -> dict[str, str]:
        serialized = json.dumps(report, indent=2)
        preferred_dir = self.results_dir
        fallback_dir = self.workspace_root / "results"

        for directory in (preferred_dir, fallback_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                timestamped_path = directory / f"{timestamp}.json"
                latest_path = directory / "latest.json"
                timestamped_path.write_text(serialized, encoding="utf-8")
                latest_path.write_text(serialized, encoding="utf-8")
                return {
                    "timestamped": str(timestamped_path),
                    "latest": str(latest_path),
                }
            except PermissionError:
                continue

        raise PermissionError("Не удалось записать benchmark report ни в bench/results, ни в workspace/results")

    def _print_report(self, report: dict[str, object]) -> None:
        print("Mode            Avg SQL (s)   Min SQL (s)   Avg Ingest (s)   Valid/Total")
        print("-----------------------------------------------------------------------")
        for mode, payload in report["results"].items():
            iterations = payload["iterations"]
            valid_count = sum(1 for item in iterations if item["valid"])
            avg_ingest = payload["avg_ingest_runtime_sec"]
            avg_ingest_display = f"{avg_ingest:>14.3f}" if avg_ingest is not None else f"{'-':>14}"
            print(
                f"{mode:<15} {payload['avg_sql_runtime_sec']:>11.3f} {payload['min_sql_runtime_sec']:>13.3f}"
                f" {avg_ingest_display} {valid_count:>5}/{len(iterations):<5}"
            )

        comparison = report["comparison"]
        if comparison["sql_delta_sec"] is not None:
            print("")
            print(
                "SQL overhead vs baseline: "
                f"{comparison['sql_delta_sec']:.3f}s ({comparison['sql_delta_pct']:.2f}%)"
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Firebird benchmark with and without ProcUsageTrace.")
    parser.add_argument("--mode", choices=["without_plugin", "with_plugin", "both"], default="both")
    parser.add_argument("--workspace-root", default="/tmp/firebird_proc_usage_benchmark")
    parser.add_argument("--client-count", type=int, default=64)
    parser.add_argument("--procedure-count", type=int, default=1000)
    parser.add_argument("--target-runtime-sec", type=int, default=150)
    parser.add_argument("--calibration-rounds", type=int, default=20)
    parser.add_argument("--min-rounds-per-client", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--user", default=os.environ.get("ISC_USER"))
    parser.add_argument("--password", default=os.environ.get("ISC_PASSWORD"))
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--plugin-name", default=DEFAULT_PLUGIN_NAME)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3050)
    parser.add_argument("--firebird-conf", default=str(DEFAULT_FIREBIRD_CONF))
    parser.add_argument("--plugin-conf", default=str(DEFAULT_PLUGIN_CONF))
    parser.add_argument("--audit-trace-conf", default=str(DEFAULT_AUDIT_TRACE_CONF))
    parser.add_argument("--sudo-command", default="sudo")
    parser.add_argument("--skip-plugin-binary-check", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    runner = FirebirdBenchmarkRunner(args)
    runner.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
