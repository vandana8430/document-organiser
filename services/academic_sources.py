"""Free academic discovery and source-text comparison for plagiarism reports."""
import os
import re
import tempfile
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import httpx

from services.document_processor import extract

_CACHE = OrderedDict()
_CACHE_TTL = 60 * 60 * 24
_CACHE_LIMIT = 512
_TIMEOUT = httpx.Timeout(12.0, connect=5.0)
_HEADERS = {"User-Agent": "SmartFiles/1.0 (academic similarity checker)"}
_LAST_REQUEST = {}


def _cached(key, loader):
    now = time.time()
    item = _CACHE.get(key)
    if item and now - item[0] < _CACHE_TTL:
        _CACHE.move_to_end(key)
        return item[1]
    value = loader()
    _CACHE[key] = (now, value)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return value


def _get(client, provider, url, params=None):
    # Conservative per-process spacing; provider endpoints are all public/free.
    gap = {"crossref": 0.12, "openalex": 0.12, "semantic_scholar": 1.0}.get(provider, 0.2)
    delay = gap - (time.monotonic() - _LAST_REQUEST.get(provider, 0))
    if delay > 0:
        time.sleep(delay)
    response = client.get(url, params=params, headers=_HEADERS)
    _LAST_REQUEST[provider] = time.monotonic()
    if response.status_code == 429:
        try:
            wait = min(10.0, max(1.0, float(response.headers.get("Retry-After", "2"))))
        except (TypeError, ValueError):
            wait = 2.0
        time.sleep(wait)
        response = client.get(url, params=params, headers=_HEADERS)
        _LAST_REQUEST[provider] = time.monotonic()
    response.raise_for_status()
    return response.json()


def _search(client, provider, query):
    key = (provider, query.casefold())
    def load():
        if provider == "crossref":
            data = _get(client, provider, "https://api.crossref.org/works", {
                "query.bibliographic": query, "rows": 5, "select": "DOI,title,author,published,URL",
            }).get("message", {}).get("items", [])
            return [{"doi": x.get("DOI"), "title": (x.get("title") or [""])[0],
                     "authors": [a.get("given", "") + " " + a.get("family", "") for a in x.get("author", [])],
                     "url": x.get("URL", ""), "locations": []} for x in data]
        if provider == "openalex":
            data = _get(client, provider, "https://api.openalex.org/works", {"search": query, "per-page": 5}).get("results", [])
            result = []
            for x in data:
                locs = list(dict.fromkeys(
                    url for location in x.get("locations", [])
                    for url in (location.get("pdf_url"), location.get("landing_page_url")) if url
                ))
                primary = x.get("primary_location") or {}
                best = primary.get("landing_page_url") or primary.get("pdf_url") or ""
                result.append({"doi": (x.get("doi") or "").removeprefix("https://doi.org/"),
                               "title": x.get("title", ""), "authors": [a.get("author", {}).get("display_name", "") for a in x.get("authorships", [])],
                               "url": best or x.get("id", ""), "locations": list(dict.fromkeys(locs))})
            return result
        data = _get(client, provider, "https://api.semanticscholar.org/graph/v1/paper/search", {
            "query": query, "limit": 5, "fields": "title,authors,externalIds,openAccessPdf,url",
        }).get("data", [])
        return [{"doi": (x.get("externalIds") or {}).get("DOI"), "title": x.get("title", ""),
                 "authors": [a.get("name", "") for a in x.get("authors", [])],
                 "url": (x.get("openAccessPdf") or {}).get("url") or x.get("url", ""),
                 "locations": [(x.get("openAccessPdf") or {}).get("url", "")] if (x.get("openAccessPdf") or {}).get("url") else []} for x in data]
    return _cached(key, load)


def _source_text(client, url):
    def load():
        response = client.get(url, headers=_HEADERS, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if len(data) > 20 * 1024 * 1024:
            return ""
        ctype = response.headers.get("content-type", "").casefold()
        if "pdf" not in ctype and not data.startswith(b"%PDF"):
            # Only accept actual text content; metadata/abstracts are not comparison evidence.
            if ctype.split(";", 1)[0].strip() in {"text/plain", "text/markdown"}:
                return data.decode(response.encoding or "utf-8", errors="replace")[:2_000_000]
            return ""
        suffix = ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            path = f.name
        try:
            text, _ = extract(path, "document", ".pdf")
            return (text or "")[:2_000_000]
        finally:
            try: os.unlink(path)
            except OSError: pass
    try:
        return _cached(("source", url), load)
    except Exception:
        return ""


def _tokens(text):
    return re.findall(r"[\w'-]+", text.casefold(), flags=re.UNICODE)


def compare(passages, report):
    """Discover papers and retain a match only when source body text supports it."""
    providers = ("crossref", "openalex", "semantic_scholar")
    report.update(online_search_provider="crossref+openalex+semantic_scholar",
                  online_search_mode="academic_fulltext_v1", online_search_status="searching",
                  online_matches=[], sources=[], searches_attempted=0, searches_completed=0,
                  searches_failed=0, online_pages_analyzed=0)
    source_map, match_map = {}, {}
    covered = set()
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        for passage in passages[:20]:
            query = " ".join(_tokens(passage["text"])[:18])
            if len(query.split()) < 6:
                continue
            report["searches_attempted"] += len(providers)
            papers = []
            seen_papers = set()
            for provider in providers:
                try:
                    found = _search(client, provider, query)
                    report["searches_completed"] += 1
                    for paper in found:
                        paper["provider"] = provider
                        papers.append(paper)
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    report["searches_failed"] += 1
            for paper in papers:
                title = re.sub(r"\s+", " ", str(paper.get("title") or "")).strip()
                doi = str(paper.get("doi") or "").strip()
                key = (doi.casefold() if doi else title.casefold())
                if not title or not key:
                    continue
                if key in seen_papers:
                    continue
                seen_papers.add(key)
                # Only inspect accessible OA locations, never metadata abstracts.
                locations = paper.get("locations") or ([paper.get("url")] if paper.get("url") else [])
                text = ""
                for url in locations[:3]:
                    if url and urlsplit(url).scheme in {"http", "https"}:
                        text = _source_text(client, url)
                        if len(_tokens(text)) >= 80:
                            break
                        text = ""
                if not text:
                    continue
                passage_tokens = _tokens(passage["text"])
                normalized_passage = " ".join(passage_tokens)
                normalized_source = " ".join(_tokens(text))
                exact = len(normalized_passage) >= 60 and normalized_passage in normalized_source
                pset = set(passage_tokens)
                sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|[\r\n]+", text) if x.strip()]
                evidence = max(sentences, key=lambda line: len(pset & set(_tokens(line))) / max(1, len(pset | set(_tokens(line)))), default="")
                etokens = set(_tokens(evidence))
                similarity = len(pset & etokens) / max(1, len(pset | etokens))
                if not exact and similarity < 0.72:
                    continue
                percent = 100.0 if exact else round(similarity * 100, 1)
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
    report["sources"] = list(source_map.values())
    report["online_pages_analyzed"] = len(source_map)
    report["online_matches"] = [{"passage": p["text"], "matched_text": p["text"],
        "match_type": "exact" if any(x["match_type"] == "exact" for x in match_map.get(p.get("index"), [])) else "approximate",
        "sources": match_map.get(p.get("index"), [])} for p in passages if match_map.get(p.get("index"))]
    denom = len(passages)
    report["academic_coverage_percent"] = round(100 * len(covered) / denom, 1) if denom else 0
    report["academic_passages_searched"] = min(len(passages), 20)
    report["academic_passages_skipped"] = max(0, len(passages) - 20)
    report["online_search_status"] = "completed" if report["searches_failed"] == 0 else "completed_with_errors"
    if report["searches_failed"]:
        report.setdefault("errors", []).append(f"{report['searches_failed']} academic provider searches failed; coverage is partial.")
    if not report["sources"]:
        report["online_search_status"] = "completed" if not report["searches_failed"] else "completed_with_errors"
    exact = {i: [x for x in rows if x["match_type"] == "exact"] for i, rows in match_map.items()}
    approximate = {i: [x for x in rows if x["match_type"] != "exact"] for i, rows in match_map.items()}
    return ({i: rows for i, rows in exact.items() if rows},
            {i: rows for i, rows in approximate.items() if rows})
