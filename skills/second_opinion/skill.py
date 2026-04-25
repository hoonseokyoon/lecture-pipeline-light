"""second_opinion — Codex 로 Claude 산출물을 재검토.

일반 스킬 (composite 아님): harness 가 prompt + inputs 로 Codex 를 자동 실행.
review + RFI + 후보/triage/claim matrix bundle 을 함께 넘겨 근거 대조 평가.
"""

from pathlib import Path

from codex_runner import CodexRunError


def _safe_name(name: str, used: set[str]) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = name
    i = 2
    while candidate in used:
        candidate = f"{stem}-{i}{suffix}"
        i += 1
    used.add(candidate)
    return candidate


def normalize(paths: list[Path]) -> dict[str, Path]:
    if not paths:
        raise CodexRunError("second_opinion: 입력 파일 필요")

    review: Path | None = None
    out: dict[str, Path] = {}
    used: set[str] = set()

    def add(name: str, path: Path) -> None:
        out[_safe_name(name, used)] = path

    for p in paths:
        lower = p.name.lower()
        path_text = p.as_posix().lower()
        if p.suffix.lower() == ".md" and "review" in lower:
            review = p
        elif lower.endswith("claim-matrix.json"):
            add("claim-matrix.json", p)
        elif lower == "candidates.json":
            add("candidates.json", p)
        elif lower == "triaged.json":
            add("triaged.json", p)
        elif lower == "request_for_information.md" or "rfi-" in lower or "/rfi/" in path_text:
            add("rfi.md", p)
        elif lower == "priority_of_intelligence.md" or "pir" in lower:
            add("pir.md", p)
        elif lower in {"summary_bundle.json", "summary-bundle.json"}:
            add("summary-bundle.json", p)
        elif p.suffix.lower() == ".md" and review is None:
            review = p
        elif p.suffix.lower() == ".json" and "summary" in lower:
            add(f"summaries/{p.name}", p)
        else:
            add(p.name, p)

    if review is None:
        raise CodexRunError("second_opinion: review markdown 입력이 필요합니다")
    add("review.md", review)
    return out


expected_outputs = ["critique.md"]
