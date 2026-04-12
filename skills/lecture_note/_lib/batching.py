"""배치 boundary 계산 및 run_id 해시 유틸."""

import hashlib
from pathlib import Path


def batch_ranges(
    num_pages: int,
    batch_size: int = 12,
    overlap: int = 3,
) -> list[tuple[int, int]]:
    """페이지 번호 1..num_pages에 대해 배치 범위 리스트를 반환.

    각 배치는 이전 배치와 `overlap`만큼 겹침. 앞 `overlap` 페이지는 재검토,
    뒤 `batch_size - overlap` 페이지는 신규 작업.

    예: num_pages=40, batch_size=12, overlap=3
      -> [(1,12), (10,21), (19,30), (28,39), (37,40)]

    예: num_pages=10, batch_size=12, overlap=3
      -> [(1,10)]

    예: num_pages=13, batch_size=12, overlap=3
      -> [(1,12), (10,13)]
    """
    if num_pages < 1:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if overlap < 0 or overlap >= batch_size:
        raise ValueError("overlap must satisfy 0 <= overlap < batch_size")

    batches: list[tuple[int, int]] = []
    start = 1
    while start <= num_pages:
        end = min(start + batch_size - 1, num_pages)
        batches.append((start, end))
        if end >= num_pages:
            break
        start = end - overlap + 1
    return batches


def compute_run_id(input_paths: list[Path]) -> str:
    """입력 파일들의 순서 + 이름 + 내용 sha1 기반 run_id (16자).

    **순서 민감**: `[A, B]` 와 `[B, A]` 는 다른 run_id.
    multi-PDF / multi-transcript 시나리오에서 파일 순서가 의미를 갖기 때문.
    """
    hasher = hashlib.sha1()
    for idx, p in enumerate(input_paths):
        p = Path(p)
        hasher.update(f"[{idx}]".encode("utf-8"))  # 순서 마커
        hasher.update(p.name.encode("utf-8"))
        hasher.update(b"\0")
        try:
            hasher.update(p.read_bytes())
        except OSError:
            hasher.update(str(p).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:16]
