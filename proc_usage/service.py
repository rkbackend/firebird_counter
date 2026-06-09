from __future__ import annotations

import threading
from dataclasses import dataclass

from proc_usage.config import ServiceConfig
from proc_usage.spool import SpoolIngestor
from proc_usage.storage import SQLiteUsageStorage


@dataclass
class ProcUsageService:
    """Долгоживущий сервис, который переносит агрегаты из spool-файлов в SQLite."""

    config: ServiceConfig
    storage: SQLiteUsageStorage
    ingestor: SpoolIngestor

    @classmethod
    def from_config(cls, config: ServiceConfig) -> "ProcUsageService":
        """Собирает рабочий экземпляр сервиса из конфигурации."""
        storage = SQLiteUsageStorage(config.sqlite_db_path)
        ingestor = SpoolIngestor(config.spool_dir)
        return cls(config=config, storage=storage, ingestor=ingestor)

    def initialize(self) -> None:
        """Готовит окружение сервиса перед первым циклом обработки."""

        # Каталог spool создаём заранее, чтобы Firebird-плагин и Python-сервис
        # работали с одной и той же директорией без ручной подготовки.
        self.config.spool_dir.mkdir(parents=True, exist_ok=True)
        # Схему SQLite тоже поднимаем заранее: так первый проход ingest не
        # зависит от того, создавал ли кто-то БД отдельной командой.
        self.storage.initialize()

    def ingest_pending_files(self) -> int:
        """Забирает все доступные spool-файлы и применяет их в SQLite."""

        processed_count = 0

        # Сначала пытаемся "захватить" файл через переименование в `.processing`.
        # Это простой способ пометить его как файл в работе и не дать другому
        # процессу подобрать тот же самый `.jsonl` повторно.
        #
        # Если сервис остановится посреди обработки, `.processing` останется на
        # диске. На следующем запуске мы снова увидим этот файл, сверим его с
        # таблицей `processed_spool_files` и либо безопасно дообработаем, либо
        # удалим как уже применённый.
        for claimed_file in self.ingestor.claim_files(self.storage.has_processed_spool_file):
            # Целиком читаем JSONL-файл в набор записей. Каждая запись уже
            # содержит не сырые времена отдельных вызовов, а готовые агрегаты
            # за час: count / total / min / max.
            records = list(self.ingestor.read_records(claimed_file.path))
            # `apply_spool_file` атомарно делает две вещи:
            # 1. обновляет агрегаты по процедурам и SQL;
            # 2. фиксирует, что именно этот spool-файл уже был применён.
            #
            # Если файл уже был обработан раньше, метод вернёт False и повторной
            # записи в статистику не произойдёт.
            applied = self.storage.apply_spool_file(claimed_file.state, records)
            # После успешной или уже подтверждённой обработки убираем временный
            # файл, чтобы каталог spool не разрастался бесконечно.
            self.ingestor.mark_processed(claimed_file)
            if applied:
                processed_count += 1

        return processed_count

    def serve_forever(self, stop_event: threading.Event) -> None:
        """Крутит бесконечный цикл ingest, пока не придёт сигнал остановки."""

        while not stop_event.is_set():
            # Один проход забирает всё, что накопилось к текущему моменту.
            self.ingest_pending_files()
            # Затем сервис "засыпает" на poll_interval_sec, но просыпается
            # раньше, если снаружи выставят stop_event.
            stop_event.wait(self.config.poll_interval_sec)
