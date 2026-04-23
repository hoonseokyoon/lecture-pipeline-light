"""Download PDFs for keep/maybe candidates.

URL 해결 우선순위:
1. candidate.pdf_url 가 있으면 사용
2. arxiv_id 가 있으면 https://arxiv.org/pdf/<id>.pdf

실패 시 needs_manual.json 에 기록 (사용자가 수동 획득).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from codex_runner import CodexRunError, current_cancel_event, load_skill
from http_utils import DEFAULT_RETRY_STATUSES

from _lib import browser_fetch

logger = logging.getLogger("lecture_pipeline.lit_fetch")

LogCb = Callable[[str], None]

_USER_AGENT = (
    "lecture-pipeline-lit-fetch/0.1 "
    "(research tool, contact via project repo)"
)


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _is_cancelled() -> bool:
    ev = current_cancel_event.get()
    return ev is not None and ev.is_set()


def _slugify(title: str, year: int | None, maxlen: int = 60) -> str:
    # title 에서 앞 단어 몇 개 + 연도
    text = re.sub(r"[^\w\s-]", "", (title or "").lower())
    parts = text.split()[:6]
    stem = "-".join(parts) or "untitled"
    if year:
        stem = f"{year}-{stem}"
    return stem[:maxlen].rstrip("-")


def _resolve_pdf_candidates(c: dict) -> list[tuple[str, str]]:
    """한 논문에 대한 URL 후보 리스트 (strategy, url).

    순서: requests 로 잘 되는 것 먼저 → browser 필요한 것 나중.
    strategy 는 로그·리포트용 태그.
    """
    urls: list[tuple[str, str]] = []

    # 1. arXiv — 항상 JS 없이 direct
    if c.get("arxiv_id"):
        urls.append(("arxiv", f"https://arxiv.org/pdf/{c['arxiv_id']}.pdf"))

    # 2. EuropePMC direct (PMC 논문이면 PMC 보다 먼저 시도 — JS 불필요)
    pmc_id = c.get("pmc_id")
    if pmc_id:
        eupmc = browser_fetch.europepmc_pdf_url(pmc_id)
        if eupmc:
            urls.append(("europepmc", eupmc))

    # 3. candidate.pdf_url (search 결과에서 받은 기본)
    if c.get("pdf_url"):
        urls.append(("primary", c["pdf_url"]))

    # 4. PMC direct (마지막 — JS challenge, browser 필요)
    if pmc_id:
        pmc_url = browser_fetch.pmc_article_url(pmc_id)
        if pmc_url:
            urls.append(("pmc-browser", pmc_url))

    # dedup while preserving order
    seen = set()
    out: list[tuple[str, str]] = []
    for strat, u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append((strat, u))
    return out


_JS_CHALLENGE_HOSTS = (
    "ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "frontiersin.org",  # 일부 Frontiers URL 이 JS 리다이렉트
)


def _needs_browser_by_url(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in _JS_CHALLENGE_HOSTS)


def _download(
    url: str,
    dest: Path,
    timeout: int,
    min_bytes: int,
    *,
    max_attempts: int = 3,
) -> tuple[bool, str, int]:
    """단일 PDF 다운로드 — 429/5xx/timeout 에 대해 exponential backoff 재시도.

    반환: (success, reason, bytes)
    """
    last_reason = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            with requests.get(
                url,
                stream=True,
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"},
                allow_redirects=True,
            ) as resp:
                status = resp.status_code
                if status in DEFAULT_RETRY_STATUSES and attempt < max_attempts:
                    retry_after = resp.headers.get("Retry-After", "").strip()
                    try:
                        wait = float(retry_after) if retry_after else 0
                    except ValueError:
                        wait = 0
                    if not wait:
                        wait = min(20.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        "[lit_fetch] HTTP %d — %.1fs 후 재시도 %d/%d: %s",
                        status, wait, attempt, max_attempts, url[:80],
                    )
                    time.sleep(wait)
                    last_reason = f"HTTP {status}"
                    continue
                if status >= 400:
                    return False, f"HTTP {status}", 0

                ctype = (resp.headers.get("Content-Type") or "").lower()
                pdf_hint = "pdf" in ctype or "octet-stream" in ctype
                first = b""
                if not pdf_hint:
                    first = resp.raw.read(5) if resp.raw else b""
                    if first[:4] != b"%PDF":
                        return False, f"not a PDF (ctype={ctype[:60]})", 0

                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    if first:
                        f.write(first)
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                size = dest.stat().st_size
                if size < min_bytes:
                    dest.unlink(missing_ok=True)
                    return False, f"file too small ({size}B)", size
                return True, "ok", size

        except requests.Timeout:
            last_reason = "timeout"
            if attempt == max_attempts:
                return False, "timeout", 0
            wait = min(20.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "[lit_fetch] timeout — %.1fs 후 재시도 %d/%d: %s",
                wait, attempt, max_attempts, url[:80],
            )
            time.sleep(wait)
            continue
        except (requests.ConnectionError, requests.RequestException) as exc:
            last_reason = f"request error: {exc.__class__.__name__}"
            if isinstance(exc, requests.ConnectionError) and attempt < max_attempts:
                wait = min(20.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "[lit_fetch] connection error — %.1fs 후 재시도 %d/%d: %s",
                    wait, attempt, max_attempts, exc,
                )
                time.sleep(wait)
                continue
            # 기타 RequestException 은 단발성 실패로 취급
            return False, last_reason, 0

    return False, f"{last_reason} (재시도 {max_attempts}회 exhausted)", 0


def run_lit_fetch(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback or (lambda _m: None)

    json_inputs = [p for p in input_paths if p.suffix.lower() == ".json"]
    if not json_inputs:
        raise CodexRunError("lit_fetch: .json 입력 필요")

    src_path = json_inputs[0]
    try:
        data = json.loads(src_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CodexRunError(f"lit_fetch: JSON 파싱 실패: {exc}")

    candidates = data.get("candidates") or data.get("items") or data
    if not isinstance(candidates, list):
        raise CodexRunError("lit_fetch: candidates 리스트 형태 필요")

    cfg = load_skill(skill_dir).config
    timeout = int(cfg.get("timeout", 60))
    max_concurrent = int(cfg.get("max_concurrent", 4))
    delay = float(cfg.get("request_delay_s", 0.5))
    min_bytes = int(cfg.get("min_bytes", 5000))
    only_statuses: set[str] = set(cfg.get("only_statuses") or [])
    use_browser = bool(cfg.get("use_browser", True))
    browser_headless = bool(cfg.get("browser_headless", True))
    browser_timeout = int(cfg.get("browser_timeout_s", 90))
    storage_state = os.environ.get("LIT_FETCH_STORAGE_STATE", "").strip() or None
    # Real Chrome mode (config or env 오버라이드)
    use_real_chrome = bool(cfg.get("use_real_chrome", False))
    if os.environ.get("LIT_USE_REAL_CHROME", "").strip() in ("1", "true", "True"):
        use_real_chrome = True
    chrome_channel = (
        os.environ.get("LIT_CHROME_CHANNEL", "").strip()
        or cfg.get("chrome_channel", "chrome")
    )
    chrome_user_data_dir = (
        os.environ.get("LIT_CHROME_USER_DATA_DIR", "").strip()
        or cfg.get("chrome_user_data_dir", "").strip()
        or None
    )
    # Real Chrome 모드 + Cloudflare-heavy publisher (ACS 등) 대응을 위해
    # headful 강제. Cloudflare 의 cf_clearance 쿠키가 headful fingerprint 에
    # 바인딩되어 headless 에서는 chain 이 재사용되지 않음.
    real_chrome_force_headful = bool(cfg.get("real_chrome_force_headful", True))
    if use_real_chrome and real_chrome_force_headful:
        browser_headless = False

    # triaged.json 대응 — decision 필드가 있으면 필터
    filtered = []
    for c in candidates:
        if only_statuses and c.get("decision") and c["decision"] not in only_statuses:
            continue
        filtered.append(c)

    _emit(
        log,
        f"[lit_fetch] 다운로드 대상 {len(filtered)}편 "
        f"(원본 candidates {len(candidates)})",
    )

    outputs: dict[str, bytes] = {}
    report: list[dict] = []
    needs_manual: list[dict] = []
    # phase 1 이후 browser 재시도 대상
    browser_queue: list[tuple[dict, list[tuple[str, str]], Path, str]] = []

    def _phase1_one(c: dict) -> dict:
        """Phase 1: requests-only. URL 후보들 순서대로 시도. browser-only URL
        은 건너뛰고 phase 2 에 위임."""
        if _is_cancelled():
            return {"id": c.get("id"), "ok": False, "reason": "cancelled"}
        url_candidates = _resolve_pdf_candidates(c)
        slug = _slugify(c.get("title", ""), c.get("year"))
        item: dict = {
            "id": c.get("id"),
            "title": c.get("title"),
            "slug": slug,
            "candidates": [{"strategy": s, "url": u} for s, u in url_candidates],
            "attempts": [],
            "ok": False,
        }
        if not url_candidates:
            item["reason"] = "no pdf_url / arxiv_id / pmc_id"
            return item

        dest = Path(f"papers/{slug}.pdf")
        item["dest"] = str(dest)

        for strat, url in url_candidates:
            if _is_cancelled():
                item["reason"] = "cancelled"
                return item
            if strat == "pmc-browser" or _needs_browser_by_url(url):
                # phase 2 로 미룸
                item["attempts"].append(
                    {"strategy": strat, "url": url, "phase": "deferred"}
                )
                continue
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tmp = Path(tf.name)
            try:
                ok, reason, size = _download(url, tmp, timeout, min_bytes)
                item["attempts"].append({
                    "strategy": strat, "url": url, "phase": "requests",
                    "ok": ok, "reason": reason, "bytes": size,
                })
                if ok:
                    item["ok"] = True
                    item["reason"] = f"{strat}:{reason}"
                    item["bytes"] = size
                    item["data"] = tmp.read_bytes()
                    item["winning_strategy"] = strat
                    item["winning_url"] = url
                    return item
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        # 모든 requests 시도 실패 → browser 시도 대상 남아있나 ?
        browser_urls = [
            (s, u) for s, u in url_candidates
            if s == "pmc-browser" or _needs_browser_by_url(u)
        ]
        if browser_urls:
            item["reason"] = "requests phase 전부 실패 — browser phase 로 이동"
            item["_browser_urls"] = browser_urls
            item["_dest"] = dest
        else:
            item["reason"] = (
                "모든 URL 후보 requests 실패 (browser 경로 없음)"
            )
        return item

    # Phase 1: 병렬 (스로틀)
    _emit(log, "[lit_fetch] phase 1 — requests 기반 병렬 다운로드")
    workers = max(1, min(max_concurrent, len(filtered)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = []
        for c in filtered:
            if _is_cancelled():
                break
            futures.append(ex.submit(_phase1_one, c))
            if delay > 0:
                time.sleep(delay)
        for fut in as_completed(futures):
            item = fut.result()
            if item.get("ok"):
                data = item.pop("data", None)
                if data:
                    outputs[item["dest"]] = data
                    _emit(
                        log,
                        f"[lit_fetch] ✓ {item['slug']} "
                        f"({item.get('bytes',0):,}B, {item.get('winning_strategy')})",
                    )
            elif "_browser_urls" in item:
                # phase 2 로
                browser_urls = item.pop("_browser_urls")
                dest = item.pop("_dest")
                browser_queue.append(
                    (
                        {"id": item["id"], "title": item["title"], "slug": item["slug"]},
                        browser_urls,
                        dest,
                        item.get("reason", ""),
                    )
                )
                item["status"] = "deferred-to-browser"
            else:
                _emit(log, f"[lit_fetch] ✗ {item.get('slug')} — {item.get('reason')}")
                needs_manual.append({
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "attempts": item.get("attempts", []),
                    "reason": item.get("reason"),
                })
            report.append({k: v for k, v in item.items() if k != "data"})

    # Phase 2: browser 순차
    if browser_queue and not _is_cancelled():
        if not use_browser:
            _emit(
                log,
                f"[lit_fetch] phase 2 skip — use_browser=false "
                f"({len(browser_queue)}편을 needs_manual 로)",
            )
            for meta, urls, dest, prev in browser_queue:
                needs_manual.append({
                    "id": meta["id"],
                    "title": meta["title"],
                    "url_tried": [u for _, u in urls],
                    "reason": f"browser 비활성 (phase1 fail: {prev})",
                })
        else:
            ok_avail, msg_avail = browser_fetch.is_available()
            if not ok_avail:
                _emit(log, f"[lit_fetch] phase 2 skip — {msg_avail}")
                for meta, urls, _, prev in browser_queue:
                    needs_manual.append({
                        "id": meta["id"],
                        "title": meta["title"],
                        "url_tried": [u for _, u in urls],
                        "reason": f"playwright 미설치: {msg_avail}",
                    })
            else:
                _emit(
                    log,
                    f"[lit_fetch] phase 2 — browser 순차 다운로드 "
                    f"({len(browser_queue)}편)",
                )
                try:
                    with browser_fetch.BrowserSession(
                        headless=browser_headless,
                        storage_state=storage_state,
                        download_timeout_s=browser_timeout,
                        use_real_chrome=use_real_chrome,
                        chrome_user_data_dir=chrome_user_data_dir,
                        chrome_channel=chrome_channel,
                    ) as sess:
                        for meta, urls, dest, prev in browser_queue:
                            if _is_cancelled():
                                break
                            got = False
                            for strat, url in urls:
                                with tempfile.NamedTemporaryFile(
                                    suffix=".pdf", delete=False,
                                ) as tf:
                                    tmp = Path(tf.name)
                                try:
                                    ok, reason, size = sess.download_pdf(
                                        url, tmp, min_bytes=min_bytes,
                                    )
                                    _emit(
                                        log,
                                        f"[lit_fetch] browser {strat} "
                                        f"{meta['slug']}: "
                                        f"{'✓' if ok else '✗'} {reason}",
                                    )
                                    if ok:
                                        outputs[str(dest)] = tmp.read_bytes()
                                        got = True
                                        # phase1 report 보강
                                        for r in report:
                                            if r.get("id") == meta["id"]:
                                                r["ok"] = True
                                                r["bytes"] = size
                                                r["winning_strategy"] = f"browser-{strat}"
                                                r["winning_url"] = url
                                                r.setdefault(
                                                    "attempts", []
                                                ).append({
                                                    "strategy": f"browser-{strat}",
                                                    "url": url,
                                                    "phase": "browser",
                                                    "ok": True,
                                                    "bytes": size,
                                                })
                                                r.pop("status", None)
                                                break
                                        break
                                    else:
                                        for r in report:
                                            if r.get("id") == meta["id"]:
                                                r.setdefault(
                                                    "attempts", []
                                                ).append({
                                                    "strategy": f"browser-{strat}",
                                                    "url": url,
                                                    "phase": "browser",
                                                    "ok": False,
                                                    "reason": reason,
                                                })
                                                break
                                finally:
                                    try:
                                        tmp.unlink(missing_ok=True)
                                    except OSError:
                                        pass
                            if not got:
                                needs_manual.append({
                                    "id": meta["id"],
                                    "title": meta["title"],
                                    "url_tried": [u for _, u in urls],
                                    "reason": "browser phase 도 실패",
                                })
                except RuntimeError as exc:
                    _emit(log, f"[lit_fetch] phase 2 browser 세션 실패: {exc}")
                    for meta, urls, _, prev in browser_queue:
                        needs_manual.append({
                            "id": meta["id"],
                            "title": meta["title"],
                            "url_tried": [u for _, u in urls],
                            "reason": f"browser 세션 실패: {exc}",
                        })

    outputs["download_report.json"] = json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input": src_path.name,
            "total_considered": len(candidates),
            "total_attempted": len(filtered),
            "succeeded": sum(1 for r in report if r.get("ok")),
            "failed": sum(1 for r in report if not r.get("ok")),
            "items": report,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    outputs["needs_manual.json"] = json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(needs_manual),
            "items": needs_manual,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    _emit(
        log,
        f"[lit_fetch] 완료 — 성공 {sum(1 for r in report if r.get('ok'))}"
        f" / 실패 {len(needs_manual)}",
    )
    return outputs
