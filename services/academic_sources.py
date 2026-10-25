"""Free academic discovery and source-text comparison for plagiarism reports."""
import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from services.document_processor import extract

_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_CACHE_TTL = 60 * 60 * 24
_CACHE_LIMIT = 512
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_HEADERS = {"User-Agent": "SmartFiles/1.0 (academic similarity checker)"}
_LAST_REQUEST = {}
_PROVIDER_LOCKS = {name: threading.Lock() for name in ("crossref", "openalex", "semantic_scholar")}
_MAX_ACADEMIC_SCAN_SECONDS = 12 * 60
_MAX_FULLTEXT_PAPERS_PER_PASSAGE = 3
_MAX_SOURCE_URLS_PER_PAPER = 2
_MAX_CONCURRENT_SOURCE_FETCHES = 4
_MIN_SOURCE_WORDS = 20
_MIN_APPROXIMATE_JACCARD = 0.62
_MIN_MATCHING_TOKENS = 6
_MIN_PASSAGE_TOKEN_COVERAGE = 0.70
_HTML_NON_BODY_SECTIONS = {
    "abstract", "summary", "highlights", "keywords", "references", "bibliography",
    "acknowledgments", "acknowledgements", "author information", "funding", "conflicts of interest",
}
logger = logging.getLogger(__name__)


class _TextHTML(HTMLParser):
    """Extract visible text from an HTML page without treating markup as evidence."""
    _hidden = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0
        self.headings = []
        self.current_heading_tag = None
        self.current_heading_parts = []
        self.in_abstract = False

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag.casefold() in self._hidden:
            self.hidden_depth += 1
        if tag in {"h2", "h3", "h4", "h5", "h6"}:
            if tag == "h2":
                self.in_abstract = False
            self.current_heading_tag = tag
            self.current_heading_parts = []

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in {"h2", "h3", "h4", "h5", "h6"} and tag == self.current_heading_tag:
            heading_text = " ".join(self.current_heading_parts).strip()
            if tag == "h2":
                heading = heading_text.casefold()
                self.headings.append(heading)
                self.in_abstract = heading in _HTML_NON_BODY_SECTIONS
                if not self.in_abstract and heading_text:
                    self.parts.append(heading_text)
            self.current_heading_tag = None
            self.current_heading_parts = []
        if tag in self._hidden and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if self.current_heading_tag and not self.hidden_depth:
            self.current_heading_parts.append(data.strip())
        if not self.hidden_depth and not self.in_abstract and not self.current_heading_tag and data.strip():
            self.parts.append(data.strip())


def _cached(key, loader):
    with _CACHE_LOCK:
        now = time.time()
        item = _CACHE.get(key)
        if item and now - item[0] < _CACHE_TTL:
            _CACHE.move_to_end(key)
            return item[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return value


def _get(client, provider, url, params=None):
    # Conservative per-process spacing; provider endpoints are all public/free.
    gap = {"crossref": 0.12, "openalex": 0.12, "semantic_scholar": 1.0}.get(provider, 0.2)
    with _PROVIDER_LOCKS[provider]:
        delay = gap - (time.monotonic() - _LAST_REQUEST.get(provider, 0))
        if delay > 0:
            time.sleep(delay)
        response = _request_with_retry(client, url, params=params, provider=provider)
        _LAST_REQUEST[provider] = time.monotonic()
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{provider} returned an unexpected response schema")
    return payload


def _request_with_retry(client, url, params=None, provider="source_text"):
    """Retry only idempotent GETs for throttling, transient server errors and network timeouts."""
    for attempt in range(3):
        try:
            response = client.get(url, params=params, headers=_HEADERS, follow_redirects=True)
        except httpx.RequestError as error:
            logger.warning("Academic request failed provider=%s attempt=%d error=%s", provider, attempt + 1, type(error).__name__)
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
            continue
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            logger.info("Academic API response provider=%s host=%s status=%d attempt=%d",
                        provider, urlsplit(url).hostname, response.status_code, attempt + 1)
            return response
        logger.warning("Academic API temporary response provider=%s host=%s status=%d attempt=%d",
                       provider, urlsplit(url).hostname, response.status_code, attempt + 1)
        try:
            delay = float(response.headers.get("Retry-After", "0.5")) if response.status_code == 429 else 0.5 * (attempt + 1)
        except (TypeError, ValueError):
            delay = 0.5
        time.sleep(min(3.0, max(0.25, delay)))
    return response


def _search(client, provider, query):
    key = (provider, query.casefold())
    def load():
        if provider == "crossref":
            payload = _get(client, provider, "https://api.crossref.org/works", {
                "query.bibliographic": query, "rows": 5, "select": "DOI,title,author,published,URL",
            })
            message = payload.get("message")
            data = message.get("items") if isinstance(message, dict) else None
            if not isinstance(data, list):
                raise ValueError("Crossref returned an unexpected works schema")
            results = [{"doi": x.get("DOI"), "title": (x.get("title") or [""])[0],
                     "authors": [a.get("given", "") + " " + a.get("family", "") for a in (x.get("author") or []) if isinstance(a, dict)],
                     "url": x.get("URL", ""), "locations": []} for x in data if isinstance(x, dict)]
            logger.info("Academic source discovery provider=%s candidates=%d", provider, len(results))
            return results
        if provider == "openalex":
            payload = _get(client, provider, "https://api.openalex.org/works", {"search": query, "per-page": 5})
            data = payload.get("results")
            if not isinstance(data, list):
                raise ValueError("OpenAlex returned an unexpected works schema")
            result = []
            for x in data:
                if not isinstance(x, dict):
                    continue
                locs = list(dict.fromkeys(
                    url for location in (x.get("locations") or [])
                    if isinstance(location, dict)
                    for url in (location.get("pdf_url"), location.get("landing_page_url")) if url
                ))
                primary = x.get("primary_location") or {}
                best = primary.get("landing_page_url") or primary.get("pdf_url") or ""
                result.append({"doi": (x.get("doi") or "").removeprefix("https://doi.org/"),
                               "title": x.get("title", ""), "authors": [a.get("author", {}).get("display_name", "") for a in (x.get("authorships") or []) if isinstance(a, dict)],
                               "url": best or x.get("id", ""), "locations": list(dict.fromkeys(locs))})
            logger.info("Academic source discovery provider=%s candidates=%d", provider, len(result))
            return result
        payload = _get(client, provider, "https://api.semanticscholar.org/graph/v1/paper/search", {
            "query": query, "limit": 5, "fields": "title,authors,externalIds,openAccessPdf,url",
        })
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Semantic Scholar returned an unexpected paper schema")
        results = [{"doi": (x.get("externalIds") or {}).get("DOI"), "title": x.get("title", ""),
                 "authors": [a.get("name", "") for a in (x.get("authors") or []) if isinstance(a, dict)],
                 "url": (x.get("openAccessPdf") or {}).get("url") or x.get("url", ""),
                 "locations": [(x.get("openAccessPdf") or {}).get("url", "")] if (x.get("openAccessPdf") or {}).get("url") else []} for x in data if isinstance(x, dict)]
        logger.info("Academic source discovery provider=%s candidates=%d", provider, len(results))
        return results
    return _cached(key, load)


def _source_text(client, url, on_error=None):
    def load():
        response = _request_with_retry(client, url, provider="source_text")
        response.raise_for_status()
        data = response.content
        if len(data) > 20 * 1024 * 1024:
            logger.warning("Academic source text rejected url=%s response_bytes=%d reason=source_too_large", url, len(data))
            return ""
        ctype = response.headers.get("content-type", "").casefold()
        if "pdf" not in ctype and not data.startswith(b"%PDF"):
            media_type = ctype.split(";", 1)[0].strip()
            decoded = data.decode(response.encoding or "utf-8", errors="replace")
            if media_type in {"text/plain", "text/markdown"}:
                text = decoded[:2_000_000]
                logger.info("Academic source text retrieved url=%s status=%d media_type=%s chars=%d words=%d",
                            url, response.status_code, media_type, len(text), len(_tokens(text)))
                return text
            if media_type in {"text/html", "application/xhtml+xml"}:
                parser = _TextHTML()
                parser.feed(decoded)
                text = "\n".join(parser.parts)[:2_000_000]
                body_headings = [heading for heading in parser.headings
                                 if heading and heading not in _HTML_NON_BODY_SECTIONS]
                if not body_headings:
                    logger.info("Academic HTML excluded as metadata-only url=%s status=%d headings=%d",
                                url, response.status_code, len(parser.headings))
                    return ""
                logger.info("Academic source text retrieved url=%s status=%d media_type=%s chars=%d words=%d",
                            url, response.status_code, media_type, len(text), len(_tokens(text)))
                return text
            logger.info("Academic source text unavailable url=%s status=%d media_type=%s chars=0 words=0",
                        url, response.status_code, media_type or "unknown")
            return ""
        suffix = ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            path = f.name
        try:
            text, _ = extract(path, "document", ".pdf")
            text = (text or "")[:2_000_000]
            logger.info("Academic source text retrieved url=%s status=%d media_type=pdf chars=%d words=%d",
                        url, response.status_code, len(text), len(_tokens(text)))
            return text
        finally:
            try: os.unlink(path)
            except OSError: pass
    try:
        return _cached(("source", url), load)
    except Exception as error:
        logger.warning("Academic source fetch failed url=%s error=%s", url, type(error).__name__)
        if on_error:
            on_error(error)
        return ""


_DEFAULT_SOURCE_TEXT = _source_text


def _tokens(text):
    return re.findall(r"[\w'-]+", text.casefold(), flags=re.UNICODE)


def _best_evidence(passage, source):
    """Return a source excerpt aligned to the passage and its token Jaccard score."""
    passage_tokens = _tokens(passage)
    if not passage_tokens:
        return "", 0.0, False
    normalized_passage = " ".join(passage_tokens)
    if len(normalized_passage) >= 60:
        source_tokens = _tokens(source)
        normalized_source = " ".join(source_tokens)
        exact_at = normalized_source.find(normalized_passage)
        exact_end = exact_at + len(normalized_passage)
        if (exact_at >= 0 and (exact_at == 0 or normalized_source[exact_at - 1] == " ")
                and (exact_end == len(normalized_source) or normalized_source[exact_end] == " ")):
            token_start = normalized_source.count(" ", 0, exact_at)
            spans = [match.span() for match in re.finditer(r"[\w'-]+", source, flags=re.UNICODE)]
            if token_start + len(passage_tokens) <= len(spans):
                return source[spans[token_start][0]:spans[token_start + len(passage_tokens) - 1][1]], 1.0, True
    pset = set(passage_tokens)
    sentences = [line.strip() for line in re.split(r"(?<=[.!?])\s+|[\r\n]+", source) if line.strip()]
    best_evidence, best_score = "", 0.0
    for sentence in sentences:
        sentence_tokens = set(_tokens(sentence))
        score = len(pset & sentence_tokens) / max(1, len(pset | sentence_tokens))
        if score > best_score:
            best_evidence, best_score = sentence, score
    return best_evidence, best_score, False


def compare(passages, report, progress_callback=None, scan_id=None):
    """Synchronous compatibility entry point for CLI/tests; the scan worker uses async concurrency."""
    return asyncio.run(compare_async(passages, report, progress_callback, scan_id))


async def compare_async(passages, report, progress_callback=None, scan_id=None):
    """Discover papers and retain a match only when source body text supports it."""
    providers = ("crossref", "openalex", "semantic_scholar")
    report.update(online_search_provider="crossref+openalex+semantic_scholar",
                  online_search_mode="academic_fulltext_v1", online_search_status="searching",
                  online_matches=[], sources=[], searches_attempted=0, searches_completed=0,
                  searches_failed=0, online_pages_analyzed=0)
    source_map, match_map = {}, {}
    covered = set()
    report["source_fetches_attempted"] = 0
    report["source_fetches_failed"] = 0
    report["academic_passages_searched"] = 0
    report["academic_passages_skipped"] = 0
    report["academic_candidates_discovered"] = 0
    report["source_texts_retrieved"] = 0
    report["candidate_matches"] = 0
    report["final_matches"] = 0
    logger.info("Academic scan started scan_id=%s passages=%d", scan_id, len(passages))
    scan_started = time.perf_counter()
    deadline = time.monotonic() + _MAX_ACADEMIC_SCAN_SECONDS
    async_tasks = {}
    source_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SOURCE_FETCHES)
    source_failure_lock = threading.Lock()

    def record_source_failure(_error):
        with source_failure_lock:
            report["source_fetches_failed"] += 1

    async def shared_call(key, function, *args):
        task = async_tasks.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            async_tasks[key] = task
        return await task

    async def source_text(url):
        async with source_semaphore:
            if _source_text is _DEFAULT_SOURCE_TEXT:
                return await shared_call(("source", url), _source_text, client, url, record_source_failure)
            return await shared_call(("source", url), _source_text, client, url)

    def progress(stage):
        report["academic_progress"] = stage
        report["academic_updated_at"] = time.time()
        report["sources"] = list(source_map.values())
        report["online_pages_analyzed"] = len(source_map)
        report["online_matches"] = [{
            "passage": item["text"], "matched_text": item["text"],
            "match_type": "exact" if any(match["match_type"] == "exact" for match in match_map.get(item.get("index"), [])) else "approximate",
            "sources": match_map.get(item.get("index"), []),
        } for item in passages if match_map.get(item.get("index"))]
        if progress_callback:
            progress_callback()
    progress("Starting academic source searches")
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        for passage in passages[:20]:
            if time.monotonic() >= deadline:
                report["academic_passages_skipped"] = len(passages) - report["academic_passages_searched"]
                report.setdefault("errors", []).append("Academic search reached its time limit; saved matches are partial.")
                break
            query = " ".join(_tokens(passage["text"])[:18])
            if len(query.split()) < 6:
                continue
            report["academic_passages_searched"] += 1
            report["searches_attempted"] += len(providers)
            papers = []
            search_started = time.perf_counter()
            async def search_provider(provider):
                return await shared_call(("search", provider, query.casefold()), _search,
                                         client, provider, query)
            provider_results = await asyncio.gather(*(search_provider(provider) for provider in providers),
                                                    return_exceptions=True)
            for provider, found in zip(providers, provider_results):
                if isinstance(found, Exception):
                    report["searches_failed"] += 1
                    logger.exception("Academic source search failed scan_id=%s provider=%s passage_index=%s",
                                     scan_id, provider, passage.get("index"),
                                     exc_info=(type(found), found, found.__traceback__))
                else:
                    report["searches_completed"] += 1
                    for paper in found:
                        if not isinstance(paper, dict):
                            logger.warning("Academic provider returned non-object candidate scan_id=%s provider=%s", scan_id, provider)
                            continue
                        paper["provider"] = provider
                        papers.append(paper)
                progress(f"Searched {report['academic_passages_searched']} passages; queried {provider}")
            logger.info("Academic provider search timing scan_id=%s passage_index=%s providers=%d elapsed_ms=%.1f",
                        scan_id, passage.get("index"), len(providers),
                        (time.perf_counter() - search_started) * 1000)
            report["academic_candidates_discovered"] += len(papers)
            logger.info("Academic discovery summary scan_id=%s passage_index=%s candidates=%d",
                        scan_id, passage.get("index"), len(papers))
            if time.monotonic() >= deadline:
                report.setdefault("errors", []).append("Academic search reached its time limit; saved matches are partial.")
                break
            merged_candidates = {}
            for paper in papers:
                title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip()
                doi = str(paper.get("doi") or "").strip()
                key = doi.casefold() if doi else title.casefold()
                if not key:
                    continue
                locations = paper.get("locations") or []
                if paper.get("url") and paper["url"] not in locations:
                    locations = [*locations, paper["url"]]
                existing = merged_candidates.get(key)
                if existing:
                    existing["locations"] = list(dict.fromkeys(
                        [*existing.get("locations", []), *locations]
                    ))
                    if not existing.get("title") and title:
                        existing["title"] = title
                    continue
                merged_candidates[key] = {**paper, "title": title, "doi": doi,
                                          "locations": list(dict.fromkeys(locations))}
            papers = list(merged_candidates.values())
            passage_papers = 0
            for paper in papers:
                if passage_papers >= _MAX_FULLTEXT_PAPERS_PER_PASSAGE or time.monotonic() >= deadline:
                    break
                title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip()
                doi = str(paper.get("doi") or "").strip()
                key = (doi.casefold() if doi else title.casefold())
                if not title or not key:
                    continue
                passage_papers += 1
                # Only inspect accessible OA locations, never metadata abstracts.
                locations = paper.get("locations") or ([paper.get("url")] if paper.get("url") else [])
                text = ""
                matched_source_url = ""
                candidate_urls = list(dict.fromkeys(
                    url for url in locations[:_MAX_SOURCE_URLS_PER_PAPER]
                    if url and urlsplit(url).scheme in {"http", "https"}
                ))
                report["source_fetches_attempted"] += len(candidate_urls)
                fetch_started = time.perf_counter()
                fetched = await asyncio.gather(*(source_text(url) for url in candidate_urls),
                                               return_exceptions=True)
                logger.info("Academic source retrieval timing scan_id=%s title=%r urls=%d elapsed_ms=%.1f",
                            scan_id, title, len(candidate_urls), (time.perf_counter() - fetch_started) * 1000)
                for url, result in zip(candidate_urls, fetched):
                    if isinstance(result, Exception):
                        with source_failure_lock:
                            report["source_fetches_failed"] += 1
                        logger.warning("Academic source retrieval failed scan_id=%s url=%s error=%s",
                                       scan_id, url, type(result).__name__)
                        continue
                    source_word_count = len(_tokens(result))
                    if source_word_count:
                        report["source_texts_retrieved"] += 1
                    logger.info("Academic source comparison input scan_id=%s provider=%s title=%r url=%s chars=%d words=%d",
                                scan_id, paper.get("provider"), title, url, len(result or ""), source_word_count)
                    progress(f"Checked full text for {report['source_fetches_attempted']} source pages")
                    if not text and source_word_count >= max(_MIN_SOURCE_WORDS, len(_tokens(passage["text"]))):
                        text, matched_source_url = result, url
                if not text:
                    logger.info("Academic candidate skipped scan_id=%s provider=%s title=%r reason=no_accessible_source_text",
                                scan_id, paper.get("provider"), title)
                    continue
                comparison_started = time.perf_counter()
                evidence, similarity, exact = _best_evidence(passage["text"], text)
                passage_tokens = set(_tokens(passage["text"]))
                evidence_tokens = set(_tokens(evidence))
                shared_tokens = len(passage_tokens & evidence_tokens)
                passage_coverage = shared_tokens / max(1, len(passage_tokens))
                candidate = exact or (
                    similarity >= _MIN_APPROXIMATE_JACCARD
                    and shared_tokens >= _MIN_MATCHING_TOKENS
                    and passage_coverage >= _MIN_PASSAGE_TOKEN_COVERAGE
                )
                logger.info("Academic candidate comparison scan_id=%s provider=%s title=%r url=%s exact=%s jaccard=%.3f shared_tokens=%d passage_coverage=%.3f evidence_chars=%d candidate=%s elapsed_ms=%.1f",
                            scan_id, paper.get("provider"), title, matched_source_url, exact,
                            similarity, shared_tokens, passage_coverage, len(evidence), candidate,
                            (time.perf_counter() - comparison_started) * 1000)
                if not candidate:
                    continue
                report["candidate_matches"] += 1
                percent = 100.0 if exact else round(similarity * 100, 1)
                # Link to the page whose accessible body text was actually compared.
                url = matched_source_url
                if not url:
                    url = "https://doi.org/" + doi if doi else str(paper.get("url") or "")
                if not url:
                    continue
                source = source_map.setdefault(key, {"title": title, "url": url, "doi": doi or None,
                    "authors": [x.strip() for x in paper.get("authors", []) if x.strip()],
                    "provider": paper["provider"], "verification": "accessible_full_text",
                    "evidence_limit": "Compared with accessible source body text; verify the linked paper.", "matches": []})
                match = {"passage": passage["text"], "matched_text": passage["text"],
                    "match_type": "exact" if exact else "approximate", "source_type": "academic",
                    "provider": paper["provider"], "verification": "accessible_full_text",
                    "source_title": title, "source_url": url, "doi": doi or None,
                    "authors": source["authors"], "evidence": evidence[:900],
                    "similarity": round(percent / 100, 3), "similarity_percent": percent,
                    "passage_index": passage.get("index"), "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset")}
                source["matches"].append(match)
                match_map.setdefault(passage.get("index"), []).append({**match, "title": title, "url": url})
                covered.add(passage.get("index"))
                progress(f"Compared source text for {title[:100]}")
    report["sources"] = list(source_map.values())
    report["online_pages_analyzed"] = len(source_map)
    report["online_matches"] = [{"passage": p["text"], "matched_text": p["text"],
        "match_type": "exact" if any(x["match_type"] == "exact" for x in match_map.get(p.get("index"), [])) else "approximate",
        "sources": match_map.get(p.get("index"), [])} for p in passages if match_map.get(p.get("index"))]
    report["final_matches"] = sum(len(rows) for rows in match_map.values())
    denom = len(passages)
    report["academic_coverage_percent"] = round(100 * len(covered) / denom, 1) if denom else 0
    report["academic_passages_skipped"] = max(
        report.get("academic_passages_skipped", 0),
        len(passages) - report["academic_passages_searched"],
    )
    if report["source_fetches_failed"]:
        report.setdefault("errors", []).append(
            f"{report['source_fetches_failed']} academic full-text downloads failed; results may be incomplete."
        )
    report["online_search_status"] = "completed" if report["searches_failed"] == 0 and report["source_fetches_failed"] == 0 and not report.get("errors") else "completed_with_errors"
    if report["searches_failed"]:
        report.setdefault("errors", []).append(f"{report['searches_failed']} academic provider searches failed; coverage is partial.")
    if not report["sources"]:
        report["online_search_status"] = "completed" if not report["searches_failed"] and not report.get("errors") else "completed_with_errors"
    logger.info("Academic scan finished scan_id=%s status=%s providers_attempted=%d providers_completed=%d provider_failures=%d candidates=%d source_fetches=%d source_fetch_failures=%d source_texts=%d candidate_matches=%d final_matches=%d skipped_passages=%d elapsed_ms=%.1f",
                scan_id, report["online_search_status"], report["searches_attempted"],
                report["searches_completed"], report["searches_failed"],
                report["academic_candidates_discovered"], report["source_fetches_attempted"],
                report["source_fetches_failed"], report["source_texts_retrieved"],
                report["candidate_matches"], report["final_matches"], report["academic_passages_skipped"],
                (time.perf_counter() - scan_started) * 1000)
    progress("Academic search finished")
    exact = {i: [x for x in rows if x["match_type"] == "exact"] for i, rows in match_map.items()}
    approximate = {i: [x for x in rows if x["match_type"] != "exact"] for i, rows in match_map.items()}
    return ({i: rows for i, rows in exact.items() if rows},
            {i: rows for i, rows in approximate.items() if rows})
