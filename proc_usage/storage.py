from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from proc_usage.spool import SpoolFileState, SpoolRecord, group_records


class SQLiteUsageStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS procedure_usage_stats (
                    database TEXT NOT NULL,
                    procedure TEXT NOT NULL,
                    total_calls INTEGER NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (database, procedure)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_spool_files (
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (path, size_bytes, mtime_ns)
                )
                """
            )

    def apply_deltas(self, records: Iterable[SpoolRecord]) -> None:
        grouped = group_records(records)
        if not grouped:
            return

        with self._connect() as connection:
            for record in grouped.values():
                # This UPSERT keeps the storage append-free while still preserving
                # the latest timestamp observed for each database/procedure pair.
                connection.execute(
                    """
                    INSERT INTO procedure_usage_stats (database, procedure, total_calls, last_seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(database, procedure) DO UPDATE SET
                        total_calls = total_calls + excluded.total_calls,
                        last_seen_at = CASE
                            WHEN excluded.last_seen_at > procedure_usage_stats.last_seen_at
                            THEN excluded.last_seen_at
                            ELSE procedure_usage_stats.last_seen_at
                        END
                    """,
                    (
                        record.db,
                        record.proc,
                        record.delta,
                        record.ts.isoformat(),
                    ),
                )

    def has_processed_spool_file(self, file_state: SpoolFileState) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM processed_spool_files
                WHERE path = ? AND size_bytes = ? AND mtime_ns = ?
                """,
                (file_state.path, file_state.size_bytes, file_state.mtime_ns),
            ).fetchone()

        return row is not None

    def apply_spool_file(self, file_state: SpoolFileState, records: Iterable[SpoolRecord]) -> bool:
        grouped = group_records(records)

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT 1
                FROM processed_spool_files
                WHERE path = ? AND size_bytes = ? AND mtime_ns = ?
                """,
                (file_state.path, file_state.size_bytes, file_state.mtime_ns),
            ).fetchone()
            if existing is not None:
                return False

            for record in grouped.values():
                connection.execute(
                    """
                    INSERT INTO procedure_usage_stats (database, procedure, total_calls, last_seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(database, procedure) DO UPDATE SET
                        total_calls = total_calls + excluded.total_calls,
                        last_seen_at = CASE
                            WHEN excluded.last_seen_at > procedure_usage_stats.last_seen_at
                            THEN excluded.last_seen_at
                            ELSE procedure_usage_stats.last_seen_at
                        END
                    """,
                    (
                        record.db,
                        record.proc,
                        record.delta,
                        record.ts.isoformat(),
                    ),
                )

            connection.execute(
                """
                INSERT INTO processed_spool_files (path, size_bytes, mtime_ns, processed_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (file_state.path, file_state.size_bytes, file_state.mtime_ns),
            )

        return True

    def top_procedures(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT database, procedure, total_calls, last_seen_at
                FROM procedure_usage_stats
                ORDER BY total_calls DESC, database ASC, procedure ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    def procedure_stats(self, procedure: str, database: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as connection:
            if database is None:
                rows = connection.execute(
                    """
                    SELECT database, procedure, total_calls, last_seen_at
                    FROM procedure_usage_stats
                    WHERE procedure = ?
                    ORDER BY total_calls DESC, database ASC
                    """,
                    (procedure,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT database, procedure, total_calls, last_seen_at
                    FROM procedure_usage_stats
                    WHERE procedure = ? AND database = ?
                    ORDER BY total_calls DESC, database ASC
                    """,
                    (procedure, database),
                ).fetchall()

        return rows
