"""doc_decode 전용 checkpoint dir — 페이지별 상태 보존.

경로: ~/.cache/lecture-pipeline/composite/doc_decode/<run_id>/
구조:
  profile.md
  pages/
    page_001/
      page.png              # 원본 렌더 (재사용)
      objects.json          # SSOT (agent 가 갱신)
      annotated.png         # 박스 오버레이
      crops/<id>.png
      renders/<id>.png      # equation 렌더 (검증 결과물)
  doc.md / structure.json   # assemble 결과
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


CACHE_ROOT = Path.home() / ".cache" / "lecture-pipeline" / "composite" / "doc_decode"


class CheckpointDir:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.path = CACHE_ROOT / run_id
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "pages").mkdir(exist_ok=True)

    def page_dir(self, page_idx: int) -> Path:
        d = self.path / "pages" / f"page_{page_idx:03d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def file(self, name: str) -> Path:
        return self.path / name

    def has_file(self, name: str) -> bool:
        return (self.path / name).exists()

    def write_text(self, name: str, text: str) -> None:
        (self.path / name).write_text(text, encoding="utf-8")

    def read_text(self, name: str) -> str:
        return (self.path / name).read_text(encoding="utf-8")

    def write_bytes(self, name: str, data: bytes) -> None:
        (self.path / name).write_bytes(data)

    def read_bytes(self, name: str) -> bytes:
        return (self.path / name).read_bytes()


def compute_run_id(pdf_path: Path, cfg_keys: dict | None = None) -> str:
    """PDF 파일 + 핵심 config 해시 → 16자 run_id.

    cfg_keys 에 들어간 키 값이 바뀌면 새 run_id → 캐시 무효화.
    """
    h = hashlib.sha1()
    h.update(pdf_path.name.encode("utf-8"))
    h.update(b"\0")
    try:
        h.update(pdf_path.read_bytes())
    except OSError:
        h.update(str(pdf_path).encode("utf-8"))
    if cfg_keys:
        h.update(b"\n--cfg--\n")
        h.update(json.dumps(cfg_keys, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]
