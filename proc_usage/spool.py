from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator


@dataclass
class SpoolRecord:
    ts: datetime
    db: str
    proc: str
    delta: int


@dataclass(frozen=True)
class SpoolFileState:
    path: str
    size_bytes: int
    mtime_ns: int


@dataclass
class ClaimedSpoolFile:
    path: Path
    state: SpoolFileState
    delete_after_processing: bool


class SpoolIngestor:
    def __init__(self, spool_dir: Path) -> None:
        self.spool_dir = spool_dir

    def claim_files(self, is_processed: Callable[[SpoolFileState], bool]) -> list[ClaimedSpoolFile]:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        claimed: list[ClaimedSpoolFile] = []

        # Leftover ".processing" files are retried first because they indicate the
        # previous service run crashed after claiming but before final cleanup.
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
                continue
            except PermissionError:
                # In sticky or shared directories Firebird may create files we can read
                # but cannot rename. In that case we process the file in place and let
                # SQLite deduplicate by file fingerprint.
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
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            payload = json.loads(line)
            yield SpoolRecord(
                ts=datetime.fromisoformat(payload["ts"].replace("Z", "+00:00")),
                db=str(payload["db"]),
                proc=str(payload["proc"]),
                delta=int(payload["delta"]),
            )

    def mark_processed(self, claimed_file: ClaimedSpoolFile) -> None:
        if not claimed_file.delete_after_processing:
            return

        claimed_file.path.unlink(missing_ok=True)

    def describe_file(self, path: Path) -> SpoolFileState:
        stat_result = path.stat()
        return SpoolFileState(
            path=str(path.resolve()),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )


def group_records(records: Iterable[SpoolRecord]) -> dict[tuple[str, str], SpoolRecord]:
    grouped: dict[tuple[str, str], SpoolRecord] = {}

    for record in records:
        key = (record.db, record.proc)
        current = grouped.get(key)

        if current is None:
            grouped[key] = SpoolRecord(ts=record.ts, db=record.db, proc=record.proc, delta=record.delta)
            continue

        current.delta += record.delta
        if record.ts > current.ts:
            current.ts = record.ts

    return grouped
