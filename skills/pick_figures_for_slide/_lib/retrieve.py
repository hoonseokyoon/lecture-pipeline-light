"""Cosine similarity top-K retrieval."""

from __future__ import annotations

import numpy as np


def top_k_by_cosine(
    *,
    query: list[float] | np.ndarray,
    asset_embeddings: np.ndarray,
    asset_names: list[str],
    captions: dict[str, str] | None = None,
    k: int = 5,
) -> list[dict]:
    """query 와 asset 들 사이의 cosine similarity 상위 k 반환.

    반환: [{"filename": str, "caption": str, "score": float}, ...] 내림차순.
    """
    if asset_embeddings.size == 0 or not asset_names:
        return []

    q = np.asarray(query, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm

    # 문서 벡터 정규화 (0 벡터는 스킵)
    norms = np.linalg.norm(asset_embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    doc = asset_embeddings / norms

    scores = doc @ q  # (N,)
    order = np.argsort(-scores)[:k]

    out = []
    for idx in order:
        name = asset_names[idx]
        out.append({
            "filename": name,
            "caption": (captions or {}).get(name, ""),
            "score": float(scores[idx]),
        })
    return out
