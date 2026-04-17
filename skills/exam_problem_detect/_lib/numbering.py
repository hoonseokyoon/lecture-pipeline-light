"""페이지별 문제 번호 결정론적 부여.

정렬 키:
1. y-bucket (정규화 좌표 기준 2%씩 묶음 = bucket_size 20)
   → 다단 레이아웃/행 단위 정렬 허용치
2. 같은 bucket 내에선 x_center (좌→우)

ID 형태: "p{page}-q{n}" — 1-based.
"""

from __future__ import annotations


Box = tuple[float, float, float, float]


def assign_ids(
    page_num: int,
    reps: list[Box],
    bucket_size: float = 20.0,
) -> list[tuple[str, Box]]:
    """page_num 기반 id 부여. (id_str, box) 리스트 반환.

    reps: 각 cluster의 대표 박스 리스트 (입력 순서 무관).
    """
    def key(box: Box) -> tuple[int, float]:
        y_center = (box[1] + box[3]) / 2.0
        x_center = (box[0] + box[2]) / 2.0
        y_bucket = int(y_center // bucket_size) * int(bucket_size)
        return (y_bucket, x_center)

    sorted_reps = sorted(reps, key=key)
    return [(f"p{page_num}-q{i + 1}", b) for i, b in enumerate(sorted_reps)]
