"""Hash 기반 figure 캐시.

재현성의 본질: 같은 (hint, context, style, format, mode, seed) → 같은 key →
이전 산출물 재사용. Gemini image gen 의 stochastic 성질과 Codex temperature>0
문제를 캐시로 회피한다.

캐시 위치: `<workspace>/generated_figures/cache/<key>.{png,tex,source.py,source.tex,meta.json}`
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


CACHE_SUBDIR = "cache"


def cache_dir(workspace: Path) -> Path:
    return workspace / "generated_figures" / CACHE_SUBDIR


def compute_key(spec_dict: dict) -> str:
    """Deterministic 16-char hash of the canonical spec dict."""
    # 순서 무관 + 유니코드 안전
    canonical = json.dumps(spec_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def lookup(
    workspace: Path,
    key: str,
    expected_files: Iterable[str],
) -> dict[str, bytes] | None:
    """expected_files 가 전부 cache 에 존재하면 bytes dict 로 반환, 아니면 None."""
    d = cache_dir(workspace)
    results: dict[str, bytes] = {}
    for name in expected_files:
        p = d / f"{key}.{name}"
        if not p.exists():
            return None
        results[name] = p.read_bytes()
    return results


def store(
    workspace: Path,
    key: str,
    outputs: dict[str, bytes],
) -> Path:
    """cache 에 outputs 기록. 반환: cache_dir 경로."""
    d = cache_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in outputs.items():
        p = d / f"{key}.{name}"
        p.write_bytes(data)
    return d
