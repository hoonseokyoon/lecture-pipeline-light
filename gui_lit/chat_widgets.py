"""Chat 페이지 전용 렌더링 헬퍼.

2_Chat.py 에서 import. 순수 렌더링·파싱 로직만 — side-effect (network, subprocess)
없음. 테스트 가능성 + 페이지 파일 간결성.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import streamlit as st


# ── 시간 포맷 ──


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def relative_time(ts: str, *, now: datetime | None = None) -> str:
    """'방금 전 / 3분 전 / 14:02' 스타일."""
    dt = _parse_iso(ts)
    if dt is None:
        return ts[:19] if ts else ""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return dt.astimezone().strftime("%H:%M:%S")
    if secs < 10:
        return "방금 전"
    if secs < 60:
        return f"{secs}초 전"
    if secs < 3600:
        return f"{secs // 60}분 전"
    if secs < 86400:
        return f"{secs // 3600}시간 전"
    return dt.astimezone().strftime("%m-%d %H:%M")


# ── Front matter 파서 ──


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def parse_front_matter(text: str) -> dict[str, str]:
    """경량 YAML-ish 파서 — `key: "value"` 한 줄씩. 복잡한 구조는 무시."""
    out: dict[str, str] = {}
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return out
    body = m.group(1)
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


# ── RFI 배너 ──


def render_rfi_banner(
    info: Any,  # project.ProjectInfo
    *,
    session_id: str | None,
    db_stats: dict[str, int] | None,
) -> None:
    """페이지 상단 고정 배너: RFI id/status/branch/session/counters."""
    rfi_text = ""
    if info.rfi_path.exists():
        try:
            rfi_text = info.rfi_path.read_text(encoding="utf-8")
        except OSError:
            pass
    fm = parse_front_matter(rfi_text)

    rfi_id = fm.get("id") or "-"
    status = fm.get("status") or "-"
    slug = fm.get("slug") or ""
    branch = fm.get("branch") or _current_branch(info.root) or "-"

    # 상태별 색상·이모지
    status_display = {
        "draft": "📝 draft",
        "researching": "🔬 researching",
        "done": "✅ done",
        "abandoned": "🚫 abandoned",
        "placeholder": "💤 placeholder",
    }.get(status, f"❓ {status}")

    # 진행 카운터 (DB)
    progress = ""
    if db_stats:
        total = db_stats.get("total", 0)
        done = db_stats.get("summarized", 0) + db_stats.get("done", 0)
        if total:
            progress = f"논문 {done}/{total}"

    with st.container(border=True):
        cols = st.columns([2, 2, 3, 2])
        with cols[0]:
            st.caption("RFI")
            st.markdown(f"**{rfi_id}** · {status_display}")
            if slug:
                st.caption(slug)
        with cols[1]:
            st.caption("Branch")
            st.code(branch, language="bash")
        with cols[2]:
            st.caption("Session")
            if session_id:
                st.code(session_id, language="bash")
            else:
                st.caption("_(미생성 — 첫 메시지 시 생성)_")
        with cols[3]:
            if progress:
                st.caption("Progress")
                st.markdown(f"**{progress}**")
            elif db_stats is not None:
                st.caption("DB")
                st.caption(f"{db_stats.get('total', 0)} 편")


def _current_branch(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root, capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


# ── 이벤트 타입별 메타데이터 (아이콘·라벨) ──


EVENT_META: dict[str, dict] = {
    "user_message":      {"icon": "👤", "role": "user",      "label": "사용자"},
    "agent_response":    {"icon": "🤖", "role": "assistant", "label": "Head"},
    "subagent_called":   {"icon": "🔧", "role": "middle",    "label": "Tool"},
    "subagent_returned": {"icon": "✅", "role": "middle",    "label": "Tool done"},
    "tool_use":          {"icon": "🔧", "role": "middle",    "label": "Tool"},
    "tool_result":       {"icon": "📤", "role": "middle",    "label": "Tool result"},
    "assistant_text":    {"icon": "💬", "role": "middle",    "label": "thinking"},
    "decision":          {"icon": "💭", "role": "middle",    "label": "Decision"},
    "commit":            {"icon": "📦", "role": "middle",    "label": "Commit"},
    "rfi_opened":        {"icon": "🎯", "role": "middle",    "label": "RFI opened"},
    "rfi_closed":        {"icon": "🏁", "role": "middle",    "label": "RFI closed"},
    "rfi_status_changed":{"icon": "🔄", "role": "middle",    "label": "RFI status"},
    "review_images_embedded": {"icon": "🖼", "role": "middle", "label": "Images"},
    "second_opinion_requested":{"icon": "🔍", "role": "middle", "label": "2nd opinion"},
    "second_opinion_received": {"icon": "📋", "role": "middle", "label": "2nd opinion"},
    "halt_requested":    {"icon": "⏸",  "role": "middle",    "label": "Halt"},
    "session_start":     {"icon": "🚀", "role": "middle",    "label": "Session start"},
    "session_end":       {"icon": "🛑", "role": "middle",    "label": "Session end"},
    "session_fresh_requested": {"icon": "🆕", "role": "middle", "label": "Fresh session"},
    "session_resumed":   {"icon": "🔁", "role": "middle",    "label": "Session resumed"},
    "project_initialized":{"icon": "🌱", "role": "middle",   "label": "Init"},
}


def event_meta(ev: dict) -> dict:
    kind = ev.get("kind", "unknown")
    return EVENT_META.get(
        kind, {"icon": "•", "role": "middle", "label": kind},
    )


def summarize_event(ev: dict) -> str:
    """중간 이벤트 한 줄 요약."""
    kind = ev.get("kind", "")
    content = ev.get("content", "")

    if kind in ("subagent_called", "tool_use"):
        tool = ev.get("tool") or ev.get("skill") or ev.get("name") or "?"
        args = ev.get("args") or ev.get("input") or ""
        args_short = (
            (json.dumps(args, ensure_ascii=False)[:80] + "…")
            if not isinstance(args, str)
            else args[:80]
        )
        return f"{tool} {args_short}"

    if kind == "subagent_returned" or kind == "tool_result":
        tool = ev.get("tool") or ev.get("skill") or ""
        dur = ev.get("duration_s")
        extras = []
        for k in ("count", "bytes", "failed", "succeeded", "hits"):
            if k in ev:
                extras.append(f"{k}={ev[k]}")
        suffix = " · ".join(extras)
        dur_s = f" ({dur:.1f}s)" if isinstance(dur, (int, float)) else ""
        return f"{tool}{dur_s} {suffix}".strip()

    if kind == "decision":
        text = ev.get("text") or content
        return text[:160] if text else ""

    if kind == "commit":
        msg = ev.get("message") or ev.get("summary") or content or ""
        sha = ev.get("sha", "")
        return f"{sha[:7]} {msg[:120]}".strip()

    if kind in ("rfi_opened", "rfi_closed"):
        rfi = ev.get("rfi", "")
        title = ev.get("title", "")
        return f"RFI-{rfi} {title}".strip()

    # fallback
    if content:
        return content[:160]
    parts = [f"{k}={v}" for k, v in ev.items() if k not in ("ts", "kind", "actor")]
    return " · ".join(parts[:3])[:160]


def render_chat_message(
    ev: dict, *, show_ts: bool = True,
) -> None:
    """user_message / agent_response 렌더."""
    meta = event_meta(ev)
    role = meta["role"] if meta["role"] in ("user", "assistant") else "assistant"
    with st.chat_message(role):
        header_bits = []
        if show_ts:
            header_bits.append(relative_time(ev.get("ts", "")))
        if meta["role"] == "assistant":
            model = ev.get("model")
            if model:
                header_bits.append(f"`{model}`")
            dur = ev.get("duration_s")
            if isinstance(dur, (int, float)):
                header_bits.append(f"{dur:.1f}s")
        if header_bits:
            st.caption(" · ".join(header_bits))
        content = ev.get("content", "")
        if content:
            st.markdown(content)
        else:
            st.caption("_(빈 응답)_")


def render_middle_events(events: list[dict]) -> None:
    """user_message 와 agent_response 사이의 이벤트들을 하나의 collapsed
    expander 로 묶어 보여줌. 이벤트가 많으면 이벤트 개수 · 종류 요약."""
    if not events:
        return
    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
    kind_summary = " · ".join(
        f"{event_meta({'kind':k})['icon']} {v}" for k, v in by_kind.items()
    )
    label = f"🔩 Tool activity ({len(events)} events) — {kind_summary}"
    with st.expander(label, expanded=False):
        for ev in events:
            meta = event_meta(ev)
            line = f"{meta['icon']} **{meta['label']}** · `{relative_time(ev.get('ts',''))}`"
            st.markdown(line)
            summary = summarize_event(ev)
            if summary:
                st.caption(summary)


def render_outcome_pill(outcome: dict | None) -> None:
    """마지막 agent_response 바로 아래 붙는 메타데이터 pill. container(border)
    로 시각 구분. outcome 은 session_state 에 저장된 직전 호출 결과 metadata."""
    if outcome is None:
        return
    with st.container(border=True):
        if outcome.get("kind") == "error":
            st.error(f"❌ 직전 호출 실패 · {relative_time(outcome.get('ts',''))}")
            st.code(outcome.get("message", ""), language="text")
        else:
            bits = [f"⏱ {outcome.get('duration_s', 0):.1f}s"]
            if outcome.get("session"):
                bits.append(f"sess `{outcome['session'][:10]}…`")
            it = outcome.get("input_tokens")
            ot = outcome.get("output_tokens")
            if it or ot:
                bits.append(f"{it or '-'}↑ / {ot or '-'}↓ tok")
            if outcome.get("cost_usd") is not None:
                bits.append(f"${outcome['cost_usd']:.4f}")
            if outcome.get("num_turns"):
                bits.append(f"{outcome['num_turns']} turns")
            st.caption(" · ".join(bits))
            if outcome.get("response_empty"):
                st.warning(
                    "⚠️ 응답 텍스트 비어있음. raw_json keys: "
                    f"`{outcome.get('raw_keys')}`"
                )
        if st.button("dismiss", key=f"dismiss-{outcome.get('ts','')}", type="tertiary"):
            st.session_state["last_agent_outcome"] = None
            st.rerun()


# ── Chat 전체 렌더 (interleaved) ──


def render_chat_timeline(
    events: list[dict],
    *,
    outcome: dict | None,
) -> None:
    """user_message + agent_response 를 chat_message 로,
    그 사이의 중간 이벤트는 하나의 expander 로 묶어 렌더.
    마지막 assistant 메시지 아래에 outcome pill 부착.

    Fresh/Resume 로 세션이 전환된 이후 기록만 본문에 보이고, 그 이전 기록은
    상단 collapsed expander 로 묶어 "이전 대화" 로 접근. journal 은 그대로
    보존되며 단지 *시각적* 으로 분리된다.
    """

    # 세션 경계 찾기 — 마지막 session_fresh_requested / session_resumed 이후만 본문
    split_idx = -1
    for i, ev in enumerate(events):
        if ev.get("kind") in ("session_fresh_requested", "session_resumed"):
            split_idx = i  # 가장 마지막 것을 선택

    prior_events = events[:split_idx] if split_idx >= 0 else []
    active_events = events[split_idx:] if split_idx >= 0 else events

    if prior_events:
        boundary_kind = events[split_idx].get("kind")
        label_verb = "Fresh" if boundary_kind == "session_fresh_requested" else "Resume"
        with st.expander(
            f"📚 이전 대화 ({len(prior_events)} 이벤트) — {label_verb} 이전 기록",
            expanded=False,
        ):
            _render_grouped_events(prior_events, outcome=None, show_outcome=False)
        st.caption(f"─── 🆕 {label_verb} 이후 대화 ───")

    _render_grouped_events(active_events, outcome=outcome, show_outcome=True)


def _render_grouped_events(
    events: list[dict],
    *,
    outcome: dict | None,
    show_outcome: bool,
) -> None:
    """내부 헬퍼 — 주어진 events 리스트를 main/middle 로 버킷팅 후 렌더."""
    buckets: list[dict] = []
    current_middle: list[dict] = []
    for ev in events:
        meta = event_meta(ev)
        if meta["role"] in ("user", "assistant"):
            if current_middle:
                buckets.append({"type": "middle", "events": current_middle})
                current_middle = []
            buckets.append({"type": "main", "event": ev})
        else:
            current_middle.append(ev)
    if current_middle:
        buckets.append({"type": "middle", "events": current_middle})

    last_asst_idx = -1
    for i, b in enumerate(buckets):
        if b["type"] == "main" and b["event"].get("kind") == "agent_response":
            last_asst_idx = i

    for i, b in enumerate(buckets):
        if b["type"] == "middle":
            render_middle_events(b["events"])
        else:
            render_chat_message(b["event"])
            if show_outcome and i == last_asst_idx and outcome is not None:
                render_outcome_pill(outcome)


# ── 작업 중 라이브 탭 ──


def render_session_manager(
    known_sessions: list[dict],
    current_sid: str | None,
    *,
    on_resume: "Callable[[str], None] | None" = None,  # noqa: F821
    on_fresh: "Callable[[], None] | None" = None,  # noqa: F821
    is_running: bool = False,
) -> None:
    """세션 선택·전환 UI. 접기식 expander."""
    count = len(known_sessions)
    label = f"🗂 세션 관리 ({count}개 알려진, 현재: "
    label += f"`{current_sid[:10]}…`" if current_sid else "_없음_"
    label += ")"

    with st.expander(label, expanded=False):
        if is_running:
            st.caption("_작업 중이라 세션 전환 비활성. 먼저 완료·취소._")

        # Fresh start
        col_fresh1, col_fresh2 = st.columns([1, 3])
        with col_fresh1:
            if st.button(
                "🆕 Fresh",
                disabled=is_running,
                help=(
                    "Claude CLI 새 세션 시작 + 채팅창에서 이전 대화를 "
                    "collapsed 로 접어둠 (journal 은 보존)."
                ),
            ):
                if on_fresh:
                    on_fresh()
        with col_fresh2:
            st.caption(
                "새로 시작하면 Claude CLI 가 새 세션 id 를 발급합니다. 기존 세션은 "
                "목록에 남아 언제든 resume 가능."
            )

        st.divider()

        if not known_sessions:
            st.caption("_아직 세션 기록이 없습니다. 첫 메시지 이후 나타남._")
            return

        st.caption("기록된 세션 (최근 사용 순)")

        for s in known_sessions:
            sid = s.get("session_id", "")
            is_current = sid == current_sid
            with st.container(border=True):
                cols = st.columns([3, 2, 1])
                with cols[0]:
                    title = s.get("title") or "_(제목 없음)_"
                    # 한 줄로 자르기
                    title_short = title.split("\n", 1)[0][:80]
                    prefix = "🟢 " if is_current else ""
                    st.markdown(f"{prefix}**{title_short}**")
                    st.code(sid, language="bash")
                with cols[1]:
                    rfi = s.get("rfi")
                    if rfi:
                        st.caption(f"RFI-{rfi}")
                    ts = s.get("last_used_ts") or s.get("created_ts", "")
                    st.caption(relative_time(ts))
                    st.caption(f"{s.get('events', 0)} 이벤트")
                with cols[2]:
                    if is_current:
                        st.caption("현재")
                    else:
                        if st.button(
                            "Resume",
                            key=f"resume-{sid}",
                            disabled=is_running,
                            use_container_width=True,
                        ):
                            if on_resume:
                                on_resume(sid)


def render_live_tail(
    new_events: Iterable[dict],
    *,
    max_rows: int = 6,
) -> None:
    """st.status 안에서 새로 도착한 이벤트를 간결하게 뿌림."""
    rows = list(new_events)[-max_rows:]
    if not rows:
        st.caption("_(아직 새 이벤트 없음. journal 을 폴링 중…)_")
        return
    for ev in rows:
        meta = event_meta(ev)
        summary = summarize_event(ev)
        st.markdown(
            f"{meta['icon']} **{meta['label']}** · `{relative_time(ev.get('ts',''))}`"
            f" — {summary}"
        )
