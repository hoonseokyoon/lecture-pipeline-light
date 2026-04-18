"""objects.json 읽기/쓰기 + id 발급.

detect/annotate 스크립트의 공통 관심사.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path


SSOT_FILENAME = "objects.json"


def _iso_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_state(outputs_dir: Path, inputs_dir: Path | None = None) -> dict:
    """outputs/objects.json 을 우선, 없으면 inputs/objects.json 을 seed 로 사용.

    새 run 이면 skeleton 생성.
    """
    target = outputs_dir / SSOT_FILENAME
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))
    if inputs_dir is not None:
        seed = inputs_dir / SSOT_FILENAME
        if seed.exists():
            data = json.loads(seed.read_text(encoding="utf-8"))
            return data
    return {
        "page": None,
        "page_size": [0, 0],
        "objects": [],
    }


def save_state(outputs_dir: Path, state: dict) -> None:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / SSOT_FILENAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def next_obj_id(state: dict, page_idx: int) -> str:
    """p{page_idx}_NNN 패턴으로 다음 id 발급."""
    existing = state.get("objects", [])
    max_n = 0
    prefix = f"p{page_idx}_"
    for obj in existing:
        oid = str(obj.get("id", ""))
        if oid.startswith(prefix):
            try:
                n = int(oid[len(prefix):])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return f"{prefix}{max_n + 1:03d}"


def stamp_now() -> str:
    return _iso_now()


def find_obj(state: dict, obj_id: str) -> dict | None:
    for obj in state.get("objects", []):
        if obj.get("id") == obj_id:
            return obj
    return None
