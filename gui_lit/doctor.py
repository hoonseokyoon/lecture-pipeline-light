"""gui_lit 프로젝트 무결성 검사 및 journal 복구 CLI.

사용:
    python -m gui_lit.doctor <project-root>
    python -m gui_lit.doctor repair-journal <project-root> --backup
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass
class Finding:
    level: str  # error | warning
    code: str
    path: str
    message: str


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _parse_json_objects(line: str) -> tuple[list[dict], str | None]:
    """한 줄에서 연속 JSON object 들을 읽는다.

    Returns:
        (objects, error). error 가 None 이 아니면 복구 불가능한 파싱 실패.
    """
    decoder = json.JSONDecoder()
    pos = 0
    out: list[dict] = []
    n = len(line)
    while pos < n:
        while pos < n and line[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, end = decoder.raw_decode(line, pos)
        except json.JSONDecodeError as exc:
            return out, str(exc)
        if not isinstance(obj, dict):
            return out, "journal row is not a JSON object"
        out.append(obj)
        pos = end
    return out, None


def _iso_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_journal(root: Path) -> list[Finding]:
    path = root / ".litproj" / "journal.jsonl"
    findings: list[Finding] = []
    if not path.exists():
        findings.append(Finding("error", "journal_missing", _rel(path, root), "journal.jsonl 없음"))
        return findings

    prev_dt: datetime | None = None
    prev_line: int | None = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        objs, err = _parse_json_objects(line)
        if err:
            findings.append(
                Finding(
                    "error",
                    "journal_json",
                    f"{_rel(path, root)}:{lineno}",
                    f"JSON 파싱 실패: {err}",
                )
            )
            continue
        if len(objs) > 1:
            findings.append(
                Finding(
                    "error",
                    "journal_concatenated",
                    f"{_rel(path, root)}:{lineno}",
                    f"한 줄에 JSON object {len(objs)}개가 붙어 있음",
                )
            )
        for obj in objs:
            dt = _iso_dt(str(obj.get("ts", "")))
            if dt is None:
                findings.append(
                    Finding(
                        "error",
                        "journal_ts",
                        f"{_rel(path, root)}:{lineno}",
                        "ts 가 없거나 ISO8601 이 아님",
                    )
                )
                continue
            if prev_dt is not None and dt < prev_dt:
                findings.append(
                    Finding(
                        "warning",
                        "journal_nonmonotonic_ts",
                        f"{_rel(path, root)}:{lineno}",
                        f"timestamp 가 이전 라인보다 과거임 (prev line {prev_line})",
                    )
                )
            prev_dt = dt
            prev_line = lineno
    return findings


def _front_matter(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def check_rfi_pointer(root: Path) -> list[Finding]:
    pointer = root / "REQUEST_FOR_INFORMATION.md"
    findings: list[Finding] = []
    fm = _front_matter(pointer)
    rid = fm.get("id")
    slug = fm.get("slug")
    pointer_status = fm.get("status")
    if not rid or not slug:
        return findings
    archive = root / "agent-docs" / "rfi" / f"{rid}-{slug}.md"
    afm = _front_matter(archive)
    if not afm:
        findings.append(
            Finding(
                "warning",
                "rfi_archive_missing",
                _rel(pointer, root),
                f"active pointer 가 가리키는 archive RFI 없음: {archive.name}",
            )
        )
        return findings
    archive_status = afm.get("status")
    if archive_status and pointer_status and archive_status != pointer_status:
        findings.append(
            Finding(
                "error",
                "rfi_status_mismatch",
                _rel(pointer, root),
                f"REQUEST status={pointer_status!r}, archive status={archive_status!r}",
            )
        )
    return findings


def check_candidates(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in root.rglob("candidates.json"):
        if ".git" in path.parts:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                Finding("error", "candidates_json", _rel(path, root), f"파싱 실패: {exc}")
            )
            continue
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = data.get("entries") or data.get("items") or data.get("candidates") or []
        else:
            candidates = []
        if not isinstance(candidates, list):
            findings.append(
                Finding("error", "candidates_shape", _rel(path, root), "candidate list 없음")
            )
            continue
        expected = data.get("total_after_dedup") if isinstance(data, dict) else None
        if isinstance(expected, int) and expected != len(candidates):
            findings.append(
                Finding(
                    "error",
                    "candidates_count",
                    _rel(path, root),
                    f"total_after_dedup={expected}, 실제 candidates={len(candidates)}",
                )
            )
    return findings


_REF_RE = re.compile(r"(?<!\!)\[(\d+)\]")
_REF_LINE_RE = re.compile(r"^\s*(\d+)\.\s+")
_PLACEHOLDER_RE = re.compile(
    r"placeholder|TODO|FIXME|대표 논문 번호 분산|본 클러스터 findings 합성|세부 인용 없음",
    re.I,
)


def _review_files(root: Path) -> Iterable[Path]:
    reviews = root / "agent-docs" / "reviews"
    if not reviews.is_dir():
        return []
    return reviews.glob("*.md")


def check_review_lint(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _review_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _PLACEHOLDER_RE.search(text):
            findings.append(
                Finding(
                    "error",
                    "review_placeholder_ref",
                    _rel(path, root),
                    "placeholder/TODO/세부 인용 없음 문구가 남아 있음",
                )
            )
        head, marker, refs = text.partition("## References")
        body_refs = {int(x) for x in _REF_RE.findall(head)}
        ref_nums = {int(m.group(1)) for m in map(_REF_LINE_RE.match, refs.splitlines()) if m}
        for n in sorted(body_refs - ref_nums):
            findings.append(
                Finding(
                    "error",
                    "review_dangling_citation",
                    _rel(path, root),
                    f"본문 인용 [{n}] 에 대응하는 References 항목 없음",
                )
            )
        for n in sorted(ref_nums - body_refs):
            findings.append(
                Finding(
                    "warning",
                    "review_unused_reference",
                    _rel(path, root),
                    f"References {n}. 항목이 본문에서 인용되지 않음",
                )
            )
    for path in (root / "agent-docs" / "reviews").glob("*claim-matrix.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                Finding("error", "claim_matrix_json", _rel(path, root), f"파싱 실패: {exc}")
            )
            continue
        claims = data.get("claims") if isinstance(data, dict) else data
        if not isinstance(claims, list):
            findings.append(
                Finding("error", "claim_matrix_shape", _rel(path, root), "claims list 없음")
            )
            continue
        for idx, claim in enumerate(claims, 1):
            if not isinstance(claim, dict):
                continue
            confidence = str(claim.get("confidence", "")).lower()
            high = confidence in {"high", "높음"} or confidence.startswith("0.8") or confidence.startswith("0.9") or confidence == "1"
            full = claim.get("fulltext_refs") or []
            abstract = claim.get("abstract_only_refs") or []
            if high and abstract and not full:
                findings.append(
                    Finding(
                        "error",
                        "claim_abstract_only_high_confidence",
                        f"{_rel(path, root)}:{idx}",
                        "abstract-only 근거만 있는 high-confidence claim",
                    )
                )
    return findings


def run_checks(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    findings.extend(check_journal(root))
    findings.extend(check_rfi_pointer(root))
    findings.extend(check_candidates(root))
    findings.extend(check_review_lint(root))
    return findings


def repair_journal(root: Path, *, backup: bool) -> Path:
    root = root.resolve()
    path = root / ".litproj" / "journal.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    original = path.read_text(encoding="utf-8")
    lines: list[str] = []
    for lineno, line in enumerate(original.splitlines(), 1):
        if not line.strip():
            continue
        objs, err = _parse_json_objects(line)
        if err:
            raise ValueError(f"{path}:{lineno}: 복구 불가 JSON: {err}")
        for obj in objs:
            lines.append(json.dumps(obj, ensure_ascii=False))
    if backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, path.with_name(f"{path.name}.{stamp}.bak"))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _print_findings(findings: list[Finding]) -> None:
    if not findings:
        print("doctor: OK")
        return
    for f in findings:
        print(f"{f.level.upper()} {f.code} {f.path}: {f.message}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "repair-journal":
        ap = argparse.ArgumentParser(description="concatenated journal JSONL 복구")
        ap.add_argument("project_root", type=Path)
        ap.add_argument("--backup", action="store_true")
        args = ap.parse_args(argv[1:])
        repaired = repair_journal(args.project_root, backup=args.backup)
        print(f"repaired: {repaired}")
        return 0

    ap = argparse.ArgumentParser(description="gui_lit 프로젝트 doctor")
    ap.add_argument("project_root", type=Path)
    args = ap.parse_args(argv)
    if args.project_root is None:
        ap.error("project_root 필요")
    findings = run_checks(args.project_root)
    _print_findings(findings)
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
