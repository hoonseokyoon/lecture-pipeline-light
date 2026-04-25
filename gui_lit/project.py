"""프로젝트 생성·열기·유효성 검증.

프로젝트 루트 디렉토리 구조 (init 시 생성):

    <project-root>/
    ├── AGENTS.md
    ├── PRIORITY_OF_INTELLIGENCE.md
    ├── REQUEST_FOR_INFORMATION.md
    ├── README.md
    ├── .gitignore, .gitattributes
    ├── .claude/
    │   ├── settings.json
    │   └── skills/{rfi-open, rfi-followup, rfi-close, lit-triage, compile-review}/SKILL.md
    ├── .litproj/
    │   ├── config.json
    │   ├── journal.jsonl    (0 bytes, 이후 append)
    │   ├── inbox/           (빈 디렉토리)
    │   ├── sessions/        (빈 디렉토리)
    │   └── followups/       (RFI addendum run metadata)
    ├── user-docs/
    ├── agent-docs/{reviews,summaries,study,rfi}/
    ├── originals/{papers,search}/
    ├── extracted/
    └── scripts/
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gui_lit import ipc

TEMPLATES = Path(__file__).parent / "templates"
TEMPLATE_RENAMES = {
    "gitignore": ".gitignore",
    "gitattributes": ".gitattributes",
}
MARKER_FILES = (
    "AGENTS.md",
    "PRIORITY_OF_INTELLIGENCE.md",
    ".litproj/config.json",
)


class ProjectError(Exception):
    """프로젝트 생성/검증 실패."""


@dataclass
class ProjectInfo:
    root: Path
    name: str
    pir_path: Path
    rfi_path: Path
    agents_path: Path
    config: dict


def is_project(root: Path) -> bool:
    """주어진 디렉토리가 litproj 프로젝트인지 marker 로 판정."""
    try:
        return all((root / m).is_file() for m in MARKER_FILES)
    except OSError:
        return False


def load_project(root: Path) -> ProjectInfo:
    root = Path(root).resolve()
    if not is_project(root):
        raise ProjectError(
            f"{root} 는 litproj 프로젝트가 아닙니다 "
            f"(AGENTS.md / PIR / .litproj/config.json 중 누락)"
        )
    cfg_path = root / ".litproj" / "config.json"
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectError(f"config.json 파싱 실패: {exc}") from exc
    return ProjectInfo(
        root=root,
        name=config.get("name", root.name),
        pir_path=root / "PRIORITY_OF_INTELLIGENCE.md",
        rfi_path=root / "REQUEST_FOR_INFORMATION.md",
        agents_path=root / "AGENTS.md",
        config=config,
    )


def _copy_template(src: Path, dst: Path) -> None:
    """템플릿 파일을 복사 (폴더면 전체)."""
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def _render_readme(template: str, project_name: str) -> str:
    return template.replace("{project_name}", project_name)


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def init_project(
    root: Path,
    *,
    name: str | None = None,
    harness_root: Path | None = None,
    git_user_name: str | None = None,
    git_user_email: str | None = None,
    overwrite: bool = False,
) -> ProjectInfo:
    """새 프로젝트 생성.

    Args:
        root: 프로젝트 루트 (없으면 생성). 이미 파일이 있고 overwrite=False 면 에러.
        name: 프로젝트 이름. None 이면 root.name.
        harness_root: lecture-pipeline-light 경로. config.json 에 기록.
        git_user_name / git_user_email: 프로젝트 로컬 git config.
        overwrite: True 면 기존 파일 덮어씀.
    """
    root = Path(root).resolve()
    name = name or root.name

    if root.exists():
        if any(root.iterdir()) and not overwrite:
            # AGENTS.md 가 이미 있으면 이미 init 된 프로젝트로 간주.
            if (root / "AGENTS.md").exists():
                raise ProjectError(
                    f"{root} 는 이미 초기화된 프로젝트로 보입니다. "
                    "overwrite=True 로 강제하거나 다른 디렉토리 선택."
                )
    else:
        root.mkdir(parents=True)

    # 템플릿 파일·디렉토리 복사
    for src in TEMPLATES.iterdir():
        name_on_disk = TEMPLATE_RENAMES.get(src.name, src.name)

        if src.name == "claude_settings.json":
            dst = root / ".claude" / "settings.json"
            _copy_template(src, dst)
        elif src.name == "claude_skills":
            dst = root / ".claude" / "skills"
            _copy_template(src, dst)
        elif src.name == "examples":
            # examples/ 는 user-docs/examples/ 로 — 사용자 참조용
            dst = root / "user-docs" / "examples"
            _copy_template(src, dst)
        elif src.name == "README.md":
            dst = root / "README.md"
            rendered = _render_readme(
                src.read_text(encoding="utf-8"), project_name=name
            )
            dst.write_text(rendered, encoding="utf-8")
        else:
            dst = root / name_on_disk
            _copy_template(src, dst)

    # 런타임 디렉토리 + placeholder
    for sub in (
        "user-docs",
        "agent-docs/reviews",
        "agent-docs/summaries",
        "agent-docs/study",
        "agent-docs/rfi",
        "originals/papers",
        "originals/search",
        "extracted",
        "scripts",
        ".litproj/inbox",
        ".litproj/inbox/processed",
        ".litproj/sessions",
        ".litproj/followups",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    # journal 파일 생성 (빈 파일)
    (root / ".litproj" / "journal.jsonl").touch()

    # .gitkeep 넣어서 빈 디렉토리 보존
    for empty_dir in (
        "user-docs",
        "agent-docs/reviews",
        "agent-docs/summaries",
        "agent-docs/study",
        "agent-docs/rfi",
        "originals/papers",
        "originals/search",
        "extracted",
        "scripts",
        ".litproj/sessions",
        ".litproj/followups",
    ):
        (root / empty_dir / ".gitkeep").touch()

    # config.json
    config = {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "harness_root": str(harness_root.resolve()) if harness_root else None,
        "schema_version": 1,
        "head_agent": {
            "runtime": "claude-code-cli",
            "model": "claude-opus-4-7",
        },
        "domain_profile": "mixed",
        "search_sources": ["semantic_scholar", "pubmed", "arxiv"],
        "review_depth": "section",
    }
    (root / ".litproj" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # git init + 초기 커밋
    try:
        _run_git(root, "init", "-b", "main")
        if git_user_name:
            _run_git(root, "config", "user.name", git_user_name)
        if git_user_email:
            _run_git(root, "config", "user.email", git_user_email)
        _run_git(root, "add", "-A")
        _run_git(
            root,
            "commit",
            "-m",
            "chore: initialize litproj project",
            "--allow-empty",
        )
    except subprocess.CalledProcessError as exc:
        tail = ((exc.stderr or "") + (exc.stdout or ""))[-400:]
        raise ProjectError(f"git init/commit 실패: {tail}") from exc
    except FileNotFoundError as exc:
        raise ProjectError(
            "git CLI 를 찾을 수 없습니다. 설치 후 PATH 에 등록하세요."
        ) from exc

    # journal 첫 이벤트
    _append_journal(
        root,
        {"kind": "project_initialized", "actor": "system", "name": name},
    )

    return load_project(root)


def _append_journal(root: Path, event: dict) -> None:
    ipc.append_journal(root, event)


# ── 최근 프로젝트 레지스트리 ──

_REGISTRY = Path.home() / ".lecture-pipeline" / "gui_lit_recent.json"


def load_recent(max_n: int = 10) -> list[dict]:
    if not _REGISTRY.exists():
        return []
    try:
        data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = data.get("recent", [])
    # 존재하지 않는 경로 제거
    items = [it for it in items if Path(it.get("root", "")).is_dir()]
    return items[:max_n]


def push_recent(root: Path, name: str, max_n: int = 10) -> None:
    _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    items = load_recent(max_n=100)
    root_str = str(Path(root).resolve())
    items = [it for it in items if it.get("root") != root_str]
    items.insert(
        0,
        {
            "root": root_str,
            "name": name,
            "last_opened": datetime.now(timezone.utc).isoformat(),
        },
    )
    items = items[:max_n]
    _REGISTRY.write_text(
        json.dumps({"recent": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    # 스모크 테스트: 임시 디렉토리에 프로젝트 생성.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "test-project"
        info = init_project(
            root,
            harness_root=Path(__file__).parent.parent,
        )
        print("initialized:", info.root)
        print("  name:", info.name)
        print("  files:", sorted(p.name for p in info.root.iterdir()))
        assert is_project(info.root)
        print("OK")
