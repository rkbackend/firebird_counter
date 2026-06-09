from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

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
                    usage_hour TEXT NOT NULL,
                    database TEXT NOT NULL,
                    procedure TEXT NOT NULL,
                    total_calls INTEGER NOT NULL,
                    total_time_ms INTEGER NOT NULL,
                    min_time_ms INTEGER NOT NULL,
                    max_time_ms INTEGER NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (usage_hour, database, procedure)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sql_usage_stats (
                    usage_hour TEXT NOT NULL,
                    database TEXT NOT NULL,
                    sql_kind TEXT NOT NULL,
                    total_calls INTEGER NOT NULL,
                    total_time_ms INTEGER NOT NULL,
                    min_time_ms INTEGER NOT NULL,
                    max_time_ms INTEGER NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (usage_hour, database, sql_kind)
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
            self._apply_grouped_records(connection, grouped.values())

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

            self._apply_grouped_records(connection, grouped.values())
            connection.execute(
                """
                INSERT INTO processed_spool_files (path, size_bytes, mtime_ns, processed_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (file_state.path, file_state.size_bytes, file_state.mtime_ns),
            )

        return True

    def top_usage(self, kind: str, limit: int = 10, usage_hour: Optional[str] = None) -> list[sqlite3.Row]:
        table_name, name_column = self._table_for_kind(kind)
        where_clause, parameters = self._hour_filter_clause(usage_hour)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    usage_hour,
                    database,
                    {name_column} AS name,
                    total_calls,
                    total_time_ms,
                    min_time_ms,
                    max_time_ms,
                    CASE
                        WHEN total_calls > 0 THEN CAST(total_time_ms AS REAL) / total_calls
                        ELSE 0
                    END AS avg_time_ms,
                    last_seen_at
                FROM {table_name}
                {where_clause}
                ORDER BY usage_hour DESC, total_calls DESC, total_time_ms DESC, database ASC, name ASC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        return rows

    def usage_stats(
        self,
        kind: str,
        name: str,
        database: Optional[str] = None,
        usage_hour: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        table_name, name_column = self._table_for_kind(kind)
        conditions = [f"{name_column} = ?"]
        parameters: list[object] = [name]

        if database is not None:
            conditions.append("database = ?")
            parameters.append(database)

        if usage_hour is not None:
            conditions.append("usage_hour = ?")
            parameters.append(usage_hour)

        where_clause = " AND ".join(conditions)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    usage_hour,
                    database,
                    {name_column} AS name,
                    total_calls,
                    total_time_ms,
                    min_time_ms,
                    max_time_ms,
                    CASE
                        WHEN total_calls > 0 THEN CAST(total_time_ms AS REAL) / total_calls
                        ELSE 0
                    END AS avg_time_ms,
                    last_seen_at
                FROM {table_name}
                WHERE {where_clause}
                ORDER BY usage_hour DESC, total_calls DESC, total_time_ms DESC, database ASC
                """
                ,
                parameters,
            ).fetchall()

        return rows

    def top_procedures(self, limit: int = 10, usage_hour: Optional[str] = None) -> list[sqlite3.Row]:
        return self.top_usage(kind="procedure", limit=limit, usage_hour=usage_hour)

    def procedure_stats(
        self,
        procedure: str,
        database: Optional[str] = None,
        usage_hour: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        return self.usage_stats(kind="procedure", name=procedure, database=database, usage_hour=usage_hour)

    def top_sql(self, limit: int = 10, usage_hour: Optional[str] = None) -> list[sqlite3.Row]:
        return self.top_usage(kind="sql", limit=limit, usage_hour=usage_hour)

    def sql_stats(
        self,
        sql_kind: str,
        database: Optional[str] = None,
        usage_hour: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        return self.usage_stats(kind="sql", name=sql_kind, database=database, usage_hour=usage_hour)

    def _apply_grouped_records(self, connection: sqlite3.Connection, records: Iterable[SpoolRecord]) -> None:
        for record in records:
            table_name, name_column = self._table_for_kind(record.kind)
            connection.execute(
                f"""
                INSERT INTO {table_name} (
                    usage_hour,
                    database,
                    {name_column},
                    total_calls,
                    total_time_ms,
                    min_time_ms,
                    max_time_ms,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_hour, database, {name_column}) DO UPDATE SET
                    total_calls = {table_name}.total_calls + excluded.total_calls,
                    total_time_ms = {table_name}.total_time_ms + excluded.total_time_ms,
                    min_time_ms = MIN({table_name}.min_time_ms, excluded.min_time_ms),
                    max_time_ms = MAX({table_name}.max_time_ms, excluded.max_time_ms),
                    last_seen_at = CASE
                        WHEN excluded.last_seen_at > {table_name}.last_seen_at
                        THEN excluded.last_seen_at
                        ELSE {table_name}.last_seen_at
                    END
                """,
                (
                    record.hour,
                    record.db,
                    record.name,
                    record.count,
                    record.total_time_ms,
                    record.min_time_ms,
                    record.max_time_ms,
                    record.ts.isoformat(),
                ),
            )

    def _hour_filter_clause(self, usage_hour: Optional[str]) -> tuple[str, tuple[object, ...]]:
        if usage_hour is None:
            return ("", ())
        return ("WHERE usage_hour = ?", (usage_hour,))

    def _table_for_kind(self, kind: str) -> tuple[str, str]:
        if kind == "procedure":
            return ("procedure_usage_stats", "procedure")
        if kind == "sql":
            return ("sql_usage_stats", "sql_kind")
        raise ValueError(f"Unsupported usage kind: {kind}")
