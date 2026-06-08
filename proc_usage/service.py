from __future__ import annotations

import threading
from dataclasses import dataclass

from proc_usage.config import ServiceConfig
from proc_usage.spool import SpoolIngestor
from proc_usage.storage import SQLiteUsageStorage


@dataclass
class ProcUsageService:
    config: ServiceConfig
    storage: SQLiteUsageStorage
    ingestor: SpoolIngestor

    @classmethod
    def from_config(cls, config: ServiceConfig) -> "ProcUsageService":
        storage = SQLiteUsageStorage(config.sqlite_db_path)
        ingestor = SpoolIngestor(config.spool_dir)
        return cls(config=config, storage=storage, ingestor=ingestor)

    def initialize(self) -> None:
        self.config.spool_dir.mkdir(parents=True, exist_ok=True)
        self.storage.initialize()

    def ingest_pending_files(self) -> int:
        processed_count = 0

        # We claim files by renaming them to ".processing". That makes retries safe:
        # if the service dies mid-batch, the next run will see the leftover files and retry.
        for claimed_file in self.ingestor.claim_files(self.storage.has_processed_spool_file):
            records = list(self.ingestor.read_records(claimed_file.path))
            applied = self.storage.apply_spool_file(claimed_file.state, records)
            self.ingestor.mark_processed(claimed_file)
            if applied:
                processed_count += 1

        return processed_count

    def serve_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.ingest_pending_files()
            stop_event.wait(self.config.poll_interval_sec)
