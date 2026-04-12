"""Multi-PDF slides_textify 결과 flatten.

각 PDF별 `{"pages": [...]}` (1-based local index) 결과를 모아 하나의
글로벌 slides_data 로 합침. 글로벌 index 는 1..N_total 로 재번호.
원본 local_page 와 source_pdf 는 메타로 보존.
"""

from pathlib import Path


def flatten_slides(per_pdf_results: list[tuple[Path, dict]]) -> dict:
    """각 PDF의 pages 를 글로벌 index 로 reindex + 메타 부착.

    Args:
        per_pdf_results: [(pdf_path, slides_textify 결과 dict), ...]
            입력 순서가 강의 진행 순서.

    Returns:
        {
            "pages": [
                {
                    "index": int,             # 1..N_total (글로벌)
                    "title": str,
                    "anchors": list[str],
                    "brief": str,
                    "source_pdf": str,        # basename
                    "source_pdf_order": int,  # 0-based 입력 순서
                    "local_page": int,        # 해당 PDF 내 1-based
                },
                ...
            ],
            "sources": [
                {"order": 0, "pdf": "...", "page_count": N, "global_start": 1, "global_end": N},
                ...
            ],
        }
    """
    global_idx = 0
    pages_out: list[dict] = []
    sources: list[dict] = []

    for order, (pdf_path, data) in enumerate(per_pdf_results):
        raw_pages = list(data.get("pages", []))
        # slides_textify는 index 순이 보장돼 있지만 안전하게 정렬.
        raw_pages.sort(key=lambda p: p.get("index", 0) if isinstance(p, dict) else 0)

        g_start = global_idx + 1
        for raw in raw_pages:
            if not isinstance(raw, dict):
                continue
            global_idx += 1
            pages_out.append({
                "index": global_idx,
                "title": raw.get("title", ""),
                "anchors": list(raw.get("anchors", []) or []),
                "brief": raw.get("brief", ""),
                "source_pdf": pdf_path.name,
                "source_pdf_order": order,
                "local_page": raw.get("index", 0),
            })
        g_end = global_idx

        sources.append({
            "order": order,
            "pdf": pdf_path.name,
            "page_count": len(raw_pages),
            "global_start": g_start if raw_pages else 0,
            "global_end": g_end if raw_pages else 0,
        })

    return {"pages": pages_out, "sources": sources}
