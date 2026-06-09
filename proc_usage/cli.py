from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path
from typing import Optional

from proc_usage.config import ServiceConfig, load_service_config
from proc_usage.service import ProcUsageService
from proc_usage.storage import SQLiteUsageStorage


def _config_from_args(args: argparse.Namespace) -> ServiceConfig:
    if args.config:
        return load_service_config(Path(args.config))
    return ServiceConfig(
        spool_dir=Path(args.spool_dir),
        sqlite_db_path=Path(args.sqlite_db_path),
        poll_interval_sec=args.poll_interval_sec,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proc-usage")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument("--config", help="Path to a JSON service config")
    config_parent.add_argument("--spool-dir", default="/tmp/firebird_proc_usage_spool")
    config_parent.add_argument("--sqlite-db-path", default="./var/proc_usage.sqlite3")
    config_parent.add_argument("--poll-interval-sec", type=int, default=5)

    init_db = subparsers.add_parser("init-db", parents=[config_parent], help="Create the SQLite schema")
    init_db.set_defaults(handler=handle_init_db)

    ingest_once = subparsers.add_parser("ingest-once", parents=[config_parent], help="Process pending spool files once")
    ingest_once.set_defaults(handler=handle_ingest_once)

    serve = subparsers.add_parser("serve", parents=[config_parent], help="Run the long-lived ingestion service")
    serve.set_defaults(handler=handle_serve)

    top = subparsers.add_parser("top", parents=[config_parent], help="Show the busiest procedures or SQL kinds")
    top.add_argument("--limit", type=int, default=10)
    top.add_argument("--kind", choices=["procedure", "sql"], default="procedure")
    top.add_argument("--hour", help="Optional UTC hour filter in YYYY-MM-DDTHH:00Z format")
    top.set_defaults(handler=handle_top)

    show = subparsers.add_parser("show", parents=[config_parent], help="Show stats for one procedure or SQL kind")
    show.add_argument("name", help="Procedure name or SQL kind to inspect")
    show.add_argument("--database", help="Optional database filter")
    show.add_argument("--kind", choices=["procedure", "sql"], default="procedure")
    show.add_argument("--hour", help="Optional UTC hour filter in YYYY-MM-DDTHH:00Z format")
    show.set_defaults(handler=handle_show)

    dump_config = subparsers.add_parser("sample-config", help="Print a sample JSON config")
    dump_config.set_defaults(handler=handle_sample_config)

    return parser


def handle_init_db(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    storage = SQLiteUsageStorage(config.sqlite_db_path)
    storage.initialize()
    print(f"Initialized SQLite database at {config.sqlite_db_path}")
    return 0


def handle_ingest_once(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    service = ProcUsageService.from_config(config)
    service.initialize()
    processed = service.ingest_pending_files()
    print(f"Processed {processed} spool file(s)")
    return 0


def handle_serve(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    service = ProcUsageService.from_config(config)
    service.initialize()

    stop_event = threading.Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    print("Starting procedure usage service. Press Ctrl+C to stop.")
    service.serve_forever(stop_event=stop_event)
    return 0


def handle_top(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    storage = SQLiteUsageStorage(config.sqlite_db_path)
    storage.initialize()

    for row in storage.top_usage(kind=args.kind, limit=args.limit, usage_hour=args.hour):
        print(_format_row(row))

    return 0


def handle_show(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    storage = SQLiteUsageStorage(config.sqlite_db_path)
    storage.initialize()

    rows = storage.usage_stats(
        kind=args.kind,
        name=args.name,
        database=args.database,
        usage_hour=args.hour,
    )
    if not rows:
        print("No statistics found.")
        return 0

    for row in rows:
        print(_format_row(row))

    return 0


def handle_sample_config(args: argparse.Namespace) -> int:
    _ = args
    sample = {
        "spool_dir": "/tmp/firebird_proc_usage_spool",
        "sqlite_db_path": "./var/proc_usage.sqlite3",
        "poll_interval_sec": 5,
    }
    print(json.dumps(sample, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _format_row(row: object) -> str:
    avg_time_ms = float(row["avg_time_ms"])
    return (
        f"{row['total_calls']:>10}  "
        f"{row['usage_hour']}  "
        f"{row['database']}  "
        f"{row['name']}  "
        f"min={row['min_time_ms']}ms  "
        f"avg={avg_time_ms:.2f}ms  "
        f"max={row['max_time_ms']}ms  "
        f"{row['last_seen_at']}"
    )
