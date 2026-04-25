"""Literature search across Semantic Scholar + arXiv + PubMed + EuropePMC.

간단한 전략:
1. query.json 또는 requirements.md 읽기 → query 문자열 확정
2. 각 source 에 순차 호출 (rate limit 준수)
3. DOI / arXiv id / PMID / 정규화된 title 기준으로 dedup
4. 간이 score: (citation_count scaled) + (keyword overlap) + (연도 보너스)
5. candidates.json 저장

network_access 필요. harness venv 에 `requests` 가 있으므로 사용.

Source 별 강점:
- semantic_scholar: CS/ML 영역에서 강함. citation graph. API key 권장.
- arxiv: 물리/수학/CS preprint. biology 는 q-bio 로 소수.
- pubmed: 생물/의학 gold standard. 무료, 무키 3 req/s.
- europepmc: PMC/PMCID/PDF URL 확보에 강함. biomedical full-text seed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from codex_runner import CodexRunError, current_cancel_event, load_skill
from http_utils import (
    CrossProcessRateLimiter,
    HttpError,
    get_with_retry,
)

logger = logging.getLogger("lecture_pipeline.lit_search")

# Per-source rate limiter — **크로스 프로세스** (tempdir lockfile 기반).
# Head Agent 가 `run_skill.py lit_search` 를 연속 호출해도 여러 Python 프로세스
# 사이에서 rate 를 공유하기 때문에 외부 API 쪽에서 429 유발 안 됨.
#
# - SS: 공식 1 req/s. 보수적 0.9.
# - PubMed: 무키 3 req/s 허용. 2.5 (PUBMED_API_KEY 있으면 추후 상향 검토).
# - EuropePMC: polite 2 req/s.
# - arXiv: 요청간 3s 권장. 0.33 req/s.
_LIMITERS = {
    "semantic_scholar": CrossProcessRateLimiter(rps=0.9, name="semantic-scholar"),
    "pubmed": CrossProcessRateLimiter(rps=2.5, name="pubmed"),
    "europepmc": CrossProcessRateLimiter(rps=2.0, name="europepmc"),
    "arxiv": CrossProcessRateLimiter(rps=0.33, name="arxiv"),
}

_DOMAIN_PROFILE_SOURCES = {
    "biomed": ["pubmed", "semantic_scholar", "europepmc"],
    "ml_cs": ["semantic_scholar", "arxiv"],
    "physics": ["arxiv", "semantic_scholar"],
    "mixed": ["semantic_scholar", "pubmed", "arxiv"],
}

_STOPWORDS = {
    "and", "are", "for", "from", "how", "into", "its", "the", "their", "this",
    "that", "with", "without", "using", "used", "uses", "use", "paper", "study",
    "review", "survey", "model", "models", "foundation", "analysis",
}


def _get_limiter(source: str) -> CrossProcessRateLimiter:
    return _LIMITERS.get(source) or CrossProcessRateLimiter(
        rps=1.0, name=f"src-{source}",
    )

LogCb = Callable[[str], None]


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


def _raise_if_cancelled() -> None:
    if _is_cancelled():
        raise CodexRunError("lit_search: 사용자 취소")


# ── 입력 파싱 ──


def _parse_query_spec(inputs: list[Path]) -> dict:
    """query.json 이 있으면 우선, 없으면 requirements.md 본문."""
    qjson = next(
        (p for p in inputs if p.name.lower().endswith(".json")), None,
    )
    if qjson is not None:
        try:
            data = json.loads(qjson.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexRunError(f"lit_search: query json 파싱 실패: {exc}")
        if not isinstance(data, dict) or not data.get("query"):
            raise CodexRunError("lit_search: query 필드 필요")
        return data

    md = next(
        (p for p in inputs if p.name.lower().endswith(".md")), None,
    )
    if md is not None:
        text = md.read_text(encoding="utf-8")
        # requirements.md 의 `## Search Query` 섹션을 우선 추출
        m = re.search(
            r"(?im)^##\s+(Search\s+Query|검색\s*쿼리)\s*\n+(.+?)(?=^##|\Z)",
            text,
            re.S,
        )
        if m:
            q = m.group(2).strip()
        else:
            # 첫 수백 글자를 query 로
            q = " ".join(text.split())[:400]
        return {"query": q}

    raise CodexRunError("lit_search: 유효 입력 없음")


# ── Semantic Scholar ──


_SS_FIELDS = ",".join([
    "paperId", "externalIds", "title", "abstract", "authors",
    "year", "venue", "citationCount", "openAccessPdf",
])


def _fetch_semantic_scholar(
    query: str,
    max_n: int,
    year_min: int | None,
    year_max: int | None,
    timeout: int,
) -> tuple[list[dict], dict]:
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(100, max_n),
        "fields": _SS_FIELDS,
    }
    if year_min and year_max:
        params["year"] = f"{year_min}-{year_max}"
    elif year_min:
        params["year"] = f"{year_min}-"

    headers = {"Accept": "application/json"}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    try:
        resp = get_with_retry(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            rate_limiter=_get_limiter("semantic_scholar"),
            log_name="SS-search",
        )
    except HttpError as exc:
        raise CodexRunError(f"Semantic Scholar 연결 반복 실패: {exc}") from exc
    if resp.status_code >= 300:
        raise CodexRunError(
            f"Semantic Scholar 실패 ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    items = data.get("data", []) or []
    return items, data


def _ss_to_candidate(item: dict) -> dict:
    ex = item.get("externalIds") or {}
    doi = ex.get("DOI")
    arxiv = ex.get("ArXiv")
    authors = [a.get("name") for a in (item.get("authors") or []) if a.get("name")]
    pdf_url = None
    if item.get("openAccessPdf"):
        pdf_url = item["openAccessPdf"].get("url")
    paper_id = item.get("paperId") or f"ss-{hash(item.get('title','') )}"
    return {
        "id": f"ss-{paper_id}",
        "source": "semantic_scholar",
        "title": item.get("title") or "",
        "authors": authors,
        "year": item.get("year"),
        "venue": item.get("venue"),
        "abstract": item.get("abstract") or "",
        "doi": doi,
        "arxiv_id": arxiv,
        "pdf_url": pdf_url,
        "citation_count": item.get("citationCount"),
    }


# ── arXiv ──


_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _fetch_arxiv(
    query: str,
    max_n: int,
    year_min: int | None,
    year_max: int | None,
    timeout: int,
) -> tuple[list[dict], str]:
    base = "http://export.arxiv.org/api/query"
    q = f"all:{urllib.parse.quote(query)}"
    url = (
        f"{base}?search_query={q}&start=0&max_results={max_n}"
        "&sortBy=relevance&sortOrder=descending"
    )
    try:
        resp = get_with_retry(
            url,
            timeout=timeout,
            rate_limiter=_get_limiter("arxiv"),
            log_name="arXiv",
        )
    except HttpError as exc:
        raise CodexRunError(f"arXiv 연결 반복 실패: {exc}") from exc
    if resp.status_code >= 300:
        raise CodexRunError(
            f"arXiv 실패 ({resp.status_code}): {resp.text[:300]}"
        )
    raw = resp.text
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CodexRunError(f"arXiv XML 파싱 실패: {exc}") from exc

    entries: list[dict] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        def _text(tag: str) -> str | None:
            el = entry.find(f"atom:{tag}", _ARXIV_NS)
            return el.text.strip() if el is not None and el.text else None

        title = _text("title") or ""
        summary = _text("summary") or ""
        published = _text("published") or ""
        year: int | None = None
        if len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])
        if year_min and year and year < year_min:
            continue
        if year_max and year and year > year_max:
            continue

        authors = [
            (a.findtext("atom:name", default="", namespaces=_ARXIV_NS) or "").strip()
            for a in entry.findall("atom:author", _ARXIV_NS)
        ]
        authors = [a for a in authors if a]

        # id 는 http://arxiv.org/abs/2404.01234v1 형태
        id_full = _text("id") or ""
        arxiv_id = id_full.rsplit("/", 1)[-1] if "/" in id_full else id_full
        # 버전 suffix 제거
        arxiv_id_no_ver = re.sub(r"v\d+$", "", arxiv_id)

        pdf_url = None
        for link in entry.findall("atom:link", _ARXIV_NS):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break

        entries.append({
            "id": f"arxiv-{arxiv_id_no_ver}",
            "source": "arxiv",
            "title": re.sub(r"\s+", " ", title).strip(),
            "authors": authors,
            "year": year,
            "venue": "arXiv",
            "abstract": re.sub(r"\s+", " ", summary).strip(),
            "doi": None,
            "arxiv_id": arxiv_id_no_ver,
            "pdf_url": pdf_url,
            "citation_count": None,
        })

    return entries, raw


# ── PubMed (NCBI E-utilities) ──


_PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _pubmed_common_params() -> dict:
    """NCBI 권장: tool + email 식별자 포함 → rate limit 완화 가능."""
    params: dict = {"tool": "lecture-pipeline-lit-search"}
    email = os.environ.get("NCBI_EMAIL", "").strip()
    if email:
        params["email"] = email
    api_key = os.environ.get("PUBMED_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    return params


def _fetch_pubmed(
    query: str,
    max_n: int,
    year_min: int | None,
    year_max: int | None,
    timeout: int,
) -> tuple[list[dict], str]:
    """PubMed 검색 → esearch(PMID 목록) → efetch(XML 메타데이터).

    - esearch 는 JSON 응답 → PMID 리스트
    - efetch 는 XML 응답 → PubmedArticle 트리
    - 실패 시 CodexRunError (호출 측에서 source 실패로 처리)
    """
    # Step 1. esearch
    es_params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(min(100, max_n)),
        "sort": "relevance",
        **_pubmed_common_params(),
    }
    if year_min or year_max:
        es_params["datetype"] = "pdat"
        if year_min:
            es_params["mindate"] = f"{year_min}/01/01"
        if year_max:
            es_params["maxdate"] = f"{year_max}/12/31"

    try:
        resp = get_with_retry(
            _PUBMED_ESEARCH,
            params=es_params,
            timeout=timeout,
            rate_limiter=_get_limiter("pubmed"),
            log_name="PubMed-esearch",
        )
    except HttpError as exc:
        raise CodexRunError(f"PubMed esearch 연결 반복 실패: {exc}") from exc
    if resp.status_code >= 300:
        raise CodexRunError(
            f"PubMed esearch 실패 ({resp.status_code}): {resp.text[:300]}"
        )
    try:
        es_data = resp.json()
    except ValueError as exc:
        raise CodexRunError(f"PubMed esearch JSON 파싱 실패: {exc}") from exc

    idlist = (es_data.get("esearchresult") or {}).get("idlist") or []
    if not idlist:
        return [], json.dumps(es_data, ensure_ascii=False)

    # Step 2. efetch (XML)
    ef_params = {
        "db": "pubmed",
        "id": ",".join(idlist),
        "retmode": "xml",
        **_pubmed_common_params(),
    }
    try:
        resp2 = get_with_retry(
            _PUBMED_EFETCH,
            params=ef_params,
            timeout=timeout,
            rate_limiter=_get_limiter("pubmed"),
            log_name="PubMed-efetch",
        )
    except HttpError as exc:
        raise CodexRunError(f"PubMed efetch 연결 반복 실패: {exc}") from exc
    if resp2.status_code >= 300:
        raise CodexRunError(
            f"PubMed efetch 실패 ({resp2.status_code}): {resp2.text[:300]}"
        )
    xml_raw = resp2.text
    try:
        root = ET.fromstring(xml_raw)
    except ET.ParseError as exc:
        raise CodexRunError(f"PubMed XML 파싱 실패: {exc}") from exc

    candidates = []
    for article in root.findall("PubmedArticle"):
        candidates.append(_pubmed_article_to_candidate(article))

    combined_raw = json.dumps(
        {"esearch": es_data, "efetch_xml_len": len(xml_raw)}, ensure_ascii=False,
    )
    return [c for c in candidates if c], combined_raw


def _pubmed_article_to_candidate(article: ET.Element) -> dict | None:
    medline = article.find("MedlineCitation")
    if medline is None:
        return None
    pmid_el = medline.find("PMID")
    pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""
    if not pmid:
        return None

    art = medline.find("Article")
    if art is None:
        return None

    title_el = art.find("ArticleTitle")
    title = _extract_mixed_text(title_el) if title_el is not None else ""

    # Abstract — 여러 AbstractText (Label 포함) 를 이어붙임
    abs_el = art.find("Abstract")
    abstract_parts: list[str] = []
    if abs_el is not None:
        for at in abs_el.findall("AbstractText"):
            label = at.get("Label") or at.get("NlmCategory")
            txt = _extract_mixed_text(at)
            if label:
                abstract_parts.append(f"{label}: {txt}")
            else:
                abstract_parts.append(txt)
    abstract = " ".join(p for p in abstract_parts if p).strip()

    # Authors
    authors: list[str] = []
    for au in art.findall("AuthorList/Author"):
        ln = au.findtext("LastName") or ""
        fn = au.findtext("ForeName") or au.findtext("Initials") or ""
        name = (f"{ln} {fn}".strip()) or (au.findtext("CollectiveName") or "")
        if name:
            authors.append(name)

    # Venue + Year
    journal_el = art.find("Journal")
    venue = ""
    year: int | None = None
    if journal_el is not None:
        venue = (
            journal_el.findtext("ISOAbbreviation")
            or journal_el.findtext("Title")
            or ""
        )
        pub_date = journal_el.find("JournalIssue/PubDate")
        if pub_date is not None:
            y = pub_date.findtext("Year")
            if not y:
                # Some records use MedlineDate like "2023 Mar-Apr"
                medline_date = pub_date.findtext("MedlineDate") or ""
                m = re.search(r"(\d{4})", medline_date)
                if m:
                    y = m.group(1)
            if y and y.isdigit():
                year = int(y)

    # DOI / PMC from ArticleIdList
    doi = None
    pmc_id = None
    id_list = article.find("PubmedData/ArticleIdList")
    if id_list is not None:
        for aid in id_list.findall("ArticleId"):
            it = (aid.get("IdType") or "").lower()
            val = (aid.text or "").strip()
            if it == "doi" and val:
                doi = val
            elif it == "pmc" and val:
                pmc_id = val

    # PDF URL — PMC 에 오픈 액세스 버전이 있으면 거기로 (없으면 None 두기)
    pdf_url = None
    if pmc_id:
        pid = pmc_id if pmc_id.startswith("PMC") else f"PMC{pmc_id}"
        pdf_url = f"https://europepmc.org/articles/{pid}?pdf=render"

    return {
        "id": f"pubmed-{pmid}",
        "source": "pubmed",
        "title": re.sub(r"\s+", " ", title).strip(),
        "authors": authors,
        "year": year,
        "venue": venue,
        "abstract": abstract,
        "doi": doi,
        "arxiv_id": None,
        "pmid": pmid,
        "pmc_id": pmc_id,
        "pdf_url": pdf_url,
        "citation_count": None,
    }


def _extract_mixed_text(el: ET.Element) -> str:
    """<Element>text <sub>foo</sub> tail</Element> 에서 텍스트만 수집."""
    if el is None:
        return ""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_extract_mixed_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


# ── EuropePMC ──


_EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _normalize_pmc_id(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    m = re.match(r"(?i)^(PMC)?(\d+)$", s)
    if not m:
        return None
    return f"PMC{m.group(2)}"


def _europepmc_pdf_url(pmcid: str | None) -> str | None:
    norm = _normalize_pmc_id(pmcid)
    if not norm:
        return None
    return f"https://europepmc.org/articles/{norm}?pdf=render"


def _fetch_europepmc(
    query: str,
    max_n: int,
    year_min: int | None,
    year_max: int | None,
    timeout: int,
) -> tuple[list[dict], dict]:
    epmc_query = query
    if year_min or year_max:
        lo = year_min or 1800
        hi = year_max or datetime.now(timezone.utc).year
        epmc_query = f"({query}) AND FIRST_PDATE:[{lo}-01-01 TO {hi}-12-31]"
    params = {
        "query": epmc_query,
        "format": "json",
        "resultType": "core",
        "pageSize": str(min(100, max_n)),
    }
    try:
        resp = get_with_retry(
            _EUROPEPMC_SEARCH,
            params=params,
            timeout=timeout,
            rate_limiter=_get_limiter("europepmc"),
            log_name="EuropePMC-search",
        )
    except HttpError as exc:
        raise CodexRunError(f"EuropePMC 연결 반복 실패: {exc}") from exc
    if resp.status_code >= 300:
        raise CodexRunError(
            f"EuropePMC 실패 ({resp.status_code}): {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise CodexRunError(f"EuropePMC JSON 파싱 실패: {exc}") from exc
    items = ((data.get("resultList") or {}).get("result") or [])
    return [_europepmc_to_candidate(it) for it in items], data


def _europepmc_to_candidate(item: dict) -> dict:
    pmid = item.get("pmid")
    pmcid = _normalize_pmc_id(item.get("pmcid"))
    doi = item.get("doi")
    title = re.sub(r"\s+", " ", item.get("title") or "").strip()
    authors_raw = item.get("authorString") or ""
    authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
    year = None
    y = item.get("pubYear") or ""
    if str(y).isdigit():
        year = int(y)
    try:
        cited_by = int(item.get("citedByCount")) if item.get("citedByCount") else None
    except (TypeError, ValueError):
        cited_by = None
    stable = pmcid or pmid or doi or str(abs(hash(title)))
    return {
        "id": f"europepmc-{stable}",
        "source": "europepmc",
        "title": title,
        "authors": authors,
        "year": year,
        "venue": item.get("journalTitle") or item.get("bookOrReportDetails") or "",
        "abstract": re.sub(r"\s+", " ", item.get("abstractText") or "").strip(),
        "doi": doi,
        "arxiv_id": None,
        "pmid": pmid,
        "pmc_id": pmcid,
        "pdf_url": _europepmc_pdf_url(pmcid),
        "citation_count": cited_by,
    }


# ── Dedup + Score ──


def _norm_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _dedup(candidates: list[dict]) -> list[dict]:
    """DOI > PMID > arxiv_id > 정규화 title 순으로 dedup.

    동일 논문이면 메타데이터 선호 순위: pubmed (venue·abstract 풍부) >
    semantic_scholar (citation count) > arxiv.
    """
    seen_doi: dict[str, int] = {}
    seen_pmid: dict[str, int] = {}
    seen_arxiv: dict[str, int] = {}
    seen_title: dict[str, int] = {}
    out: list[dict] = []

    source_priority = {
        "pubmed": 4,
        "europepmc": 3,
        "semantic_scholar": 2,
        "arxiv": 1,
    }

    def _merge(dst: dict, src: dict) -> dict:
        merged = dict(dst)
        for k, v in src.items():
            if not merged.get(k) and v:
                merged[k] = v
        # citation_count 는 더 큰 값 유지
        if (src.get("citation_count") or 0) > (merged.get("citation_count") or 0):
            merged["citation_count"] = src["citation_count"]
        # source priority — 더 높은 쪽의 id 채택
        if source_priority.get(src.get("source", ""), 0) > \
                source_priority.get(merged.get("source", ""), 0):
            merged["source"] = src["source"]
            merged["id"] = src.get("id") or merged["id"]
        return merged

    for c in candidates:
        idx = None
        if c.get("doi") and c["doi"] in seen_doi:
            idx = seen_doi[c["doi"]]
        elif c.get("pmid") and c["pmid"] in seen_pmid:
            idx = seen_pmid[c["pmid"]]
        elif c.get("arxiv_id") and c["arxiv_id"] in seen_arxiv:
            idx = seen_arxiv[c["arxiv_id"]]
        else:
            nt = _norm_title(c.get("title", ""))
            if nt and nt in seen_title:
                idx = seen_title[nt]

        if idx is not None:
            out[idx] = _merge(out[idx], c)
        else:
            out.append(c)
            i = len(out) - 1
            if c.get("doi"):
                seen_doi[c["doi"]] = i
            if c.get("pmid"):
                seen_pmid[c["pmid"]] = i
            if c.get("arxiv_id"):
                seen_arxiv[c["arxiv_id"]] = i
            nt = _norm_title(c.get("title", ""))
            if nt:
                seen_title[nt] = i

    return out


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"\w{3,}", (text or "").lower())
    return set(tokens)


def _score(candidate: dict, query_tokens: set[str]) -> float:
    """간이 heuristic. 0~1 근사, 완벽 정확도 불필요 (triage 단계에서 LLM 재순위)."""
    title_tok = _tokenize(candidate.get("title", ""))
    abs_tok = _tokenize(candidate.get("abstract", ""))
    overlap_title = len(query_tokens & title_tok)
    overlap_abs = len(query_tokens & abs_tok)
    coverage = 0.0
    if query_tokens:
        coverage = (2 * overlap_title + overlap_abs) / (3 * len(query_tokens))
    coverage = min(1.0, coverage)

    year = candidate.get("year") or 0
    year_bonus = 0.0
    if year >= 2024:
        year_bonus = 0.15
    elif year >= 2022:
        year_bonus = 0.08

    citations = candidate.get("citation_count") or 0
    import math
    citation_bonus = min(0.25, math.log10(citations + 1) * 0.08)

    return round(min(1.0, coverage + year_bonus + citation_bonus), 4)


def sources_for_domain_profile(profile: str | None) -> list[str] | None:
    if not profile:
        return None
    return _DOMAIN_PROFILE_SOURCES.get(str(profile).strip().lower())


def _resolve_sources(spec: dict, cfg: dict) -> tuple[list[str], str | None]:
    if spec.get("sources"):
        raw = spec["sources"]
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.split(",")]
        return [str(s).strip() for s in raw if str(s).strip()], spec.get("domain_profile")
    profile = spec.get("domain_profile") or cfg.get("domain_profile")
    prof_sources = sources_for_domain_profile(profile)
    if prof_sources:
        return prof_sources, str(profile).strip().lower()
    raw_cfg = cfg.get("sources", ["semantic_scholar", "arxiv"])
    if isinstance(raw_cfg, str):
        raw_cfg = [s.strip() for s in raw_cfg.split(",")]
    return [str(s).strip() for s in raw_cfg if str(s).strip()], None


def _sanity_query_tokens(query: str) -> set[str]:
    return {t for t in _tokenize(query) if t not in _STOPWORDS}


def _validate_sanity_gate(candidates: list[dict], query: str) -> None:
    """저장 전 coarse relevance gate.

    top 20 중 query signal token 이 제목/초록에 하나도 겹치지 않는 후보가
    절반 이상이면 source/query 선택 실패로 간주한다. 이 단계는 LLM triage
    이전의 값싼 guardrail 이므로 error 는 후보 파일 저장을 막는다.
    """
    top = candidates[:20]
    if not top:
        return
    signal = _sanity_query_tokens(query)
    if not signal:
        return
    low = 0
    for c in top:
        cand_tokens = _tokenize(
            f"{c.get('title', '')} {c.get('abstract', '')} {c.get('venue', '')}"
        )
        if not (signal & cand_tokens):
            low += 1
    if low / len(top) >= 0.5:
        raise CodexRunError(
            "lit_search sanity gate 실패: top 후보의 절반 이상이 query/domain "
            "token 과 겹치지 않습니다. query 또는 domain_profile 을 재작성하세요."
        )


# ── Entry ──


def run_lit_search(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    log = log_callback or (lambda _m: None)
    spec = _parse_query_spec(input_paths)
    query = spec["query"]
    cfg = load_skill(skill_dir).config

    sources, domain_profile = _resolve_sources(spec, cfg)
    max_per_source = int(
        spec.get("max_per_source") or cfg.get("max_per_source", 50)
    )
    year_min = spec.get("year_min")
    year_max = spec.get("year_max")
    timeout = int(cfg.get("timeout", 120))
    delay = float(cfg.get("request_delay_s", 1.0))

    profile_msg = f" domain_profile={domain_profile}" if domain_profile else ""
    _emit(log, f"[lit_search] query='{query[:80]}' sources={sources}{profile_msg}")

    collected: list[dict] = []
    outputs: dict[str, bytes] = {}

    for i, src in enumerate(sources):
        _raise_if_cancelled()
        if i > 0 and delay > 0:
            time.sleep(delay)
        if src == "semantic_scholar":
            _emit(log, f"[lit_search] Semantic Scholar 호출 (max={max_per_source})")
            try:
                items, raw = _fetch_semantic_scholar(
                    query, max_per_source, year_min, year_max, timeout,
                )
            except CodexRunError as exc:
                _emit(log, f"[lit_search] SS 실패 — 계속: {exc}")
                continue
            outputs["raw_semantic_scholar.json"] = json.dumps(
                raw, ensure_ascii=False, indent=2,
            ).encode("utf-8")
            mapped = [_ss_to_candidate(it) for it in items]
            _emit(log, f"[lit_search] SS 결과 {len(mapped)}건")
            collected.extend(mapped)

        elif src == "arxiv":
            _emit(log, f"[lit_search] arXiv 호출 (max={max_per_source})")
            try:
                items, raw_xml = _fetch_arxiv(
                    query, max_per_source, year_min, year_max, timeout,
                )
            except CodexRunError as exc:
                _emit(log, f"[lit_search] arXiv 실패 — 계속: {exc}")
                continue
            outputs["raw_arxiv.xml"] = raw_xml.encode("utf-8")
            _emit(log, f"[lit_search] arXiv 결과 {len(items)}건")
            collected.extend(items)

        elif src == "pubmed":
            _emit(log, f"[lit_search] PubMed 호출 (max={max_per_source})")
            try:
                items, raw_pm = _fetch_pubmed(
                    query, max_per_source, year_min, year_max, timeout,
                )
            except CodexRunError as exc:
                _emit(log, f"[lit_search] PubMed 실패 — 계속: {exc}")
                continue
            outputs["raw_pubmed.json"] = raw_pm.encode("utf-8")
            _emit(log, f"[lit_search] PubMed 결과 {len(items)}건")
            collected.extend(items)

        elif src == "europepmc":
            _emit(log, f"[lit_search] EuropePMC 호출 (max={max_per_source})")
            try:
                items, raw_epmc = _fetch_europepmc(
                    query, max_per_source, year_min, year_max, timeout,
                )
            except CodexRunError as exc:
                _emit(log, f"[lit_search] EuropePMC 실패 — 계속: {exc}")
                continue
            outputs["raw_europepmc.json"] = json.dumps(
                raw_epmc, ensure_ascii=False, indent=2,
            ).encode("utf-8")
            _emit(log, f"[lit_search] EuropePMC 결과 {len(items)}건")
            collected.extend(items)

        else:
            _emit(log, f"[lit_search] 미지원 source: {src} (skip)")

    _raise_if_cancelled()

    total = len(collected)
    deduped = _dedup(collected)
    _emit(log, f"[lit_search] dedup: {total} → {len(deduped)}")

    query_tokens = _tokenize(query)
    for c in deduped:
        c["score"] = _score(c, query_tokens)
    deduped.sort(key=lambda c: c.get("score", 0), reverse=True)
    _validate_sanity_gate(deduped, query)
    total_after = len(deduped)

    result = {
        "query": query,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "domain_profile": domain_profile,
        "year_min": year_min,
        "year_max": year_max,
        "total_before_dedup": total,
        "total_after_dedup": total_after,
        "candidates": deduped,
    }
    if result["total_after_dedup"] != len(result["candidates"]):
        raise CodexRunError("lit_search invariant 실패: total_after_dedup count mismatch")
    outputs["candidates.json"] = json.dumps(
        result, ensure_ascii=False, indent=2,
    ).encode("utf-8")

    _emit(log, f"[lit_search] 완료 — {len(deduped)}편, top score={deduped[0]['score'] if deduped else 'n/a'}")
    return outputs
