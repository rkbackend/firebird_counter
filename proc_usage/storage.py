from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from proc_usage.spool import SpoolFileState, SpoolRecord, group_records


class SQLiteUsageStorage:
    """Слой доступа к SQLite для почасовых агрегатов по процедурам и SQL."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        """Открывает соединение с БД и настраивает строки как dict-like Row."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Создаёт таблицы, если БД ещё пустая."""

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
            # Для процедур и SQL используются две отдельные таблицы с очень
            # похожей схемой. Это упрощает запросы и не заставляет держать
            # nullable-колонки под разные типы сущностей.
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
            # Полный текст SQL храним отдельно от почасовых агрегатов, чтобы не
            # дублировать длинные строки в каждой строке статистики за каждый час.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sql_text_catalog (
                    fingerprint TEXT NOT NULL,
                    sql_text TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (fingerprint)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sql_text_usage_stats (
                    usage_hour TEXT NOT NULL,
                    database TEXT NOT NULL,
                    sql_fingerprint TEXT NOT NULL,
                    total_calls INTEGER NOT NULL,
                    total_time_ms INTEGER NOT NULL,
                    min_time_ms INTEGER NOT NULL,
                    max_time_ms INTEGER NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (usage_hour, database, sql_fingerprint),
                    FOREIGN KEY (sql_fingerprint) REFERENCES sql_text_catalog(fingerprint)
                )
                """
            )
            # Здесь храним "отпечатки" уже применённых spool-файлов. Таблица
            # нужна, чтобы один и тот же файл не увеличил статистику дважды,
            # даже если сервис перезапустился или файл повторно появился.
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
        """Применяет набор записей напрямую, без учёта spool-файла."""

        grouped = group_records(records)
        if not grouped:
            return

        with self._connect() as connection:
            self._apply_grouped_records(connection, grouped.values())

    def has_processed_spool_file(self, file_state: SpoolFileState) -> bool:
        """Проверяет, применяли ли мы уже конкретный spool-файл."""

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
        """Атомарно применяет файл и помечает его как обработанный."""

        grouped = group_records(records)

        with self._connect() as connection:
            # Сначала делаем защиту от дубля на уровне той же транзакции.
            # Если запись о файле уже есть, значит его содержимое ранее было
            # применено и второй раз статистику менять нельзя.
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

            # Только после проверки обновляем агрегаты и фиксируем сам файл как
            # обработанный. Так обе операции живут вместе и не расходятся.
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
        """Возвращает самые "тяжёлые" агрегаты по вызовам и времени."""

        normalized_kind = self._normalize_kind(kind)
        if normalized_kind == "sql_text":
            return self.top_sql_texts(limit=limit, usage_hour=usage_hour)

        table_name, name_column = self._table_for_kind(normalized_kind)
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
                    -- Среднее не храним отдельно, а считаем на чтении из уже
                    -- накопленных total_time_ms и total_calls.
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
        """Возвращает статистику по конкретной процедуре или виду SQL."""

        normalized_kind = self._normalize_kind(kind)
        if normalized_kind == "sql_text":
            return self.sql_text_stats(name, database=database, usage_hour=usage_hour)

        table_name, name_column = self._table_for_kind(normalized_kind)
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
                    -- Здесь среднее тоже вычисляется динамически, чтобы не
                    -- хранить в таблице ещё одно производное поле.
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

    def top_sql_texts(self, limit: int = 10, usage_hour: Optional[str] = None) -> list[sqlite3.Row]:
        """Возвращает самые "тяжёлые" агрегаты по полному SQL-тексту."""

        where_clause, parameters = self._hour_filter_clause(usage_hour, table_alias="stats")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    stats.usage_hour,
                    stats.database,
                    stats.sql_fingerprint AS name,
                    catalog.sql_text,
                    stats.total_calls,
                    stats.total_time_ms,
                    stats.min_time_ms,
                    stats.max_time_ms,
                    CASE
                        WHEN stats.total_calls > 0 THEN CAST(stats.total_time_ms AS REAL) / stats.total_calls
                        ELSE 0
                    END AS avg_time_ms,
                    stats.last_seen_at
                FROM sql_text_usage_stats AS stats
                JOIN sql_text_catalog AS catalog
                  ON catalog.fingerprint = stats.sql_fingerprint
                {where_clause}
                ORDER BY stats.usage_hour DESC, stats.total_calls DESC, stats.total_time_ms DESC, stats.database ASC, name ASC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        return rows

    def sql_text_stats(
        self,
        sql_fingerprint: str,
        database: Optional[str] = None,
        usage_hour: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """Возвращает почасовую статистику по одному полному SQL-тексту."""

        conditions = ["stats.sql_fingerprint = ?"]
        parameters: list[object] = [sql_fingerprint]

        if database is not None:
            conditions.append("stats.database = ?")
            parameters.append(database)

        if usage_hour is not None:
            conditions.append("stats.usage_hour = ?")
            parameters.append(usage_hour)

        where_clause = " AND ".join(conditions)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    stats.usage_hour,
                    stats.database,
                    stats.sql_fingerprint AS name,
                    catalog.sql_text,
                    stats.total_calls,
                    stats.total_time_ms,
                    stats.min_time_ms,
                    stats.max_time_ms,
                    CASE
                        WHEN stats.total_calls > 0 THEN CAST(stats.total_time_ms AS REAL) / stats.total_calls
                        ELSE 0
                    END AS avg_time_ms,
                    stats.last_seen_at
                FROM sql_text_usage_stats AS stats
                JOIN sql_text_catalog AS catalog
                  ON catalog.fingerprint = stats.sql_fingerprint
                WHERE {where_clause}
                ORDER BY stats.usage_hour DESC, stats.total_calls DESC, stats.total_time_ms DESC, stats.database ASC
                """,
                parameters,
            ).fetchall()

        return rows

    def sql_text_by_fingerprint(self, sql_fingerprint: str) -> Optional[str]:
        """Возвращает полный SQL-текст по его fingerprint, если он известен."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sql_text
                FROM sql_text_catalog
                WHERE fingerprint = ?
                """,
                (sql_fingerprint,),
            ).fetchone()
        return None if row is None else str(row["sql_text"])

    def _apply_grouped_records(self, connection: sqlite3.Connection, records: Iterable[SpoolRecord]) -> None:
        """Upsert-ит агрегаты в нужную таблицу в рамках одной транзакции."""

        for record in records:
            normalized_kind = self._normalize_kind(record.kind)
            if normalized_kind == "sql_text":
                self._apply_sql_text_record(connection, record)
                continue

            table_name, name_column = self._table_for_kind(normalized_kind)
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
                    -- Новые данные за тот же час не заменяют старые, а
                    -- наращивают счётчики и общее время.
                    total_calls = {table_name}.total_calls + excluded.total_calls,
                    total_time_ms = {table_name}.total_time_ms + excluded.total_time_ms,
                    -- Экстремумы должны отражать весь час целиком.
                    min_time_ms = MIN({table_name}.min_time_ms, excluded.min_time_ms),
                    max_time_ms = MAX({table_name}.max_time_ms, excluded.max_time_ms),
                    -- Для отладки и просмотра полезно знать последнюю метку
                    -- времени, когда по этому ключу пришли данные.
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

    def _apply_sql_text_record(self, connection: sqlite3.Connection, record: SpoolRecord) -> None:
        """Сохраняет полный SQL в каталог и обновляет его почасовой агрегат."""

        if not record.name:
            return

        fingerprint = self._sql_text_fingerprint(record.name)
        timestamp = record.ts.isoformat()

        connection.execute(
            """
            INSERT INTO sql_text_catalog (
                fingerprint,
                sql_text,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen_at = CASE
                    WHEN excluded.last_seen_at > sql_text_catalog.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE sql_text_catalog.last_seen_at
                END
            """,
            (fingerprint, record.name, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO sql_text_usage_stats (
                usage_hour,
                database,
                sql_fingerprint,
                total_calls,
                total_time_ms,
                min_time_ms,
                max_time_ms,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(usage_hour, database, sql_fingerprint) DO UPDATE SET
                total_calls = sql_text_usage_stats.total_calls + excluded.total_calls,
                total_time_ms = sql_text_usage_stats.total_time_ms + excluded.total_time_ms,
                min_time_ms = MIN(sql_text_usage_stats.min_time_ms, excluded.min_time_ms),
                max_time_ms = MAX(sql_text_usage_stats.max_time_ms, excluded.max_time_ms),
                last_seen_at = CASE
                    WHEN excluded.last_seen_at > sql_text_usage_stats.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE sql_text_usage_stats.last_seen_at
                END
            """,
            (
                record.hour,
                record.db,
                fingerprint,
                record.count,
                record.total_time_ms,
                record.min_time_ms,
                record.max_time_ms,
                timestamp,
            ),
        )

    def _hour_filter_clause(self, usage_hour: Optional[str], table_alias: Optional[str] = None) -> tuple[str, tuple[object, ...]]:
        """Формирует WHERE по часу только когда фильтр действительно задан."""

        if usage_hour is None:
            return ("", ())
        prefix = "" if table_alias is None else f"{table_alias}."
        return (f"WHERE {prefix}usage_hour = ?", (usage_hour,))

    def _sql_text_fingerprint(self, sql_text: str) -> str:
        """Строит стабильный fingerprint для полного текста SQL."""

        return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()

    def _normalize_kind(self, kind: str) -> str:
        """Приводит внешние имена видов статистики к внутреннему формату."""

        if kind == "sql-text":
            return "sql_text"
        return kind

    def _table_for_kind(self, kind: str) -> tuple[str, str]:
        """Сопоставляет логический тип статистики с таблицей и именем колонки."""

        if kind == "procedure":
            return ("procedure_usage_stats", "procedure")
        if kind == "sql":
            return ("sql_usage_stats", "sql_kind")
        if kind == "sql_text":
            return ("sql_text_usage_stats", "sql_fingerprint")
        raise ValueError(f"Unsupported usage kind: {kind}")
