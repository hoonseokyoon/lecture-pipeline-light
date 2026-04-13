from pathlib import Path

from codex_runner import CodexRunError

# slides.pdf 1개 필수 + _enriched/*.txt 0개 이상 허용.
def normalize(paths: list[Path]) -> dict[str, Path]:
    pdfs = [p for p in paths if p.suffix.lower() == ".pdf"]
    txts = [p for p in paths if p.suffix.lower() == ".txt"]

    if len(pdfs) != 1:
        raise CodexRunError(
            f"slides_textify: PDF 1개 필요 (받음: {len(pdfs)})"
        )
    inputs: dict[str, Path] = {"slides.pdf": pdfs[0]}

    # enriched 파일들은 _enriched/ 서브디렉토리로 전달
    for txt in txts:
        inputs[f"_enriched/{txt.name}"] = txt

    return inputs

expected_outputs = ["pages.json"]
