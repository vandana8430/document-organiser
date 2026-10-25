"""Owner-scoped local similarity checks and optional Quetext plagiarism reports."""
import json
import logging
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from database.repository import q, stored_file_path, transaction
from services.document_processor import extract
from services.academic_sources import compare as _academic_comparison

QUETEXT_API_BASE = "https://www.quetext.com/api/v2"
QUETEXT_MIN_WORDS = 20
QUETEXT_MAX_WORDS = 50_000
QUETEXT_MAX_PROCESSING_SECONDS = max(120, int(os.getenv("SMARTFILES_QUETEXT_MAX_PROCESSING_SECONDS", "900")))
QUETEXT_POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("SMARTFILES_QUETEXT_POLL_INTERVAL_SECONDS", "2.0")))
QUETEXT_MAX_GET_RETRIES = max(0, int(os.getenv("SMARTFILES_QUETEXT_MAX_GET_RETRIES", "4")))
MAX_WEB_SEARCHES = max(0, int(os.getenv("SMARTFILES_MAX_WEB_SEARCHES", "4")))
MAX_ANALYZED_PASSAGES = max(0, int(os.getenv("SMARTFILES_MAX_ANALYZED_PASSAGES", "300")))
MAX_LOCAL_SENTENCES = max(0, int(os.getenv("SMARTFILES_MAX_LOCAL_SENTENCES", "1000")))
MIN_PASSAGE_WORDS = max(1, int(os.getenv("SMARTFILES_MIN_PASSAGE_WORDS", "8")))
QUETEXT_CONNECT_TIMEOUT_SECONDS = float(os.getenv("SMARTFILES_QUETEXT_CONNECT_TIMEOUT_SECONDS", "15.0"))
QUETEXT_READ_TIMEOUT_SECONDS = float(os.getenv("SMARTFILES_QUETEXT_READ_TIMEOUT_SECONDS", "90.0"))
QUETEXT_WRITE_TIMEOUT_SECONDS = float(os.getenv("SMARTFILES_QUETEXT_WRITE_TIMEOUT_SECONDS", "60.0"))
SCAN_STALE_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


def _quetext_api_key():
    value = os.getenv("QUETEXT_API_KEY", "").strip()
    if not value:
        return ""
    placeholders = {
        "your_api_key_here",
        "your_api_key",
        "replace_me",
        "replace_with_your_quetext_api_key",
    }
    return "" if value.casefold() in {item.casefold() for item in placeholders} else value


def _quetext_request(method, path, api_key, json_body=None):
    timeout = httpx.Timeout(
        connect=QUETEXT_CONNECT_TIMEOUT_SECONDS,
        read=QUETEXT_READ_TIMEOUT_SECONDS,
        write=QUETEXT_WRITE_TIMEOUT_SECONDS,
        pool=QUETEXT_CONNECT_TIMEOUT_SECONDS,
    )
    with httpx.Client(timeout=timeout) as client:
        return client.request(
            method,
            f"{QUETEXT_API_BASE}/{path.lstrip('/')}",
            headers={"X-API-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"},
            json=json_body,
        )


def _unique_errors(report):
    """Keep report errors readable when the same provider failure affects many passages."""
    report["errors"] = list(dict.fromkeys(
        message.strip() for message in report.get("errors", [])
        if isinstance(message, str) and message.strip()
    ))


def _now():
    return time.time()


def _timestamp(value=None):
    return datetime.fromtimestamp(value or _now(), timezone.utc).isoformat(timespec="seconds")


def _normalize(text):
    """Case-fold and normalize Unicode/whitespace without removing punctuation."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _exact_span(passage, source):
    normalized = _normalize(passage)
    if not normalized:
        return None
    return re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", _normalize(source), flags=re.UNICODE)


def _sentences(text):
    cleaned = "".join(
        char for char in unicodedata.normalize("NFC", text)
        if char in "\n\r\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", cleaned)
        if sentence.strip()
    ]


def _sentences_with_offsets(text):
    """Return readable sentences with their character ranges in the input text."""
    cleaned = "".join(
        char for char in unicodedata.normalize("NFC", text)
        if char in "\n\r\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    passages = []
    cursor = 0
    for sentence in _sentences(cleaned):
        start = cleaned.find(sentence, cursor)
        if start < 0:
            continue
        end = start + len(sentence)
        passages.append({"text": sentence, "start_offset": start, "end_offset": end})
        cursor = end
    return passages


def _words(text):
    return re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)


def _candidate_passages(sentences):
    eligible = [
        {
            "index": index,
            "text": sentence["text"] if isinstance(sentence, dict) else sentence,
            "words": len(_words(sentence["text"] if isinstance(sentence, dict) else sentence)),
            **(
                {
                    "start_offset": sentence["start_offset"],
                    "end_offset": sentence["end_offset"],
                }
                if isinstance(sentence, dict) else {}
            ),
        }
        for index, sentence in enumerate(sentences)
        if len(_words(sentence["text"] if isinstance(sentence, dict) else sentence)) >= 5
    ]
    total = len(eligible)
    if MAX_ANALYZED_PASSAGES <= 0 or total <= MAX_ANALYZED_PASSAGES:
        return eligible, total
    stride = max(1, total // MAX_ANALYZED_PASSAGES)
    sampled = [eligible[min(len(eligible) - 1, index * stride)] for index in range(MAX_ANALYZED_PASSAGES)]
    return sampled, total


def _analysis_limit_details(total_eligible):
    if total_eligible <= MAX_ANALYZED_PASSAGES:
        return 0, False
    return total_eligible - MAX_ANALYZED_PASSAGES, True


def _distinctive_passages(passages):
    frequency = defaultdict(int)
    for position, passage in enumerate(passages):
        passage["scan_index"] = position
        for word in set(_words(passage["text"].casefold())):
            frequency[word] += 1
    ranked = []
    seen = set()
    for passage in passages:
        words = _words(passage["text"])
        text = passage["text"].strip()
        normalized = _normalize(text)
        if len(words) < MIN_PASSAGE_WORDS or len(text) > 240 or normalized in seen:
            continue
        seen.add(normalized)
        score = sum(1 / frequency[word.casefold()] for word in set(words))
        ranked.append((score, passage))
    ranked.sort(key=lambda item: (-item[0], item[1]["index"]))
    limit = max(0, MAX_WEB_SEARCHES)
    return [passage for _, passage in ranked[:limit]]


def _jaccard(left, right):
    left_words = set(_words(_normalize(left)))
    right_words = set(_words(_normalize(right)))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def _safe_url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        path = parts.path.rstrip("/") or "/"
        query = urlencode([
            (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}
        ])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))
    except (TypeError, ValueError):
        return ""


def _tavily_fallback(passages, report):
    """Search with Tavily when the paid checker is unavailable; only retain verified page text."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return False
    queries = _distinctive_passages(passages)
    report["online_search_provider"] = "tavily"
    report["online_search_mode"] = "verified_web_search_v1"
    report["online_search_status"] = "searching"
    sources, online_matches = {}, []
    exact_indexes, approximate_indexes = set(), set()
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            for passage in queries:
                report["searches_attempted"] += 1
                response = client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"query": '"' + passage["text"] + '"',
                          "search_depth": "basic", "max_results": 5,
                          "include_raw_content": True},
                )
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results", []) if isinstance(payload, dict) else []
                report["searches_completed"] += 1
                for item in results if isinstance(results, list) else []:
                    if not isinstance(item, dict):
                        continue
                    url = _safe_url(item.get("url"))
                    raw = item.get("raw_content") or ""
                    if not url or not isinstance(raw, str) or not raw.strip():
                        continue
                    # A search hit alone is not evidence. Require substantial lexical overlap
                    # between the document passage and provider-returned page text.
                    sentences = _sentences(raw)
                    best = max(sentences, key=lambda sentence: _jaccard(passage["text"], sentence), default="")
                    similarity = 1.0 if _exact_span(passage["text"], raw) else _jaccard(passage["text"], best)
                    if similarity < 0.78:
                        continue
                    title = re.sub(r"\s+", " ", str(item.get("title") or urlsplit(url).hostname)).strip()[:300]
                    source = sources.setdefault(url, {
                        "title": title, "url": url, "domain": urlsplit(url).hostname or "",
                        "provider": "tavily", "verification": "provider_text_match",
                        "evidence_limit": "Match verified against text returned by Tavily; review the linked page.",
                        "evidence": best[:1000], "matches": [],
                    })
                    match = {"passage": passage["text"], "matched_text": passage["text"],
                             "match_type": "exact" if similarity == 1.0 else "approximate",
                             "source_type": "online", "source_title": title, "source_url": url,
                             "evidence": best[:1000], "source_text": best[:1000],
                             "similarity": round(similarity, 3),
                             "similarity_percent": round(similarity * 100, 1),
                             "passage_index": passage["index"], "start_offset": passage.get("start_offset"),
                             "end_offset": passage.get("end_offset")}
                    source["matches"].append(match)
                    online_matches.append({"passage": passage["text"], "matched_text": passage["text"],
                                           "match_type": match["match_type"],
                                           "sources": [{**match, "provider": "tavily",
                                                        "verification": "provider_text_match", "title": title,
                                                        "url": url}]})
                    (exact_indexes if similarity == 1.0 else approximate_indexes).add(passage["index"])
    except (httpx.HTTPError, ValueError, TypeError) as error:
        report["online_search_status"] = "provider_error"
        report["errors"].append(f"Tavily fallback failed ({type(error).__name__}). No unverified matches were reported.")
        return True
    report["sources"] = list(sources.values())
    report["online_matches"] = online_matches
    report["online_pages_analyzed"] = len(sources)
    report["online_search_status"] = "completed" if queries else "no_suitable_passages"
    report["online_exact_match_count"] = len(exact_indexes)
    report["online_approximate_match_count"] = len(approximate_indexes - exact_indexes)
    total_words = sum(item["words"] for item in passages)
    exact_words = sum(item["words"] for item in passages if item["index"] in exact_indexes)
    approximate_words = sum(item["words"] for item in passages if item["index"] in approximate_indexes - exact_indexes)
    report["online_exact_similarity_percent"] = round(100 * exact_words / total_words, 1) if total_words else None
    report["online_approximate_similarity_percent"] = round(100 * approximate_words / total_words, 1) if total_words else None
    report["quetext_score_percent"] = None
    return True


def _base_report(document_name, online_requested, is_demo=False, online_provider="quetext"):
    provider_name = "crossref+openalex+semantic_scholar" if online_provider == "academic" else "quetext"
    provider_mode = "academic_fulltext_v1" if online_provider == "academic" else "quetext_report_v1"
    return {
        "document_name": document_name,
        "sample_data": bool(is_demo),
        "scan_date": _timestamp(),
        "status": "queued",
        "online_search_requested": bool(online_requested),
        "online_search_provider": provider_name if online_requested else None,
        "online_search_mode": provider_mode if online_requested else None,
        "online_search_status": "pending" if online_requested else "not_requested",
        "quetext_score_percent": None,
        "quetext_progress_percent": None,
        "online_exact_similarity_percent": None,
        "online_approximate_similarity_percent": None,
        "online_exact_match_count": None,
        "online_approximate_match_count": None,
        "online_pages_analyzed": None,
        "online_source_words_analyzed": None,
        "estimated_text_similarity_percent": 0,
        "approximate_text_similarity_percent": 0,
        "unmatched_text_percent": None,
        "eligible_words_analyzed": 0,
        "possible_matching_sources": 0,
        "exact_match_count": 0,
        "approximate_match_count": 0,
        "matches": [],
        "sources": [],
        "online_matches": [],
        "local_matches": [],
        "errors": [],
        "searches_attempted": 0,
        "searches_completed": 0,
        "searches_failed": 0,
        "searches_skipped": 0,
        "skipped_passages": 0,
        "skipped_online_passages": 0,
        "method": (
            "Eligible passages contain at least five words. Exact-word coverage is the number of "
            "words in passages with an exact match divided by all words in the analyzed passages. "
            "Approximate-only coverage uses the same denominator and counts words in passages with "
            "an approximate match but no exact match; it is reported separately and is not added to "
            "exact-word coverage. Approximate matches require word-set Jaccard overlap of at least "
            "0.78. Exact matching uses Unicode NFKC, case-folding, and whitespace normalization. "
            f"Configured passage limits are {MAX_ANALYZED_PASSAGES} analyzed passages and "
            f"{MAX_WEB_SEARCHES} distinctive online-search passages per scan. Local comparison includes "
            "all other owned documents with extracted text, but is limited to the first 1,000 source "
            "sentences per document; reports disclose when a document was truncated. Online search, "
            "when explicitly enabled, searches only the configured distinctive subset and reports the "
            "actual coverage and reasons for skipped passages. Academic online discovery queries Crossref, "
            "OpenAlex, and Semantic Scholar. Academic matches require accessible source body text; titles "
            "and abstracts alone are never treated as evidence."
        ),
        "disclaimer": "Text similarity does not by itself prove plagiarism. Review the matching passages and sources manually.",
    }


def _store(scan_id, uid, status, report):
    report["status"] = status
    with transaction() as connection:
        connection.execute(
            "UPDATE plagiarism_scans SET status=?,updated=?,report_json=? WHERE id=? AND uid=?",
            (status, _now(), json.dumps(report, ensure_ascii=False), scan_id, uid),
        )


def _empty_report(scan_id, uid, name, online_requested, error, is_demo=False, online_provider="quetext"):
    report = _base_report(name, online_requested, is_demo, online_provider)
    report["scan_id"] = scan_id
    report["online_search_status"] = "not_run" if online_requested else "not_requested"
    report["errors"].append(error)
    logger.warning("Plagiarism scan ended before comparison scan_id=%s document=%r provider=%s error=%s",
                   scan_id, name, report.get("online_search_provider"), error)
    _store(scan_id, uid, "completed_with_warnings", report)


def create_scan(uid, file_id, online_requested, online_provider="quetext"):
    """Validate ownership/format and persist a queued report without touching files."""
    if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id <= 0:
        raise HTTPException(422, "Choose a valid document to scan.")
    now = _now()
    with transaction() as connection:
        rows = connection.execute(
            "SELECT id,name,demo_key,sha FROM files WHERE id=? AND uid=?",
            (file_id, uid),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "Document not found.")
        name = rows[0]["name"]
        extension = os.path.splitext(name)[1].lower()
        if extension not in {".pdf", ".docx", ".txt"}:
            raise HTTPException(415, "Plagiarism checking supports PDF, DOCX, and TXT documents.")

        if online_requested:
            recent = connection.execute(
            "SELECT s.id,s.status,s.created,s.updated,s.report_json FROM plagiarism_scans s "
                "JOIN files f ON f.id=s.fid "
                "WHERE s.uid=? AND f.sha=? AND s.online_requested=1 AND s.created>=? "
                "ORDER BY s.created DESC LIMIT 50",
                (uid, rows[0]["sha"], now - 30 * 24 * 60 * 60),
            ).fetchall()
            if recent:
                # A dead background worker must not make identical documents reuse
                # a permanently active scan. Keep its saved result, but close it.
                for active in recent:
                    if active["status"] in {"queued", "scanning", "searching"} and now - active["updated"] > SCAN_STALE_SECONDS:
                        stale_report = json.loads(active["report_json"])
                        stale_report.setdefault("errors", []).append(
                            "The background scan stopped updating and was marked failed. Start a new scan to retry."
                        )
                        stale_report["status"] = "failed"
                        connection.execute(
                            "UPDATE plagiarism_scans SET status='failed',updated=?,report_json=? WHERE id=? AND uid=?",
                            (now, json.dumps(stale_report, ensure_ascii=False), active["id"], uid),
                        )
                for active in recent:
                    active_report = json.loads(active["report_json"])
                    if (
                        active_report.get("online_search_provider") == ("crossref+openalex+semantic_scholar" if online_provider == "academic" else "quetext")
                        and active_report.get("online_search_mode") == ("academic_fulltext_v1" if online_provider == "academic" else "quetext_report_v1")
                        and active["status"] in {"queued", "scanning", "searching"}
                        and now - active["updated"] <= SCAN_STALE_SECONDS
                    ):
                        return {"scan_id": active["id"], "status": active["status"], "reused": True}
                # Reuse the real provider report for identical document bytes. This
                # avoids another paid Quetext submission and preserves its source URLs.
                for cached in recent:
                    cached_report = json.loads(cached["report_json"])
                    if (
                        cached_report.get("online_search_provider") == ("crossref+openalex+semantic_scholar" if online_provider == "academic" else "quetext")
                        and cached_report.get("online_search_mode") == ("academic_fulltext_v1" if online_provider == "academic" else "quetext_report_v1")
                        and cached_report.get("online_search_status") == "completed"
                        and cached["status"] in {"completed", "completed_with_warnings"}
                    ):
                        return {"scan_id": cached["id"], "status": cached["status"], "reused": True}
                recent_report = json.loads(recent[0]["report_json"])
                is_quetext_report = (
                    recent_report.get("online_search_provider") == ("crossref+openalex+semantic_scholar" if online_provider == "academic" else "quetext")
                    and recent_report.get("online_search_mode") == ("academic_fulltext_v1" if online_provider == "academic" else "quetext_report_v1")
                )
                if (is_quetext_report and recent[0]["status"] in {"queued", "scanning", "searching"}
                        and now - recent[0]["updated"] <= SCAN_STALE_SECONDS):
                    return {"scan_id": recent[0]["id"], "status": recent[0]["status"], "reused": True}
                if is_quetext_report and recent_report.get("quetext_submission_uncertain"):
                    return {"scan_id": recent[0]["id"], "status": recent[0]["status"], "reused": True}
                resumable_status = recent_report.get("online_search_status") in {
                    "provider_timeout", "provider_error", "rate_limited"
                }
                resume_report_id = recent_report.get("quetext_report_id")
                if is_quetext_report and resumable_status and resume_report_id:
                    recent_report["errors"] = [
                        error for error in recent_report.get("errors", [])
                        if not (isinstance(error, str) and error.startswith("Quetext "))
                    ]
                    recent_report["online_search_status"] = "searching"
                    recent_report["status"] = "searching"
                    connection.execute(
                        "UPDATE plagiarism_scans SET status='searching',updated=?,report_json=? WHERE id=? AND uid=?",
                        (now, json.dumps(recent_report, ensure_ascii=False), recent[0]["id"], uid),
                    )
                    return {
                        "scan_id": recent[0]["id"], "status": "searching", "reused": False,
                        "resume_report_id": str(resume_report_id),
                    }

        report = _base_report(name, online_requested, bool(rows[0]["demo_key"]), online_provider)
        cursor = connection.execute(
            "INSERT INTO plagiarism_scans(uid,fid,document_name,online_requested,status,created,updated,report_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, file_id, name, int(online_requested), "queued", now, now,
             json.dumps(report, ensure_ascii=False)),
        )
        scan_id = cursor.lastrowid
    return {"scan_id": scan_id, "status": "queued", "reused": False}


def _local_comparison(uid, file_id, passages, report):
    files = q(
        "SELECT id,name,text,demo_key FROM files WHERE uid=? AND id!=? AND kind='document' "
        "AND text IS NOT NULL AND trim(text)!='' ORDER BY created DESC",
        (uid, file_id),
    )
    exact_by_passage = defaultdict(list)
    approx_by_passage = defaultdict(list)
    local_groups = {}

    for file in files:
        if file["id"] == file_id:
            continue
        source_passages = _sentences_with_offsets(file["text"])
        truncated = len(source_passages) > MAX_LOCAL_SENTENCES
        source_passages = source_passages[:MAX_LOCAL_SENTENCES]
        source_sentences = [item["text"] for item in source_passages]
        normalized_full = _normalize("\n".join(source_sentences))
        normalized_sources = [_normalize(sentence) for sentence in source_sentences]
        source_index_by_text = {}
        for source_index, normalized_source in enumerate(normalized_sources):
            source_index_by_text.setdefault(normalized_source, source_index)
        token_sets = [set(_words(_normalize(sentence))) for sentence in source_sentences]
        inverted = defaultdict(set)
        for source_index, words in enumerate(token_sets):
            for word in words:
                inverted[word].add(source_index)

        for passage_index, passage in enumerate(passages):
            normalized = _normalize(passage["text"])
            source_match = source_index_by_text.get(normalized)
            full_match = _exact_span(passage["text"], normalized_full)
            if source_match is not None or full_match is not None:
                evidence = (
                    source_sentences[source_match] if source_match is not None
                    else normalized_full[full_match.start():full_match.end()]
                )
                source_offset = (
                    source_passages[source_match]["start_offset"]
                    if source_match is not None else None
                )
                exact_by_passage[passage_index].append({
                    "type": "local",
                    "source_type": "local",
                    "document_id": file["id"],
                    "document_name": file["name"],
                    "source_title": file["name"],
                    "source_url": None,
                    "evidence": evidence,
                    "source_text": evidence,
                    "similarity": 1.0,
                    "word_jaccard": 1.0,
                    "passage_index": passage["index"],
                    "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset"),
                    "source_sentence_index": source_match,
                    "source_start_offset": source_offset,
                    "source_end_offset": (
                        source_passages[source_match]["end_offset"]
                        if source_match is not None else None
                    ),
                })
                group = local_groups.setdefault(file["id"], {
                    "document_id": file["id"], "document_name": file["name"],
                    "is_demo": bool(file["demo_key"]), "matches": [],
                })
                group["matches"].append({
                    "passage": passage["text"],
                    "matched_text": passage["text"],
                    "match_type": "exact",
                    "source_type": "local",
                    "source_title": file["name"],
                    "source_url": None,
                    "evidence": evidence,
                    "source_text": evidence,
                    "similarity": 1.0,
                    "word_jaccard": 1.0,
                    "passage_index": passage["index"],
                    "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset"),
                    "source_sentence_index": source_match,
                    "source_start_offset": source_offset,
                    "source_end_offset": (
                        source_passages[source_match]["end_offset"]
                        if source_match is not None else None
                    ),
                })
                continue

            target_words = set(_words(_normalize(passage["text"])))
            if len(target_words) < MIN_PASSAGE_WORDS:
                continue
            possible = set()
            for word in target_words:
                possible.update(inverted.get(word, ()))
            best = None
            for source_index in possible:
                source_words = token_sets[source_index]
                union = target_words | source_words
                ratio = len(target_words & source_words) / len(union) if union else 0
                if ratio >= 0.78 and (best is None or ratio > best[0]):
                    best = (ratio, source_index)
            if best is not None:
                evidence = source_sentences[best[1]]
                approx_by_passage[passage_index].append({
                    "type": "local",
                    "source_type": "local",
                    "document_id": file["id"],
                    "document_name": file["name"],
                    "source_title": file["name"],
                    "source_url": None,
                    "evidence": evidence,
                    "source_text": evidence,
                    "similarity": round(best[0], 3),
                    "word_jaccard": round(best[0], 3),
                    "passage_index": passage["index"],
                    "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset"),
                    "source_sentence_index": best[1],
                    "source_start_offset": source_passages[best[1]]["start_offset"],
                    "source_end_offset": source_passages[best[1]]["end_offset"],
                })
                group = local_groups.setdefault(file["id"], {
                    "document_id": file["id"], "document_name": file["name"],
                    "is_demo": bool(file["demo_key"]), "matches": [],
                })
                group["matches"].append({
                    "passage": passage["text"],
                    "matched_text": passage["text"],
                    "match_type": "approximate",
                    "source_type": "local",
                    "source_title": file["name"],
                    "source_url": None,
                    "evidence": evidence,
                    "source_text": evidence,
                    "similarity": round(best[0], 3),
                    "word_jaccard": round(best[0], 3),
                    "passage_index": passage["index"],
                    "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset"),
                    "source_sentence_index": best[1],
                    "source_start_offset": source_passages[best[1]]["start_offset"],
                    "source_end_offset": source_passages[best[1]]["end_offset"],
                })
        if truncated:
            report["errors"].append(f"Local comparison for {file['name']} was limited to the first {MAX_LOCAL_SENTENCES} sentences.")

    report["local_matches"] = list(local_groups.values())
    return exact_by_passage, approx_by_passage


def _quetext_error(response):
    try:
        payload = response.json()
    except (ValueError, AttributeError, TypeError):
        payload = {}
    code = getattr(response, "status_code", 0)
    if isinstance(payload, dict) and isinstance(payload.get("code"), int) and payload["code"] >= 400:
        code = payload["code"]
    message = payload.get("message") if isinstance(payload, dict) else None
    if code == 401:
        return "invalid_api_key", "Quetext rejected QUETEXT_API_KEY. Check that the API key is active and restart SmartFiles."
    if code == 402:
        return "quota_exhausted", "Quetext API wallet has insufficient credits. Add API credits in Quetext Account > API Keys."
    if code == 403:
        return "access_denied", "Quetext denied this API request. Check that the account and API key have API access."
    if code == 429:
        return "rate_limited", "Quetext rate limit was reached. Wait briefly and try again."
    if code >= 500:
        return "provider_error", "Quetext is temporarily unavailable. Please try again later."
    safe_message = re.sub(r"\s+", " ", message).strip()[:300] if isinstance(message, str) else ""
    return "provider_error", f"Quetext could not process this request{': ' + safe_message if safe_message else '.'}"


def _quetext_has_error(response):
    if getattr(response, "status_code", 0) >= 400:
        return True
    try:
        payload = response.json()
    except (ValueError, AttributeError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("status") is False


def _quetext_retryable(response):
    code = getattr(response, "status_code", 0)
    if code < 400:
        try:
            payload = response.json()
        except (ValueError, AttributeError, TypeError):
            return False
        if isinstance(payload, dict) and isinstance(payload.get("code"), int):
            code = payload["code"]
    return code == 429 or code >= 500


def _quetext_request_with_retries(method, path, api_key, json_body=None):
    """Retry safe report reads on transient network/provider failures, never resubmit paid POSTs."""
    for attempt in range(QUETEXT_MAX_GET_RETRIES + 1):
        try:
            response = _quetext_request(method, path, api_key, json_body)
        except (httpx.TimeoutException, httpx.RequestError):
            if method.upper() != "GET" or attempt >= QUETEXT_MAX_GET_RETRIES:
                raise
            time.sleep(min(15, 2 ** attempt))
            continue
        if method.upper() != "GET" or not _quetext_retryable(response) or attempt >= QUETEXT_MAX_GET_RETRIES:
            return response
        retry_after = getattr(response, "headers", {}).get("Retry-After")
        try:
            delay = min(60.0, max(0.0, float(retry_after))) if retry_after is not None else min(15, 2 ** attempt)
        except (TypeError, ValueError):
            delay = min(15, 2 ** attempt)
        time.sleep(delay)
    raise RuntimeError("Quetext request retry loop ended unexpectedly")


def _quetext_data(response):
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") is not True or not isinstance(payload.get("data"), (dict, list)):
        raise ValueError("Quetext returned an invalid response")
    return payload["data"]


def _online_comparison(
    passages, report, document_text=None, progress_callback=None, existing_report_id=None,
):
    exact_by_passage = defaultdict(list)
    approx_by_passage = defaultdict(list)
    report.update(searches_attempted=0, searches_completed=0, online_pages_analyzed=0)
    report["online_search_status"] = "searching"
    api_key = _quetext_api_key()
    if not api_key:
        if _tavily_fallback(passages, report):
            return exact_by_passage, approx_by_passage
        report["online_search_status"] = "unavailable_missing_api_key"
        report["errors"].append("Quetext online checking is not configured. Add QUETEXT_API_KEY to services/key.env or the backend environment, then restart SmartFiles.")
        return exact_by_passage, approx_by_passage

    document_text = document_text if isinstance(document_text, str) else "\n".join(item["text"] for item in passages)
    words = len(_words(document_text))
    if words < QUETEXT_MIN_WORDS:
        report["online_search_status"] = "insufficient_text"
        report["errors"].append(f"Quetext requires at least {QUETEXT_MIN_WORDS} words for a plagiarism report. Local comparison was still completed.")
        return exact_by_passage, approx_by_passage
    if words > QUETEXT_MAX_WORDS:
        report["online_search_status"] = "document_too_large"
        report["errors"].append(f"Quetext supports up to {QUETEXT_MAX_WORDS:,} words per report. No text was sent; local comparison was still completed.")
        return exact_by_passage, approx_by_passage

    if existing_report_id:
        report_id = str(existing_report_id)
        report["quetext_report_id"] = report_id
        report["quetext_resumed"] = True
    else:
        report["searches_attempted"] = 1
        try:
            response = _quetext_request("POST", "report", api_key, {"title": report["document_name"], "text": document_text})
        except (httpx.TimeoutException, httpx.RequestError) as error:
            if _tavily_fallback(passages, report):
                report["errors"].append("Quetext could not be reached; the configured Tavily fallback was used.")
                return exact_by_passage, approx_by_passage
            report["online_search_status"] = "provider_error"
            report["quetext_submission_uncertain"] = True
            report["errors"].append(
                f"Quetext report submission failed or timed out ({type(error).__name__}). "
                "SmartFiles did not resubmit automatically because the provider may have accepted the request. Check Quetext reports before retrying."
            )
            return exact_by_passage, approx_by_passage
        if _quetext_has_error(response):
            report["online_search_status"], message = _quetext_error(response)
            quetext_status = report["online_search_status"]
            if quetext_status in {
                "quota_exhausted", "invalid_api_key", "rate_limited", "provider_error", "access_denied"
            } and _tavily_fallback(passages, report):
                report["errors"].append(f"Quetext was unavailable ({quetext_status}); the configured Tavily fallback was used.")
                return exact_by_passage, approx_by_passage
            report["errors"].append(message + " Local comparison was still completed.")
            return exact_by_passage, approx_by_passage
        try:
            submitted = _quetext_data(response)
            report_id = submitted.get("id")
            if not isinstance(report_id, (str, int)) or not str(report_id).strip():
                raise ValueError("Quetext did not return a report ID")
        except (ValueError, TypeError, AttributeError):
            report["online_search_status"] = "provider_error"
            report["errors"].append("Quetext returned an unreadable report submission. No results were claimed.")
            return exact_by_passage, approx_by_passage
        report_id = str(report_id)
        report["quetext_report_id"] = report_id
        report["quetext_progress_percent"] = 0
        if progress_callback:
            progress_callback()

    deadline = time.monotonic() + QUETEXT_MAX_PROCESSING_SECONDS
    complete = False
    poll_delay = QUETEXT_POLL_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(min(poll_delay, max(0, deadline - time.monotonic())))
        try:
            progress_response = _quetext_request_with_retries("GET", f"report-progress/{report_id}", api_key)
            if _quetext_has_error(progress_response):
                report["online_search_status"], message = _quetext_error(progress_response)
                report["errors"].append(message + " Local comparison was still completed.")
                if progress_callback:
                    progress_callback()
                return exact_by_passage, approx_by_passage
            progress_data = _quetext_data(progress_response)
            progress_items = progress_data if isinstance(progress_data, list) else [progress_data]
            progress_item = next((item for item in progress_items if isinstance(item, dict)), {})
            progress_value = progress_item.get("progress", progress_item.get("Progress"))
            if isinstance(progress_value, str):
                try:
                    progress_value = float(progress_value)
                except ValueError:
                    progress_value = None
            if isinstance(progress_value, (int, float)) and not isinstance(progress_value, bool):
                progress_value = float(progress_value)
                if 1 < progress_value <= 100:
                    progress_value /= 100
                report["quetext_progress_percent"] = round(max(0, min(100, progress_value * 100)), 1)
                complete = progress_value >= 1
            elif str(progress_item.get("status", "")).casefold() == "completed":
                report["quetext_progress_percent"] = 100
                complete = True
            elif progress_item.get("message"):
                report["online_search_status"] = "provider_error"
                report["errors"].append("Quetext could not find the submitted report while checking its progress. No result was claimed.")
                if progress_callback:
                    progress_callback()
                return exact_by_passage, approx_by_passage
            else:
                report["online_search_status"] = "provider_error"
                report["errors"].append("Quetext progress response did not include a progress value. No result was claimed.")
                if progress_callback:
                    progress_callback()
                return exact_by_passage, approx_by_passage
            if progress_callback:
                progress_callback()
            if complete:
                break
            poll_delay = min(QUETEXT_POLL_INTERVAL_SECONDS * 3, poll_delay + 1)
        except (httpx.TimeoutException, httpx.RequestError) as error:
            report["online_search_status"] = "provider_error"
            report["errors"].append(
                f"Quetext progress could not be retrieved after safe retries ({type(error).__name__}). "
                "The report ID is retained so this check can resume without submitting the document again."
            )
            if progress_callback:
                progress_callback()
            return exact_by_passage, approx_by_passage
        except (ValueError, TypeError, AttributeError):
            report["online_search_status"] = "provider_error"
            report["errors"].append("Quetext returned an unreadable progress response. The report ID is retained for recovery.")
            if progress_callback:
                progress_callback()
            return exact_by_passage, approx_by_passage

    if not complete:
        report["online_search_status"] = "provider_timeout"
        report["errors"].append(
            "Quetext has not completed this report within the configured processing window. "
            "Its report ID is retained; retrying this scan will resume the existing report without another paid submission."
        )
        if progress_callback:
            progress_callback()
        return exact_by_passage, approx_by_passage

    try:
        result_response = _quetext_request_with_retries("GET", f"report/{report_id}", api_key)
        if _quetext_has_error(result_response):
            report["online_search_status"], message = _quetext_error(result_response)
            report["errors"].append(message + " Local comparison was still completed.")
            return exact_by_passage, approx_by_passage
        result_data = _quetext_data(result_response)
        provider_state = result_data.get("status")
        if isinstance(provider_state, str) and provider_state.casefold() not in {"completed", "complete"}:
            report["online_search_status"] = "provider_timeout"
            report["errors"].append(
                "Quetext progress reached 100%, but its report is still processing. "
                "The report ID is retained for recovery without resubmitting the document."
            )
            if progress_callback:
                progress_callback()
            return exact_by_passage, approx_by_passage
        matches = result_data.get("matches", [])
        if not isinstance(matches, list):
            raise ValueError("Quetext match results were malformed")
    except (httpx.TimeoutException, httpx.RequestError) as error:
        report["online_search_status"] = "provider_error"
        report["errors"].append(f"Quetext report retrieval failed ({type(error).__name__}). Local comparison was still completed.")
        return exact_by_passage, approx_by_passage
    except (ValueError, TypeError, AttributeError):
        report["online_search_status"] = "provider_error"
        report["errors"].append("Quetext returned an unreadable plagiarism report. Local comparison was still completed.")
        return exact_by_passage, approx_by_passage

    report["searches_completed"] = 1
    report["online_search_status"] = "completed"
    raw_score = result_data.get("score")
    try:
        score = float(raw_score)
        report["quetext_score_percent"] = round(max(0.0, min(100.0, score)), 1)
    except (TypeError, ValueError):
        report["quetext_score_percent"] = None
    grouped = {}
    report_matches = []
    seen_matches = set()
    for result in matches:
        if not isinstance(result, dict):
            continue
        source_data = result.get("source") if isinstance(result.get("source"), dict) else {}
        url = _safe_url(source_data.get("url"))
        matched_text = result.get("input_text_match")
        evidence = result.get("highlighted_snippet")
        if not url or not isinstance(matched_text, str) or not matched_text.strip():
            continue
        matched_text = matched_text.strip()
        evidence = re.sub(r"<[^>]*>", " ", evidence).strip() if isinstance(evidence, str) else ""
        evidence = re.sub(r"\s+", " ", evidence)[:1000]
        raw_score = result.get("percent_similar")
        try:
            score = max(0.0, min(100.0, float(raw_score)))
        except (TypeError, ValueError):
            score = None
        # Coverage stats map only when safe; display remains based on Quetext's actual returned match text.
        candidate_index = next((i for i, passage in enumerate(passages) if _normalize(matched_text) in _normalize(passage["text"])), None)
        if candidate_index is None:
            candidate_index = next((i for i, passage in enumerate(passages) if _normalize(passage["text"]) in _normalize(matched_text)), None)
        passage = passages[candidate_index] if candidate_index is not None else {}
        source_title = source_data.get("title")
        title = re.sub(r"\s+", " ", source_title).strip()[:300] if isinstance(source_title, str) and source_title.strip() else (urlsplit(url).hostname or url)
        source = grouped.setdefault(url, {
            "title": title, "url": url, "domain": urlsplit(url).hostname or "",
            "provider": "quetext", "verification": "provider_match",
            "evidence_limit": "Match and source snippet returned by Quetext; open the source to review it.",
            "evidence": evidence, "matches": [],
        })
        match = {
            "passage": matched_text, "matched_text": matched_text,
            "match_type": "exact" if score is not None and score >= 100 else "approximate",
            "source_type": "online", "source_title": title, "source_url": url,
            "evidence": evidence, "source_text": evidence,
            "similarity": round(score / 100, 3) if score is not None else None,
            "similarity_percent": round(score, 1) if score is not None else None,
            "passage_index": passage.get("index"),
            "start_offset": passage.get("start_offset"), "end_offset": passage.get("end_offset"),
        }
        grouped[url]["matches"].append(match)
        key = (matched_text, url, evidence)
        if key not in seen_matches:
            seen_matches.add(key)
            report_matches.append({
                "passage": matched_text, "matched_text": matched_text,
                "match_type": match["match_type"],
                "sources": [{
                    "type": "online", "source_type": "online", "provider": "quetext",
                    "verification": "provider_match", "title": title, "source_title": title,
                    "url": url, "source_url": url, "evidence": evidence,
                    "source_text": evidence, "matched_text": matched_text,
                    "similarity": match["similarity"],
                    "similarity_percent": match["similarity_percent"],
                    "match_type": match["match_type"],
                }],
            })
        if candidate_index is not None:
            target = exact_by_passage if match["match_type"] == "exact" else approx_by_passage
            target[candidate_index].append({
                "type": "online", "source_type": "online", "title": title, "source_title": title,
                "url": url, "source_url": url, "evidence": evidence, "source_text": evidence,
                "matched_text": matched_text,
                "similarity": match["similarity"], "similarity_percent": match["similarity_percent"],
                "match_type": match["match_type"],
            })
    report["sources"] = list(grouped.values())
    report["online_matches"] = report_matches
    report["online_pages_analyzed"] = len(grouped)
    _unique_errors(report)
    return exact_by_passage, approx_by_passage


def run_scan(scan_id, uid, file_id, online_requested, resume_report_id=None, online_provider="quetext"):
    """Run a scan and persist a terminal state even if setup fails before analysis starts."""
    try:
        _run_scan_impl(scan_id, uid, file_id, online_requested, resume_report_id, online_provider)
    except Exception:
        logger.exception("Plagiarism scan worker failed during startup for scan_id=%s uid=%s", scan_id, uid)
        try:
            rows = q("SELECT status,report_json FROM plagiarism_scans WHERE id=? AND uid=?", (scan_id, uid))
            if rows and rows[0]["status"] not in {"completed", "completed_with_warnings", "failed"}:
                report = json.loads(rows[0]["report_json"])
                report.setdefault("errors", []).append(
                    "The scan worker could not start or continue. The saved scan was marked failed; retry it to continue."
                )
                _unique_errors(report)
                _store(scan_id, uid, "failed", report)
        except Exception:
            logger.exception("Could not persist failed state for plagiarism scan_id=%s", scan_id)


def _run_scan_impl(scan_id, uid, file_id, online_requested, resume_report_id=None, online_provider="quetext"):
    """Run local analysis and optional bounded web searches as a background task."""
    scan_started = time.perf_counter()
    logger.info("Plagiarism scan worker started scan_id=%s uid=%s file_id=%s online=%s provider=%s",
                scan_id, uid, file_id, online_requested, online_provider)
    rows = q(
        "SELECT id,name,path,kind,text,demo_key FROM files WHERE id=? AND uid=?",
        (file_id, uid),
    )
    if not rows:
        _empty_report(scan_id, uid, "Unavailable document", online_requested,
                      "The document is no longer in this account.", online_provider=online_provider)
        return
    file = rows[0]
    is_demo = bool(file["demo_key"])
    report = _base_report(file["name"], online_requested, is_demo, online_provider)
    report["scan_id"] = scan_id
    _store(scan_id, uid, "scanning", report)
    try:
        extraction_started = time.perf_counter()
        text = (file["text"] or "").strip()
        extraction_source = "stored_text"
        if not text:
            extraction_source = "original_file"
            extension = os.path.splitext(file["name"])[1].lower()
            path = stored_file_path(file["path"])
            if not path.is_file():
                _empty_report(scan_id, uid, file["name"], online_requested,
                              "The stored original file is missing.", is_demo, online_provider)
                return
            try:
                text, _ = extract(str(path), file["kind"], extension)
            except Exception as error:
                logger.exception("Plagiarism document extraction failed scan_id=%s file_id=%s ext=%s",
                                 scan_id, file_id, extension)
                message = (
                    "OCR is not configured: install the Tesseract executable and restart SmartFiles."
                    if type(error).__name__ == "TesseractNotFoundError"
                    else f"Text extraction failed ({type(error).__name__}); the original upload is still available."
                )
                _empty_report(scan_id, uid, file["name"], online_requested, message, is_demo, online_provider)
                return
            text = (text or "").strip()
        logger.info("Plagiarism document text scan_id=%s file_id=%s name=%r source=%s chars=%d words=%d elapsed_ms=%.1f",
                    scan_id, file_id, file["name"], extraction_source, len(text), len(_words(text)),
                    (time.perf_counter() - extraction_started) * 1000)
        if not text:
            if os.path.splitext(file["name"])[1].lower() == ".pdf":
                error = "No readable text was extracted from this PDF. It may be blank or scanned; scanned pages require legible text and Tesseract OCR."
            else:
                error = "No readable text was extracted from this document."
            _empty_report(scan_id, uid, file["name"], online_requested, error, is_demo, online_provider)
            return

        passage_started = time.perf_counter()
        all_sentences = _sentences_with_offsets(text)
        passages, total_eligible = _candidate_passages(all_sentences)
        if not passages:
            _empty_report(scan_id, uid, file["name"], online_requested,
                          "The document has no passages long enough to compare.", is_demo, online_provider)
            return
        logger.info("Plagiarism passages prepared scan_id=%s file_id=%s eligible_passages=%d analyzed_passages=%d eligible_words=%d elapsed_ms=%.1f",
                    scan_id, file_id, total_eligible, len(passages),
                    sum(passage["words"] for passage in passages),
                    (time.perf_counter() - passage_started) * 1000)
        report["eligible_words_analyzed"] = sum(passage["words"] for passage in passages)
        report["analyzed_passages"] = len(passages)
        if total_eligible > len(passages):
            report["skipped_passages"] = total_eligible - len(passages)
            report["errors"].append(
                f"Analysis sampled {len(passages)} of {total_eligible} eligible passages to keep comparisons bounded."
            )

        if online_requested:
            # Online plagiarism reports are website-only. Never mix in matches from
            # another saved PDF or document in this account.
            local_exact, local_approx = defaultdict(list), defaultdict(list)
        else:
            local_exact, local_approx = _local_comparison(uid, file_id, passages, report)
        _store(scan_id, uid, "searching" if online_requested else "scanning", report)
        if online_requested and online_provider == "academic":
            online_exact, online_approx = _academic_comparison(
                passages, report, progress_callback=lambda: _store(scan_id, uid, "searching", report),
                scan_id=scan_id,
            )
        elif online_requested:
            online_exact, online_approx = _online_comparison(
                passages, report, text,
                progress_callback=lambda: _store(scan_id, uid, "searching", report),
                existing_report_id=resume_report_id,
            )
        else:
            online_exact, online_approx = defaultdict(list), defaultdict(list)

        exact_passages = set()
        approximate_passages = set()
        matches = []
        for index, passage in enumerate(passages):
            exact_sources = []
            approximate_sources = []
            for source in local_exact.get(index, []):
                exact_sources.append(source)
            for source in local_approx.get(index, []):
                approximate_sources.append(source)
            if exact_sources:
                exact_passages.add(index)
            if approximate_sources:
                approximate_passages.add(index)
            if exact_sources or approximate_sources:
                matches.append({
                    "passage": passage["text"],
                    "matched_text": passage["text"],
                    "match_type": "exact" if exact_sources else "approximate",
                    "passage_index": passage["index"],
                    "start_offset": passage.get("start_offset"),
                    "end_offset": passage.get("end_offset"),
                    "exact_sources": exact_sources,
                    "approximate_sources": approximate_sources,
                })

        exact_words = sum(passages[index]["words"] for index in exact_passages)
        approximate_only_passages = approximate_passages - exact_passages
        approximate_only_words = sum(passages[index]["words"] for index in approximate_only_passages)
        analyzed_words = report["eligible_words_analyzed"]
        percent = round(100 * exact_words / analyzed_words, 1) if analyzed_words else 0
        approximate_percent = round(100 * approximate_only_words / analyzed_words, 1) if analyzed_words else 0
        report["estimated_text_similarity_percent"] = percent
        report["approximate_text_similarity_percent"] = approximate_percent
        report["unmatched_text_percent"] = round(max(0, 100 - percent), 1)
        report["exact_match_count"] = len(exact_passages)
        report["approximate_match_count"] = len(approximate_only_passages)
        report["matches"] = matches
        online_exact_passages = set(online_exact)
        online_approximate_passages = set(online_approx) - online_exact_passages
        online_exact_words = sum(passages[index]["words"] for index in online_exact_passages)
        online_approximate_words = sum(passages[index]["words"] for index in online_approximate_passages)
        if report["online_pages_analyzed"]:
            report["online_exact_match_count"] = len(online_exact_passages)
            report["online_approximate_match_count"] = len(online_approximate_passages)
        if report["online_pages_analyzed"]:
            report["online_exact_similarity_percent"] = round(100 * online_exact_words / analyzed_words, 1) if analyzed_words else 0
            report["online_approximate_similarity_percent"] = round(100 * online_approximate_words / analyzed_words, 1) if analyzed_words else 0
        mapped_online_matches = [
            {
                "passage": passages[index]["text"],
                "matched_text": passages[index]["text"],
                "match_type": "exact" if index in online_exact_passages else "approximate",
                "passage_index": passages[index]["index"],
                "start_offset": passages[index].get("start_offset"),
                "end_offset": passages[index].get("end_offset"),
                "sources": [
                    {
                        **source,
                        "match_type": source.get("match_type", "exact" if index in online_exact_passages else "approximate"),
                        "matched_text": source.get("matched_text", passages[index]["text"]),
                        "passage_index": passages[index]["index"],
                        "start_offset": passages[index].get("start_offset"),
                        "end_offset": passages[index].get("end_offset"),
                    }
                    for source in online_exact.get(index, []) + online_approx.get(index, [])
                ],
            }
            for index in sorted(online_exact_passages | online_approximate_passages)
        ]
        # Preserve provider matches already grouped by academic source.
        if not report.get("online_matches"):
            report["online_matches"] = mapped_online_matches
        report["possible_matching_sources"] = len(report["sources"]) + len(report["local_matches"])
        if online_requested and report["online_search_status"] in {
            "unavailable_missing_api_key", "invalid_api_key", "rate_limited",
            "quota_exhausted", "provider_error", "provider_timeout", "access_denied",
            "insufficient_text", "document_too_large", "completed_with_errors",
        }:
            final_status = "completed_with_warnings"
        elif report["errors"]:
            final_status = "completed_with_warnings"
        else:
            final_status = "completed"
        _unique_errors(report)
        _store(scan_id, uid, final_status, report)
        logger.info("Plagiarism scan completed scan_id=%s status=%s exact_passages=%d approximate_passages=%d online_matches=%d source_count=%d errors=%d elapsed_ms=%.1f",
                    scan_id, final_status, report["exact_match_count"], report["approximate_match_count"],
                    len(report.get("online_matches", [])), len(report.get("sources", [])),
                    len(report.get("errors", [])), (time.perf_counter() - scan_started) * 1000)
    except Exception:
        logger.exception("Plagiarism scan failed for scan_id=%s uid=%s", scan_id, uid)
        report["errors"].append(
            "The scan could not be completed because of an internal processing error. "
            "Try again or contact support."
        )
        _unique_errors(report)
        _store(scan_id, uid, "failed", report)


def _saved_scan(uid, scan_id):
    rows = q(
        "SELECT id,uid,fid,document_name,status,created,updated,report_json "
        "FROM plagiarism_scans WHERE id=? AND uid=?",
        (scan_id, uid),
    )
    if not rows:
        raise HTTPException(404, "Plagiarism report not found.")
    row = rows[0]
    report = json.loads(row["report_json"])
    return row, report


def get_report(uid, scan_id):
    row, report = _saved_scan(uid, scan_id)
    if row["status"] in {"queued", "scanning", "searching"} and _now() - row["updated"] > SCAN_STALE_SECONDS:
        report.setdefault("errors", []).append(
            "The background scan stopped updating and was marked failed. Start a new scan to retry."
        )
        _unique_errors(report)
        report["status"] = "failed"
        with transaction() as connection:
            connection.execute(
                "UPDATE plagiarism_scans SET status='failed',updated=?,report_json=? "
                "WHERE id=? AND uid=? AND status IN ('queued','scanning','searching') AND updated=?",
                (_now(), json.dumps(report, ensure_ascii=False), scan_id, uid, row["updated"]),
            )
        row, report = _saved_scan(uid, scan_id)
    if report.get("online_search_requested"):
        # Older saved online reports may contain local matches from previous
        # versions. Keep them out of the website-source report and its PDF.
        report.update(
            matches=[], local_matches=[], exact_match_count=None, approximate_match_count=None,
            possible_matching_sources=len(report.get("sources", [])),
            estimated_text_similarity_percent=None,
            approximate_text_similarity_percent=None,
            unmatched_text_percent=None,
            eligible_words_analyzed=None, analyzed_passages=None, skipped_passages=None,
        )
    return {
        **report, "scan_id": row["id"], "status": row["status"],
        "scan_date": report.get("scan_date") or _timestamp(row["created"]),
        "updated_at": _timestamp(row["updated"]),
    }


def list_reports(uid, file_id=None, limit=50, offset=0):
    if file_id is not None:
        rows = q(
            "SELECT id,fid,document_name,status,created,updated,report_json FROM plagiarism_scans "
            "WHERE uid=? AND fid=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
            (uid, file_id, limit, offset),
        )
    else:
        rows = q(
            "SELECT id,fid,document_name,status,created,updated,report_json FROM plagiarism_scans "
            "WHERE uid=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
            (uid, limit, offset),
        )
    reports = []
    for row in rows:
        report = json.loads(row["report_json"])
        reports.append({
            "scan_id": row["id"],
            "file_id": row["fid"],
            "document_name": row["document_name"],
            "status": row["status"],
            "scan_date": report.get("scan_date") or _timestamp(row["created"]),
            "estimated_text_similarity_percent": report.get("estimated_text_similarity_percent"),
            "approximate_text_similarity_percent": report.get("approximate_text_similarity_percent"),
            "exact_match_count": report.get("exact_match_count"),
            "approximate_match_count": report.get("approximate_match_count"),
            "online_exact_match_count": report.get("online_exact_match_count"),
            "online_approximate_match_count": report.get("online_approximate_match_count"),
            "eligible_words_analyzed": report.get("eligible_words_analyzed"),
            "analyzed_passages": report.get("analyzed_passages"),
            "skipped_passages": report.get("skipped_passages"),
            "skipped_online_passages": report.get("skipped_online_passages"),
            "online_pages_analyzed": report.get("online_pages_analyzed"),
            "online_search_requested": report.get("online_search_requested"),
            "online_search_status": report.get("online_search_status"),
            "sample_data": report.get("sample_data", False),
        })
    return reports


def _comparison_match_records(report, scope):
    records = {}

    def add(passage, match_type, source_id, source_name, source_url, evidence):
        if not isinstance(passage, str) or not isinstance(match_type, str):
            return
        normalized_passage = _normalize(passage)
        if not normalized_passage:
            return
        if scope == "local":
            source_key = str(source_id) if source_id is not None else _normalize(source_name or "")
        else:
            source_key = _safe_url(source_url)
        if not source_key:
            return
        key = (match_type, normalized_passage, source_key)
        records[key] = {
            "passage": passage,
            "match_type": match_type,
            "source_name": source_name or source_url or "Unknown source",
            "source_url": source_url or None,
            "evidence": evidence if isinstance(evidence, str) else "",
        }

    if scope == "local":
        groups = report.get("local_matches")
        if not isinstance(groups, list):
            return None
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("matches"), list):
                continue
            for match in group["matches"]:
                if isinstance(match, dict):
                    add(
                        match.get("passage"), match.get("match_type"),
                        group.get("document_id"), group.get("document_name"), None,
                        match.get("evidence"),
                    )
        return records

    matches = report.get("online_matches")
    if not isinstance(matches, list):
        return None
    for match in matches:
        if not isinstance(match, dict) or not isinstance(match.get("sources"), list):
            continue
        for source in match["sources"]:
            if isinstance(source, dict):
                add(
                    match.get("passage"), match.get("match_type"), None,
                    source.get("title"), source.get("url"), source.get("evidence"),
                )
    return records


def _comparison_source_records(report):
    sources = report.get("sources")
    if not isinstance(sources, list):
        return None
    return {
        url: {
            "title": source.get("title") or url,
            "url": url,
            "verification": source.get("verification"),
        }
        for source in sources
        if isinstance(source, dict)
        and (url := _safe_url(source.get("url")))
    }


def _local_matched_source_ids(report):
    groups = report.get("local_matches")
    if not isinstance(groups, list):
        return None
    return {
        str(group["document_id"])
        for group in groups
        if isinstance(group, dict)
        and group.get("document_id") is not None
        and isinstance(group.get("matches"), list)
        and group["matches"]
    }


def _match_differences(earlier, later):
    if earlier is None or later is None:
        return None
    earlier_keys, later_keys = set(earlier), set(later)

    def ordered(records, keys):
        return [records[key] for key in sorted(keys, key=lambda item: (item[0], item[1], item[2]))]

    return {
        "newly_detected": ordered(later, later_keys - earlier_keys),
        "no_longer_detected": ordered(earlier, earlier_keys - later_keys),
    }


def _metric_change(earlier_report, later_report, key):
    earlier = earlier_report.get(key)
    later = later_report.get(key)
    if isinstance(earlier, bool) or not isinstance(earlier, (int, float)):
        earlier = None
    if isinstance(later, bool) or not isinstance(later, (int, float)):
        later = None
    return {
        "earlier": earlier,
        "later": later,
        "change": later - earlier if earlier is not None and later is not None else None,
    }


def _online_results_available(report):
    return (
        report.get("online_search_requested") is True
        and report.get("online_search_status") in {"completed", "completed_with_errors"}
        and isinstance(report.get("searches_completed"), int)
        and report["searches_completed"] > 0
    )


def compare_reports(uid, earlier_scan_id, later_scan_id):
    if earlier_scan_id == later_scan_id:
        raise HTTPException(422, "Choose two different saved scans to compare.")
    earlier_row, earlier = _saved_scan(uid, earlier_scan_id)
    later_row, later = _saved_scan(uid, later_scan_id)
    if earlier_row["fid"] != later_row["fid"]:
        raise HTTPException(422, "Only scans of the same uploaded document can be compared.")

    warnings = []
    if earlier_row["status"] not in {"completed", "completed_with_warnings"} or later_row["status"] not in {"completed", "completed_with_warnings"}:
        warnings.append("One or both scans are incomplete or failed; match and coverage changes may be partial.")
    if earlier.get("method") != later.get("method"):
        warnings.append("The scan methods differ; match changes may not be directly comparable.")
    if earlier.get("online_search_requested") != later.get("online_search_requested"):
        warnings.append("Online source discovery was enabled for only one scan; online results are not directly comparable.")
    if (
        earlier.get("online_search_provider") != later.get("online_search_provider")
        or earlier.get("online_search_mode") != later.get("online_search_mode")
    ):
        warnings.append("Online search provider or comparison mode differs between scans.")

    for key, label in (
        ("eligible_words_analyzed", "Analyzed word counts differ"),
        ("analyzed_passages", "Analyzed passage counts differ"),
        ("skipped_passages", "Locally unchecked passage counts differ"),
        ("skipped_online_passages", "Online unchecked passage counts differ"),
    ):
        if earlier.get(key) != later.get(key):
            warnings.append(f"{label}; coverage changed between scans.")

    earlier_online_status = earlier.get("online_search_status")
    later_online_status = later.get("online_search_status")
    if earlier_online_status != later_online_status:
        warnings.append("Online search availability or completion status differs between scans.")
    earlier_pages = earlier.get("online_pages_analyzed")
    later_pages = later.get("online_pages_analyzed")
    if earlier_pages != later_pages:
        warnings.append("The number of online pages with returned text differs; online source coverage changed.")

    old_local = _comparison_match_records(earlier, "local")
    new_local = _comparison_match_records(later, "local")
    old_local_sources = _local_matched_source_ids(earlier)
    new_local_sources = _local_matched_source_ids(later)
    if old_local_sources is not None and new_local_sources is not None and old_local_sources != new_local_sources:
        warnings.append("The set of local documents producing matches differs between scans.")
    online_comparable = _online_results_available(earlier) and _online_results_available(later)
    old_online = _comparison_match_records(earlier, "online") if online_comparable else None
    new_online = _comparison_match_records(later, "online") if online_comparable else None
    local_changes = _match_differences(old_local, new_local)
    online_changes = _match_differences(old_online, new_online)
    if local_changes is None:
        warnings.append("Stored local match details are unavailable for at least one scan.")
    if online_changes is None:
        warnings.append("Stored online match details are unavailable for at least one scan.")
    elif online_comparable:
        warnings.append("Online sources can change over time; online match changes describe these reports only.")
    elif earlier.get("online_search_requested") or later.get("online_search_requested"):
        warnings.append("Verified online results are unavailable for both scans, so online match changes cannot be compared.")

    earlier_sources = _comparison_source_records(earlier) if online_comparable else None
    later_sources = _comparison_source_records(later) if online_comparable else None
    source_changes = None
    if earlier_sources is not None and later_sources is not None:
        old_urls, new_urls = set(earlier_sources), set(later_sources)
        if old_urls != new_urls:
            warnings.append("The set of returned online sources differs; online match changes may reflect source availability.")
        source_changes = {
            "newly_returned": [later_sources[url] for url in sorted(new_urls - old_urls)],
            "no_longer_returned": [earlier_sources[url] for url in sorted(old_urls - new_urls)],
        }
    else:
        warnings.append("Stored online source lists are unavailable for at least one scan.")

    metric_keys = {
        "local_exact_matches": "exact_match_count",
        "local_approximate_matches": "approximate_match_count",
        "online_exact_matches": "online_exact_match_count",
        "online_approximate_matches": "online_approximate_match_count",
        "analyzed_words": "eligible_words_analyzed",
        "analyzed_passages": "analyzed_passages",
        "locally_unchecked_passages": "skipped_passages",
        "online_unchecked_passages": "skipped_online_passages",
        "online_pages_checked": "online_pages_analyzed",
    }
    metrics = {name: _metric_change(earlier, later, key) for name, key in metric_keys.items()}
    for key in ("online_exact_matches", "online_approximate_matches", "online_unchecked_passages", "online_pages_checked"):
        if earlier.get("online_search_requested") is not True:
            metrics[key]["earlier"] = None
            metrics[key]["change"] = None
        if later.get("online_search_requested") is not True:
            metrics[key]["later"] = None
            metrics[key]["change"] = None
    return {
        "document_name": later_row["document_name"],
        "earlier_scan": {
            "scan_id": earlier_row["id"],
            "scan_date": earlier.get("scan_date") or _timestamp(earlier_row["created"]),
            "status": earlier_row["status"],
        },
        "later_scan": {
            "scan_id": later_row["id"],
            "scan_date": later.get("scan_date") or _timestamp(later_row["created"]),
            "status": later_row["status"],
        },
        "metrics": metrics,
        "local_matches": local_changes,
        "online_matches": online_changes,
        "sources": source_changes,
        "warnings": warnings,
        "interpretation": (
            "This compares two stored scan observations only. Fewer detected matches do not mean the document "
            "became more original; changes can reflect scan coverage, settings, source availability, or document changes."
        ),
    }
