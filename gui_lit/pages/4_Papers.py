"""Papers — DB 에 적재된 논문 리스트 브라우저."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import render_sidebar, require_project
from gui_lit import db


st.set_page_config(page_title="Papers", page_icon="📄", layout="wide")
render_sidebar()

info = require_project()

st.title("📄 Papers")
st.caption(str(info.root))

try:
    db.init_schema(info.root)
except Exception as exc:
    st.error(f"DB 초기화 실패: {exc}")
    st.stop()


# 자동 ingest — 파일시스템(candidates.json / summaries / originals / extracted)
# 을 source-of-truth 로 삼아 DB 재생성. session_state 에 "한 번만" 기록.
if "papers_last_ingest" not in st.session_state:
    st.session_state["papers_last_ingest"] = None

col_refresh, col_stats = st.columns([1, 4])
with col_refresh:
    refresh = st.button("🔄 Refresh", help="파일시스템 스캔 후 DB 재생성")
with col_stats:
    last = st.session_state["papers_last_ingest"]
    if last:
        stats_text = " · ".join(f"{k}={v}" for k, v in last["stats"].items())
        st.caption(f"마지막 ingest: {last['ts']} — {stats_text}")
    else:
        st.caption("DB ingest 대기 중 (페이지 최초 진입 시 자동 실행)")

need_ingest = refresh or st.session_state["papers_last_ingest"] is None
if need_ingest:
    with st.spinner("파일시스템 스캔 → DB ingest..."):
        try:
            stats = db.ingest_from_filesystem(info.root)
            from datetime import datetime, timezone
            st.session_state["papers_last_ingest"] = {
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "stats": stats,
            }
            if refresh:
                st.toast(f"Ingest 완료: {stats}", icon="✅")
        except Exception as exc:
            st.error(f"Ingest 실패: {exc}")


with db.connect(info.root) as conn:
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

if total == 0:
    st.info(
        "DB 에 논문이 없습니다. Chat 에서 `lit_search` 워크플로우를 시작해 "
        "`candidates.json` 을 생성한 뒤 이 페이지의 **🔄 Refresh** 를 눌러 "
        "파일시스템을 다시 스캔하세요.\n\n"
        "ingest 는 다음 경로를 스캔합니다:\n"
        "- `**/candidates.json` (lit_search 산출)\n"
        "- `**/triaged.json` (lit-triage 결정)\n"
        "- `originals/papers/*.pdf` (다운로드된 본문)\n"
        "- `extracted/*/doc.md` (OCR 결과)\n"
        "- `agent-docs/summaries/*.json` (요약)"
    )
    st.stop()


colA, colB = st.columns([3, 1])
with colA:
    q = st.text_input("검색 (제목/초록 FTS)", placeholder="예: diffusion")
with colB:
    status_filter = st.selectbox(
        "상태 필터",
        ["(전체)", "discovered", "downloaded", "extracted", "summarized", "done", "failed"],
    )

if q.strip():
    rows = db.search_papers(info.root, q.strip(), limit=200)
elif status_filter != "(전체)":
    rows = db.list_papers_by_status(info.root, status_filter)
else:
    with db.connect(info.root) as conn:
        cur = conn.execute(
            "SELECT * FROM papers ORDER BY year DESC, title LIMIT 200"
        )
        rows = [dict(r) for r in cur.fetchall()]


st.caption(f"{len(rows)}편 표시 (전체 {total}편)")

for r in rows:
    with st.expander(
        f"[{r.get('year') or '—'}] {r.get('title') or r.get('id')}",
        expanded=False,
    ):
        cols = st.columns([3, 2])
        with cols[0]:
            st.markdown(f"**ID**: `{r.get('id')}`")
            st.markdown(f"**저자**: {r.get('authors') or '-'}")
            st.markdown(f"**Venue**: {r.get('venue') or '-'}")
            if r.get("doi"):
                st.markdown(f"**DOI**: {r['doi']}")
            if r.get("arxiv_id"):
                st.markdown(f"**arXiv**: `{r['arxiv_id']}`")
            if r.get("abstract"):
                st.caption(r["abstract"][:1200])
        with cols[1]:
            st.markdown(f"**Status**: `{r.get('status')}`")
            st.markdown(f"**Citations**: {r.get('citation_count') or '-'}")
            if r.get("original_path"):
                st.markdown(f"📄 `{r['original_path']}`")
            if r.get("extracted_path"):
                st.markdown(f"📝 `{r['extracted_path']}`")
            if r.get("pdf_url"):
                st.markdown(f"[PDF link]({r['pdf_url']})")
