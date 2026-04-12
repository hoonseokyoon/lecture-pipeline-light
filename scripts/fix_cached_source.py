#!/usr/bin/env python
"""lecture_note 캐시의 source 필드 복구 스크립트.

align/review/reconcile LLM 이 `source` 필드에 `prompt.txt` 같은 잘못된 값을
hallucinate 한 오래된 캐시를 수동으로 고치기 위한 유틸리티. `numbered_meta.json`
에서 실제 녹취록 파일명 목록을 읽고, 유효하지 않은 `source` 값을 auto-remap.

단일 녹취록 → 그 하나로 덮어씀.
다중 녹취록 → 경고만 출력하고 건드리지 않음 (수동 검수 필요).

사용:
    python scripts/fix_cached_source.py <cache_dir>

    예:
    python scripts/fix_cached_source.py "C:/Users/poinc/.cache/lecture-pipeline/composite/lecture_note/e3004e3bdc36d44b"

복구 대상 파일:
    - step2_alignments.json
    - step2b_reviewed.json
    - step3_mapping.json (mapping + unassigned)

복구 후: step3_mapping.json 이후(step5_pages/, step5b_polished/, step6/7/8)를
삭제해야 downstream 이 재계산됨. 이 스크립트는 자동 삭제하지 않음 — 안전을 위해
사용자에게 안내 메시지만 출력.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_allowed_sources(cache_dir: Path) -> list[str]:
    meta_path = cache_dir / "numbered_meta.json"
    if not meta_path.exists():
        raise SystemExit(f"numbered_meta.json 없음: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return list(meta.keys())


def _fix_ranges_in_list(
    ranges: list,
    allowed: set[str],
    fixed_one: str | None,
    stats: dict,
) -> None:
    for r in ranges:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if src in allowed:
            stats["kept"] += 1
            continue
        if fixed_one is not None:
            r["source"] = fixed_one
            stats["remapped"] += 1
            stats["remapped_from"].setdefault(src, 0)
            stats["remapped_from"][src] += 1
        else:
            stats["invalid_multi"] += 1
            stats["invalid_values"].add(src)


def fix_alignments_file(
    path: Path, allowed: set[str], fixed_one: str | None,
) -> dict:
    """step2_alignments.json / step2b_reviewed.json 복구.

    포맷: `{"1": [{source, start_line, end_line}, ...], "2": [...]}`
    (pipeline 이 JSON 저장 시 int key → str 변환해 놓은 구조)
    """
    stats = {
        "kept": 0,
        "remapped": 0,
        "invalid_multi": 0,
        "remapped_from": {},
        "invalid_values": set(),
    }
    if not path.exists():
        return stats
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print(f"  {path.name}: 최상위가 dict 아님, 건너뜀")
        return stats
    for page_key, ranges in data.items():
        if isinstance(ranges, list):
            _fix_ranges_in_list(ranges, allowed, fixed_one, stats)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stats


def fix_mapping_file(
    path: Path, allowed: set[str], fixed_one: str | None,
) -> dict:
    """step3_mapping.json 복구.

    포맷: `{"mapping": {"1": [...], ...}, "unassigned": [{source, ...}, ...]}`
    """
    stats = {
        "kept": 0,
        "remapped": 0,
        "invalid_multi": 0,
        "remapped_from": {},
        "invalid_values": set(),
    }
    if not path.exists():
        return stats
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return stats
    mapping = data.get("mapping", {})
    if isinstance(mapping, dict):
        for page_key, ranges in mapping.items():
            if isinstance(ranges, list):
                _fix_ranges_in_list(ranges, allowed, fixed_one, stats)
    unassigned = data.get("unassigned", [])
    if isinstance(unassigned, list):
        _fix_ranges_in_list(unassigned, allowed, fixed_one, stats)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stats


def _report(file_name: str, stats: dict) -> None:
    print(f"  {file_name}:")
    print(f"    유효: {stats['kept']}")
    print(f"    remap: {stats['remapped']}")
    if stats["remapped_from"]:
        for bad, n in stats["remapped_from"].items():
            print(f"      {bad!r} × {n}")
    if stats["invalid_multi"]:
        print(f"    ⚠️  다중 녹취록 미해결: {stats['invalid_multi']}")
        for v in sorted(stats["invalid_values"]):
            print(f"      {v!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache_dir", help="lecture_note 캐시 디렉토리 경로")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="파일 수정 없이 검사만",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_dir():
        print(f"디렉토리 없음: {cache_dir}", file=sys.stderr)
        return 1

    try:
        allowed_list = _load_allowed_sources(cache_dir)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"캐시 디렉토리: {cache_dir}")
    print(f"허용되는 source ({len(allowed_list)}):")
    for n in allowed_list:
        print(f"  - {n!r}")
    print()

    if len(allowed_list) == 1:
        fixed_one = allowed_list[0]
        print(f"단일 녹취록 모드 → auto-remap target: {fixed_one!r}")
    else:
        fixed_one = None
        print("다중 녹취록 모드 → invalid source 는 remap 불가 (경고만 출력)")
    print()

    if args.dry_run:
        print("(dry-run: 파일 수정 안 함)")
        print()

    targets = [
        ("step2_alignments.json", fix_alignments_file),
        ("step2b_reviewed.json", fix_alignments_file),
        ("step3_mapping.json", fix_mapping_file),
    ]

    total_remapped = 0
    total_invalid = 0
    for name, fixer in targets:
        path = cache_dir / name
        if not path.exists():
            print(f"  {name}: 없음, skip")
            continue
        if args.dry_run:
            # dry-run 용 복제본 검사
            backup = json.loads(path.read_text(encoding="utf-8"))
            # 임시 파일로 검사만
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(backup, tmp, ensure_ascii=False)
                tmp_path = Path(tmp.name)
            stats = fixer(tmp_path, set(allowed_list), fixed_one)
            tmp_path.unlink()
        else:
            stats = fixer(path, set(allowed_list), fixed_one)
        _report(name, stats)
        total_remapped += stats["remapped"]
        total_invalid += stats["invalid_multi"]

    print()
    print(f"총 remap: {total_remapped}")
    print(f"총 해결 불가: {total_invalid}")

    if total_remapped > 0 and not args.dry_run:
        print()
        print("⚠️  다음 단계: downstream 캐시 무효화")
        print("  다음 파일/디렉토리를 수동 삭제 후 재실행:")
        print(f"    - {cache_dir / 'step5_pages'}")
        print(f"    - {cache_dir / 'step5b_polished'}")
        print(f"    - {cache_dir / 'step6_note.md'}")
        print(f"    - {cache_dir / 'step7_note.json'}")
        print(f"    - {cache_dir / 'step8_note.html'}")
        print()
        print("또는 전체 재실행이 더 안전하면 캐시 버전 파일 삭제:")
        print(f"    - {cache_dir / '_cache_version.txt'}")
        print("  → 다음 실행 시 pipeline 이 v3/v4 전환으로 인식해 자동 정리.")

    return 0 if total_invalid == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
