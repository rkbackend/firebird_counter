from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ServiceConfig:
    spool_dir: Path
    sqlite_db_path: Path
    poll_interval_sec: int = 5


def load_service_config(path: Path) -> ServiceConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ServiceConfig(
        spool_dir=Path(payload["spool_dir"]),
        sqlite_db_path=Path(payload["sqlite_db_path"]),
        poll_interval_sec=int(payload.get("poll_interval_sec", 5)),
    )

