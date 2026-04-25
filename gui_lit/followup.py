"""닫힌 RFI 위에 조건부 추가조사(addendum)를 얹는 CLI.

RFI follow-up 은 새 RFI 번호를 만들지 않는다. 기존 RFI id 아래에
`fu-001` 같은 하위 조사 턴을 만들고, 검색 계획과 addendum 산출물을
분리해 추적한다.

사용:
    python -m gui_lit.followup open <project-root> --rfi 0001 --topic "..."
    python -m gui_lit.followup list <project-root> [--rfi 0001]
    python -m gui_lit.followup close <project-root> --rfi 0001 --followup fu-001
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gui_lit import ipc


class FollowupError(Exception):
    """RFI follow-up 생성/갱신 실패."""


@dataclass
class FollowupPaths:
    rfi_archive: Path
    followup_file: Path
    run_dir: Path
    query_plan: Path
    addendum: Path
    claim_matrix: Path


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str, maxlen: int = 42) -> str:
    s = re.sub(r"[^A-Za-z0-9\s_-]", "", text).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "followup")[:maxlen].rstrip("-") or "followup"


def _front_matter(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if sep:
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _replace_front_matter(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        raise FollowupError(f"front matter 없음: {path}")
    fm = _front_matter(path)
    fm.update(updates)
    lines = ["---"]
    for key, val in fm.items():
        lines.append(f'{key}: "{val}"')
    lines.append("---")
    new_text = "\n".join(lines) + "\n" + text[m.end():]
    path.write_text(new_text, encoding="utf-8")


def find_rfi_archive(project_root: Path, rfi_id: str) -> Path:
    rfi_id = str(rfi_id).zfill(4) if str(rfi_id).isdigit() else str(rfi_id)
    rfi_dir = Path(project_root) / "agent-docs" / "rfi"
    matches = sorted(p for p in rfi_dir.glob(f"{rfi_id}-*.md") if p.is_file())
    if not matches:
        raise FollowupError(f"RFI archive 없음: {rfi_id}")
    return matches[0]


def _followup_root(archive: Path) -> Path:
    return archive.with_suffix("") / "followups"


def _run_root(project_root: Path, rfi_id: str, followup_id: str, slug: str) -> Path:
    return Path(project_root) / ".litproj" / "followups" / rfi_id / f"{followup_id}-{slug}"


def _next_followup_id(followups_dir: Path) -> str:
    max_n = 0
    if followups_dir.is_dir():
        for path in followups_dir.glob("fu-*.md"):
            fm = _front_matter(path)
            raw = fm.get("followup_id") or path.stem.split("-", 2)[0]
            m = re.match(r"fu-(\d+)$", raw)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"fu-{max_n + 1:03d}"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def open_followup(
    project_root: Path,
    *,
    rfi_id: str,
    topic: str,
    question: str = "",
    slug: str | None = None,
    domain_profile: str = "mixed",
    priority: str = "normal",
    must_address: list[str] | None = None,
) -> FollowupPaths:
    root = Path(project_root).resolve()
    rfi_id = str(rfi_id).zfill(4) if str(rfi_id).isdigit() else str(rfi_id)
    archive = find_rfi_archive(root, rfi_id)
    rfi_fm = _front_matter(archive)
    rfi_slug = rfi_fm.get("slug") or archive.stem.split("-", 1)[-1]
    followups_dir = _followup_root(archive)
    followups_dir.mkdir(parents=True, exist_ok=True)

    followup_id = _next_followup_id(followups_dir)
    slug = _slugify(slug or topic)
    run_dir = _run_root(root, rfi_id, followup_id, slug)
    run_dir.mkdir(parents=True, exist_ok=True)

    followup_file = followups_dir / f"{followup_id}-{slug}.md"
    if followup_file.exists():
        raise FollowupError(f"follow-up 파일이 이미 존재함: {followup_file}")
    addendum = root / "agent-docs" / "reviews" / f"rfi-{rfi_id}-{followup_id}-{slug}-addendum.md"
    claim_matrix = root / "agent-docs" / "reviews" / f"rfi-{rfi_id}-{followup_id}-{slug}-claim-matrix.json"
    query_plan = run_dir / "query_plan.json"

    must_address = must_address or []
    now = _utc_iso()
    query_plan.write_text(
        json.dumps(
            {
                "rfi": rfi_id,
                "rfi_slug": rfi_slug,
                "followup_id": followup_id,
                "topic": topic,
                "query": question or topic,
                "domain_profile": domain_profile,
                "priority": priority,
                "must_address": must_address,
                "outputs": {
                    "addendum": _relative(addendum, root),
                    "claim_matrix": _relative(claim_matrix, root),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    must_lines = "\n".join(f"- {x}" for x in must_address) or "- (조사 중 구체화)"
    body = f"""---
rfi: "{rfi_id}"
rfi_slug: "{rfi_slug}"
followup_id: "{followup_id}"
slug: "{slug}"
status: "researching"
created: "{now}"
updated: "{now}"
priority: "{priority}"
domain_profile: "{domain_profile}"
query_plan: "{_relative(query_plan, root)}"
addendum: "{_relative(addendum, root)}"
claim_matrix: "{_relative(claim_matrix, root)}"
---

# RFI-{rfi_id} Follow-up {followup_id} — {topic}

## Trigger

{question or topic}

## Conditional Focus

이 follow-up 은 RFI-{rfi_id} 본문을 재작성하기 전에 기존 결론의 약한
부분만 추가 조사한다. 새 RFI 번호를 만들지 않고 addendum 으로 근거를
축적한 뒤, 충분할 때 원 리뷰에 병합한다.

## Must Address

{must_lines}

## Expected Deliverables

- [ ] `{_relative(query_plan, root)}`
- [ ] `{_relative(addendum, root)}`
- [ ] `{_relative(claim_matrix, root)}`
- [ ] 필요 시 follow-up candidates/triage/summaries
- [ ] 원 리뷰 병합 여부 결정

## Acceptance Gate

- 새 후보는 기존 RFI claim 과의 관계가 명확해야 한다.
- abstract-only 근거만으로 high-confidence claim 을 추가하지 않는다.
- addendum 작성 후 `python -m gui_lit.doctor .` 를 통과한다.

## Progress

- {now}: follow-up opened.
"""
    followup_file.write_text(body, encoding="utf-8")
    ipc.append_journal(
        root,
        {
            "actor": "head",
            "kind": "rfi_followup_opened",
            "rfi": rfi_id,
            "followup": followup_id,
            "topic": topic,
            "query_plan": _relative(query_plan, root),
        },
    )
    return FollowupPaths(archive, followup_file, run_dir, query_plan, addendum, claim_matrix)


def list_followups(project_root: Path, *, rfi_id: str | None = None) -> list[dict]:
    root = Path(project_root).resolve()
    archives: list[Path]
    if rfi_id:
        archives = [find_rfi_archive(root, rfi_id)]
    else:
        archives = sorted((root / "agent-docs" / "rfi").glob("????-*.md"))
    out: list[dict] = []
    for archive in archives:
        for path in sorted(_followup_root(archive).glob("fu-*.md")):
            fm = _front_matter(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            title = path.stem
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line.lstrip("# ").strip()
                    break
            out.append(
                {
                    "rfi": fm.get("rfi"),
                    "followup_id": fm.get("followup_id"),
                    "slug": fm.get("slug"),
                    "status": fm.get("status"),
                    "priority": fm.get("priority"),
                    "topic": title,
                    "path": _relative(path, root),
                    "query_plan": fm.get("query_plan"),
                    "addendum": fm.get("addendum"),
                }
            )
    return out


def close_followup(
    project_root: Path,
    *,
    rfi_id: str,
    followup_id: str,
    status: str = "done",
    outcome: str = "",
) -> Path:
    root = Path(project_root).resolve()
    archive = find_rfi_archive(root, rfi_id)
    matches = sorted(_followup_root(archive).glob(f"{followup_id}-*.md"))
    if not matches:
        raise FollowupError(f"follow-up 없음: {rfi_id}/{followup_id}")
    path = matches[0]
    fm = _front_matter(path)
    now = _utc_iso()
    _replace_front_matter(path, {"status": status, "updated": now})
    if outcome:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Outcome\n\n{outcome}\n")
    ipc.append_journal(
        root,
        {
            "actor": "head",
            "kind": "rfi_followup_closed",
            "rfi": fm.get("rfi") or str(rfi_id).zfill(4),
            "followup": followup_id,
            "status": status,
            "addendum": fm.get("addendum"),
        },
    )
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="gui_lit RFI follow-up utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open", help="닫힌 RFI 아래 follow-up/addendum 턴 생성")
    op.add_argument("project_root", type=Path)
    op.add_argument("--rfi", required=True)
    op.add_argument("--topic", required=True)
    op.add_argument("--question", default="")
    op.add_argument("--slug", default=None)
    op.add_argument("--domain-profile", default="mixed")
    op.add_argument("--priority", default="normal")
    op.add_argument("--must-address", action="append", default=[])

    ls = sub.add_parser("list", help="follow-up 목록 출력")
    ls.add_argument("project_root", type=Path)
    ls.add_argument("--rfi", default=None)
    ls.add_argument("--json", action="store_true")

    cl = sub.add_parser("close", help="follow-up 상태 종료")
    cl.add_argument("project_root", type=Path)
    cl.add_argument("--rfi", required=True)
    cl.add_argument("--followup", required=True)
    cl.add_argument("--status", default="done")
    cl.add_argument("--outcome", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "open":
            paths = open_followup(
                args.project_root,
                rfi_id=args.rfi,
                topic=args.topic,
                question=args.question,
                slug=args.slug,
                domain_profile=args.domain_profile,
                priority=args.priority,
                must_address=args.must_address,
            )
            print(json.dumps({k: str(v) for k, v in paths.__dict__.items()}, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "list":
            items = list_followups(args.project_root, rfi_id=args.rfi)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            else:
                for item in items:
                    print(f"{item.get('rfi')}/{item.get('followup_id')} {item.get('status')}: {item.get('path')}")
            return 0
        if args.cmd == "close":
            path = close_followup(
                args.project_root,
                rfi_id=args.rfi,
                followup_id=args.followup,
                status=args.status,
                outcome=args.outcome,
            )
            print(f"closed: {path}")
            return 0
    except FollowupError as exc:
        print(f"followup error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
