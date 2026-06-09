from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator


@dataclass
class SpoolRecord:
    """Одна агрегированная запись, которую плагин выгрузил в JSONL."""

    ts: datetime
    kind: str
    hour: str
    db: str
    name: str
    count: int
    total_time_ms: int
    min_time_ms: int
    max_time_ms: int


@dataclass(frozen=True)
class SpoolFileState:
    """Снимок состояния файла для защиты от повторного применения."""

    path: str
    size_bytes: int
    mtime_ns: int


@dataclass
class ClaimedSpoolFile:
    """Файл spool, который текущий процесс взял в обработку."""

    path: Path
    state: SpoolFileState
    delete_after_processing: bool


class SpoolIngestor:
    """Отвечает за чтение и безопасный захват spool-файлов."""

    def __init__(self, spool_dir: Path) -> None:
        self.spool_dir = spool_dir

    def claim_files(self, is_processed: Callable[[SpoolFileState], bool]) -> list[ClaimedSpoolFile]:
        """Собирает список файлов, которые можно обработать в текущем проходе."""

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        claimed: list[ClaimedSpoolFile] = []

        # Сначала подбираем "зависшие" `.processing` после прошлых запусков.
        # Если такой файл уже есть в SQLite как обработанный, удаляем его.
        # Иначе возвращаем в обработку, чтобы не потерять статистику.
        processing_files = sorted(self.spool_dir.glob("*.processing"))
        for path in processing_files:
            state = self.describe_file(path)
            if is_processed(state):
                path.unlink(missing_ok=True)
                continue

            claimed.append(
                ClaimedSpoolFile(
                    path=path,
                    state=state,
                    delete_after_processing=True,
                )
            )

        # Затем пытаемся захватить новые `.jsonl`. Переименование в
        # `.processing` работает как дешёвая блокировка на уровне файловой
        # системы: кто успел переименовать, тот и обрабатывает файл.
        for path in sorted(self.spool_dir.glob("*.jsonl")):
            target = path.with_suffix(path.suffix + ".processing")
            try:
                path.rename(target)
                state = self.describe_file(target)
                claimed.append(
                    ClaimedSpoolFile(
                        path=target,
                        state=state,
                        delete_after_processing=True,
                    )
                )
            except FileNotFoundError:
                # Файл мог исчезнуть между glob и rename, например если его уже
                # успел подобрать другой процесс. Это штатная гонка, пропускаем.
                continue
            except PermissionError:
                # На некоторых файловых системах или сценариях развёртывания
                # rename может быть недоступен. Тогда читаем файл без
                # переименования и полагаемся на таблицу processed_spool_files,
                # чтобы не применить его повторно.
                state = self.describe_file(path)
                if is_processed(state):
                    continue

                claimed.append(
                    ClaimedSpoolFile(
                        path=path,
                        state=state,
                        delete_after_processing=False,
                    )
                )

        return claimed

    def read_records(self, path: Path) -> Iterator[SpoolRecord]:
        """Читает JSONL-файл и преобразует строки в типизированные записи."""

        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            payload = json.loads(line)
            yield SpoolRecord(
                ts=datetime.fromisoformat(payload["ts"].replace("Z", "+00:00")),
                kind=str(payload["kind"]),
                hour=str(payload["hour"]),
                db=str(payload["db"]),
                name=str(payload["name"]),
                count=int(payload["count"]),
                total_time_ms=int(payload["total_time_ms"]),
                min_time_ms=int(payload["min_time_ms"]),
                max_time_ms=int(payload["max_time_ms"]),
            )

    def mark_processed(self, claimed_file: ClaimedSpoolFile) -> None:
        """Удаляет временный файл после завершения обработки."""

        if not claimed_file.delete_after_processing:
            # Если файл обрабатывался без переименования, трогать его нельзя:
            # он мог быть создан политикой доступа или внешним процессом.
            return

        claimed_file.path.unlink(missing_ok=True)

    def describe_file(self, path: Path) -> SpoolFileState:
        """Возвращает признаки файла, по которым мы узнаём его повторно."""

        stat_result = path.stat()
        return SpoolFileState(
            path=str(path.resolve()),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )


def group_records(records: Iterable[SpoolRecord]) -> dict[tuple[str, str, str, str], SpoolRecord]:
    """Склеивает записи с одинаковым ключом в один агрегат."""

    grouped: dict[tuple[str, str, str, str], SpoolRecord] = {}

    for record in records:
        # Ключ соответствует одной строке итоговой статистики:
        # вид сущности, час, база и имя процедуры/SQL-категории.
        key = (record.kind, record.hour, record.db, record.name)
        current = grouped.get(key)

        if current is None:
            grouped[key] = SpoolRecord(
                ts=record.ts,
                kind=record.kind,
                hour=record.hour,
                db=record.db,
                name=record.name,
                count=record.count,
                total_time_ms=record.total_time_ms,
                min_time_ms=record.min_time_ms,
                max_time_ms=record.max_time_ms,
            )
            continue

        # Повторные записи за тот же час не храним отдельно, а сразу
        # сворачиваем: суммируем количество и общее время, а min/max
        # обновляем экстремальными значениями.
        current.count += record.count
        current.total_time_ms += record.total_time_ms
        current.min_time_ms = min(current.min_time_ms, record.min_time_ms)
        current.max_time_ms = max(current.max_time_ms, record.max_time_ms)
        # `last_seen_at` в SQLite должен отражать самую позднюю точку времени,
        # из которой пришли данные по этому агрегату.
        if record.ts > current.ts:
            current.ts = record.ts

    return grouped
