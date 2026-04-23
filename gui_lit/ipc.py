"""파일 기반 IPC — Streamlit UI ↔ Head agent 비동기 통신.

설계 원칙:
- **문자열/JSONL 만 사용**. 바이너리 동시성 이슈 없음.
- 모든 쓰기는 **append-only** 또는 **파일 단위 atomic rename**.
- UI 는 tail 로 관찰, agent 는 주기적으로 폴링. 소켓 불필요.

경로:
- `.litproj/journal.jsonl`        append-only 이벤트 로그
- `.litproj/inbox/<ts>.msg`       사용자 → agent 메시지 drop
- `.litproj/inbox/processed/`     agent 가 읽고 이동
- `.litproj/current_session.txt`  RFI 당 1개 claude session id
- `.litproj/halt`                 존재하면 agent 는 매 턴 시작 시 중단
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("lecture_pipeline.gui_lit.ipc")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Journal (append-only JSONL) ──


def journal_path(project_root: Path) -> Path:
    return project_root / ".litproj" / "journal.jsonl"


def append_journal(project_root: Path, event: dict) -> dict:
    """event 에 `ts` 자동 주입. 반환값은 실제 기록된 dict."""
    path = journal_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _utc_iso(), **event}
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return record


def read_journal(
    project_root: Path,
    *,
    limit: int | None = None,
    since_ts: str | None = None,
) -> list[dict]:
    """journal 을 리스트로 읽음. limit 은 최근 N 개만, since_ts 는 ISO 문자열
    초과분만."""
    path = journal_path(project_root)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts and rec.get("ts", "") <= since_ts:
                continue
            out.append(rec)
    if limit is not None:
        out = out[-limit:]
    return out


def tail_journal(
    project_root: Path,
    *,
    poll_interval: float = 1.0,
    stop_flag: "threading.Event | None" = None,  # noqa: F821
) -> Iterator[dict]:
    """파일이 자라는 대로 이벤트를 yield. 무한 반복. stop_flag 로 종료."""
    path = journal_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with path.open("r", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        while True:
            if stop_flag is not None and stop_flag.is_set():
                return
            line = f.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("journal 라인 파싱 실패: %r", line[:120])


# ── Inbox (사용자 → agent) ──


def inbox_dir(project_root: Path) -> Path:
    return project_root / ".litproj" / "inbox"


def inbox_processed_dir(project_root: Path) -> Path:
    return project_root / ".litproj" / "inbox" / "processed"


def drop_inbox_message(
    project_root: Path,
    content: str,
    *,
    author: str = "user",
) -> Path:
    """사용자 메시지를 inbox 에 drop. 파일명은 `YYYYMMDDTHHMMSSfff.msg` 로
    정렬 가능. 반환: 생성된 파일 경로.

    atomic: tempfile 로 쓰고 rename.
    """
    dir_ = inbox_dir(project_root)
    dir_.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    final = dir_ / f"{ts}.msg"
    body = {
        "ts": _utc_iso(),
        "author": author,
        "content": content,
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=dir_,
        delete=False,
        suffix=".tmp",
    ) as tf:
        json.dump(body, tf, ensure_ascii=False)
        tmp_path = Path(tf.name)
    tmp_path.replace(final)
    # Journal 에도 기록해서 observer 에서 바로 보임
    append_journal(
        project_root,
        {"actor": author, "kind": "user_message", "content": content},
    )
    return final


def list_pending_inbox(project_root: Path) -> list[Path]:
    dir_ = inbox_dir(project_root)
    if not dir_.is_dir():
        return []
    out = []
    for p in sorted(dir_.iterdir()):
        if p.is_file() and p.suffix == ".msg":
            out.append(p)
    return out


def mark_inbox_processed(project_root: Path, message_path: Path) -> Path:
    """agent 가 메시지를 읽은 뒤 processed/ 로 이동."""
    processed = inbox_processed_dir(project_root)
    processed.mkdir(parents=True, exist_ok=True)
    dst = processed / message_path.name
    message_path.replace(dst)
    return dst


# ── 현재 세션 (RFI 당 1개) ──


def session_file(project_root: Path) -> Path:
    return project_root / ".litproj" / "current_session.txt"


def read_current_session(project_root: Path) -> str | None:
    p = session_file(project_root)
    if not p.exists():
        return None
    sid = p.read_text(encoding="utf-8").strip()
    return sid or None


def write_current_session(project_root: Path, session_id: str) -> None:
    p = session_file(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(session_id.strip(), encoding="utf-8")


def clear_current_session(project_root: Path) -> None:
    p = session_file(project_root)
    if p.exists():
        p.unlink()


# ── 세션 히스토리 index ──

def session_index_path(project_root: Path) -> Path:
    return project_root / ".litproj" / "sessions" / "index.jsonl"


def record_session_event(
    project_root: Path,
    session_id: str,
    event: str,
    *,
    rfi_id: str | None = None,
    title: str | None = None,
) -> None:
    """세션 생성·사용·교체 이벤트 기록.

    event: created | used | rotated | abandoned
    title: 사용자 첫 메시지 미리보기 (UI 에서 세션 식별 힌트)
    """
    path = session_index_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _utc_iso(),
        "session_id": session_id,
        "event": event,
    }
    if rfi_id:
        record["rfi"] = rfi_id
    if title:
        record["title"] = title[:200]
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_known_sessions(project_root: Path) -> list[dict]:
    """dedup by session_id. 각 세션에 대해 첫 created ts, 마지막 used ts,
    생성 당시 title 힌트, 연결된 RFI 를 모은다."""
    path = session_index_path(project_root)
    if not path.exists():
        return []
    by_id: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = rec.get("session_id")
            if not sid:
                continue
            bucket = by_id.setdefault(
                sid,
                {
                    "session_id": sid,
                    "created_ts": None,
                    "last_used_ts": None,
                    "title": None,
                    "rfi": None,
                    "events": 0,
                },
            )
            bucket["events"] += 1
            if rec.get("event") == "created":
                bucket["created_ts"] = rec["ts"]
                if rec.get("title") and not bucket["title"]:
                    bucket["title"] = rec["title"]
                if rec.get("rfi") and not bucket["rfi"]:
                    bucket["rfi"] = rec["rfi"]
            if rec.get("event") == "used":
                bucket["last_used_ts"] = rec["ts"]
                if rec.get("rfi") and not bucket["rfi"]:
                    bucket["rfi"] = rec["rfi"]
    # 최근 사용 기준 정렬
    items = list(by_id.values())
    items.sort(
        key=lambda b: b.get("last_used_ts") or b.get("created_ts") or "",
        reverse=True,
    )
    return items


# ── Halt flag ──


def halt_flag(project_root: Path) -> Path:
    return project_root / ".litproj" / "halt"


def set_halt(project_root: Path, reason: str = "") -> None:
    halt_flag(project_root).write_text(reason or "halt", encoding="utf-8")
    append_journal(
        project_root,
        {"actor": "user", "kind": "halt_requested", "reason": reason},
    )


def clear_halt(project_root: Path) -> None:
    p = halt_flag(project_root)
    if p.exists():
        p.unlink()


def is_halted(project_root: Path) -> bool:
    return halt_flag(project_root).exists()


@dataclass
class IpcPaths:
    journal: Path
    inbox: Path
    inbox_processed: Path
    session: Path
    halt: Path

    @classmethod
    def for_project(cls, project_root: Path) -> "IpcPaths":
        return cls(
            journal=journal_path(project_root),
            inbox=inbox_dir(project_root),
            inbox_processed=inbox_processed_dir(project_root),
            session=session_file(project_root),
            halt=halt_flag(project_root),
        )


if __name__ == "__main__":
    # 스모크 테스트
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".litproj").mkdir()
        (root / ".litproj" / "inbox").mkdir()

        append_journal(root, {"actor": "test", "kind": "hello"})
        drop_inbox_message(root, "첫 메시지")
        drop_inbox_message(root, "둘째 메시지")
        pending = list_pending_inbox(root)
        assert len(pending) == 2, pending
        mark_inbox_processed(root, pending[0])
        assert len(list_pending_inbox(root)) == 1

        j = read_journal(root)
        print("journal rows:", len(j))
        for ev in j:
            print(" ", ev.get("kind"), "-", ev.get("content", ""))

        write_current_session(root, "abc-123")
        assert read_current_session(root) == "abc-123"

        set_halt(root, "test")
        assert is_halted(root)
        clear_halt(root)
        assert not is_halted(root)

        print("OK")
