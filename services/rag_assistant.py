"""Owner-scoped retrieval, grounded answers, and document comparison."""
import json
import logging
import math, os, re
from difflib import get_close_matches
import numpy as np
from fastapi import HTTPException
from database.repository import q, stored_file_path, transaction
from services.document_processor import (MAX_EXTRACTED_CHARS, embed, embedding_backend,
                                         embedding_model_state, extract, kind_of, make_chunks, unblob)
from services.document_ingestion import index_stored_text, refresh_owner_embeddings
from services.categorizer import STOP, keywords
from services.summarizer import summarize
from services.document_comparison import compare_documents
from services.llm_service import ModelUnavailable, generate_answer, model_settings


# ---------- Owner-scoped retrieval and local extractive answers ----------
QUESTION_STOP = STOP | set("what why how when where who whom which is are do does did tell explain summarize summary give me about please document documents file files".split())
INSUFFICIENT = "I couldn't find enough relevant information in your uploaded documents to answer that. Which file or specific detail should I check?"
logger = logging.getLogger(__name__)
SUMMARY_INTENT = re.compile(r"\b(summar(?:y|ize|ise)|main points|important points|key points|major points|key facts|overview|main idea|what is .* about|what .* about|describe)\b", re.I)
METADATA_QUERY_STOP = QUESTION_STOP | set(
    "have has had there find locate search show list name filename file type document documents "
    "my your our any matching matched related about please available exist do does did is are not"
    .split()
)
METADATA_FILE_TYPES = {"pdf", "docx", "txt", "md", "csv", "xlsx", "pptx", "png", "jpg", "jpeg", "webp", "bmp", "gif", "mp4", "mkv", "mov", "avi", "webm"}
METADATA_SYNONYMS = {"cv": "resume", "curriculum": "resume", "vitae": "resume", "resumes": "resume"}


def _assistant_debug(event, **fields):
    """Opt-in request tracing; records metadata only, never evidence text or secrets."""
    if os.getenv("SMARTFILES_ASSISTANT_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info("assistant_debug event=%s details=%s", event,
                    json.dumps(fields, ensure_ascii=False, default=str))


def _section_reference(text):
    page = re.search(r"(?im)^\s*Page\s+(\d+)\b", text)
    if page:
        return f"Page {page.group(1)}"
    slide = re.search(r"(?im)^\s*Slide\s+(\d+)\b", text)
    if slide:
        return f"Slide {slide.group(1)}"
    section = re.search(r"(?im)^\s*(?:Section:\s*|#{1,6}\s*)([^\r\n]{1,120})", text)
    return f"Section: {section.group(1).strip()}" if section else ""


def _question_terms(question):
    return [term for term in dict.fromkeys(re.findall(r"[a-z0-9]+", question.lower()))
            if len(term) > 2 and term not in QUESTION_STOP]


def _metadata_terms(question):
    """Keep only descriptive words for an owner-scoped metadata lookup."""
    terms = []
    for term in re.findall(r"[a-z0-9]+", question.lower()):
        term = METADATA_SYNONYMS.get(term, term)
        if (len(term) <= 2 and term not in METADATA_FILE_TYPES) or (term in METADATA_QUERY_STOP and term not in METADATA_FILE_TYPES):
            continue
        # Basic English plural normalization makes "resumes" match "resume".
        normalized = term[:-1] if len(term) > 4 and term.endswith("s") and not term.endswith("ss") else term
        if normalized not in terms:
            terms.append(normalized)
    return terms


def _is_document_existence_question(question):
    return bool(re.search(
        r"\bdo\s+(?:i|you|we)\s+have\b|\b(?:is|are)\s+there\b|"
        r"\bfind\s+(?:my|me|a|an|the)\b|\bwhat(?:\s+is|'s)\s+(?:the\s+)?(?:name|filename)\b",
        question, re.I,
    ))


def search_document_metadata(uid, question):
    """Search filenames and metadata first; never reads another user's rows."""
    terms = _metadata_terms(question)
    if not terms:
        return []
    rows = q("select id,uid,name,kind,category,tags,demo_key from files where uid=? order by created desc,id desc", (uid,))
    matches = []
    for row in rows:
        name = row["name"] or ""
        extension = os.path.splitext(name)[1].lstrip(".").lower()
        metadata = " ".join((name, os.path.splitext(name)[0], extension, row["kind"] or "",
                             row["category"] or "", row["tags"] or "")).lower()
        metadata_terms = {METADATA_SYNONYMS.get(term, term) for term in re.findall(r"[a-z0-9]+", metadata)}
        metadata_terms |= {term[:-1] for term in metadata_terms
                           if len(term) > 4 and term.endswith("s") and not term.endswith("ss")}
        matched_terms = []
        for term in terms:
            if term in metadata_terms:
                matched_terms.append(term)
            elif len(term) >= 5 and get_close_matches(term, metadata_terms, n=1, cutoff=0.88):
                matched_terms.append(term)
        # When the user provides multiple identifying words, require all of
        # them. This avoids returning a resume for an unrelated "topic" query.
        if len(matched_terms) == len(terms):
            matches.append({"id": row["id"], "name": name, "kind": row["kind"],
                            "category": row["category"], "file_type": extension.upper() or "Unknown",
                            "is_demo": bool(row["demo_key"]), "matched_terms": matched_terms})
    return matches


def _metadata_sources(matches):
    return [{"ref": f"S{index}", "id": item["id"], "name": item["name"],
             "kind": item["kind"], "file_type": item["file_type"],
             "category": item["category"], "is_demo": item["is_demo"],
             "reference": "", "snippet": ""}
            for index, item in enumerate(matches, 1)]


def _is_summary_question(question):
    return bool(SUMMARY_INTENT.search(question))


def _is_comparison_question(question):
    return bool(re.search(r"\b(compare|comparison|difference|different|similarity|similar)\b", question, re.I))


def _is_all_documents_summary(question):
    return bool(re.search(r"\b(my|all|every|overall)\s+(documents?|files?)\b", question, re.I))


def _is_email_question(question):
    return bool(re.search(r"\b(e-?mail|email address)\b", question, re.I))


def _is_small_talk(question):
    return bool(re.fullmatch(
        r"\s*(?:hi|hello|hey|good morning|good afternoon|good evening|"
        r"ok|okay|thanks|thank you|thx|bye|goodbye|see you)[!.?,\s]*", question, re.I))


def _clean_history(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for turn in value[-8:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        text = turn.get("text")
        if role in ("user", "assistant") and isinstance(text, str) and text.strip():
            cleaned.append({"role": role, "text": text.strip()[:1600]})
    return cleaned


def _try_generated_answer(question, history, hits, extraction_warnings, retrieval_mode,
                          extra_evidence=None, extra_sources=None, allow_general=False,
                          model_errors=None):
    """Use Gemini for an answer grounded in retrieved local evidence."""
    model = model_settings()
    if not model:
        if model_errors is not None:
            model_errors.append("Set GEMINI_API_KEY in the backend .env file to enable AI answers.")
        return None
    if not hits and not extra_evidence and not allow_general:
        return None
    evidence = []
    for hit in hits[:4]:
        evidence.append({
            "source_ref": hit.get("source_ref", ""),
            "name": hit.get("name", "Document"),
            "reference": _section_reference(hit.get("text", "")),
            "text": hit.get("text", ""),
        })
    evidence.extend(extra_evidence or [])
    try:
        # Old turns are useful only when the current message is an actual
        # follow-up. They are never needed to answer a fresh question.
        # A follow-up needs only the immediately preceding exchange. Sending
        # older turns lets unrelated topics from a long chat bias this answer.
        conversation_history = history[-2:] if _is_contextual_follow_up(question) else []
        answer = generate_answer(question, history=conversation_history, evidence=evidence,
                                 project_context=_is_project_question(question))
    except ModelUnavailable as error:
        logger.warning("Gemini unavailable; returning an explicit error or exact document text (%s)", error)
        if model_errors is not None:
            model_errors.append(str(error))
        return None
    valid_refs = {hit.get("source_ref") for hit in hits if hit.get("source_ref")}
    valid_refs.update(item.get("source_ref") for item in (extra_evidence or []) if item.get("source_ref"))
    citations = [ref for ref in dict.fromkeys(re.findall(r"\[([SP]\d+)\]", answer)) if ref in valid_refs]
    return {
        "answer": answer,
        "sources": _assistant_sources(hits) + list(extra_sources or []),
        "citations": citations,
        "answer_mode": "generated",
        "ai_status": "gemini",
        "extraction_warnings": extraction_warnings,
        "retrieval_mode": retrieval_mode,
    }


_PLAGIARISM_INTENT = re.compile(r"\b(plagiar(?:ism|ized|ised)|similarity|originality|match(?:es|ed)?|source report|check report|scan report)\b", re.I)
_WORKSPACE_INTENT = re.compile(r"\b(my|our|uploaded|document|documents|file|files|paper|papers|research|plagiar|similarity|report|source)\b", re.I)
_PROJECT_INTENT = re.compile(
    r"\bsmartfiles\b|"
    r"\bwhat can (?:you|smartfiles) do\b|"
    r"\bwhat does (?:this|the) (?:app|project|assistant) do\b|"
    r"\b(?:features|capabilities|architecture|tech stack|structure|purpose)\b.{0,35}\b(?:project|app|assistant)\b|"
    r"\b(?:project|app|assistant)\b.{0,35}\b(?:features|capabilities|architecture|tech stack|structure|purpose)\b",
    re.I,
)
_PERSONAL_DOCUMENT_INTENT = re.compile(r"\b(my|uploaded|this|that)\s+(?:own\s+)?(?:document|file|paper|report)s?\b|\bmy\s+(?:resume|cv|paper|report)s?\b", re.I)
_FOLLOW_UP = re.compile(r"^(?:why\??|how\??|more\??|continue\??|elaborate\??|tell me more\.?|give me (?:an|another) example\.?|what about (?:it|that|this)\??|how about (?:it|that|this)\??)$", re.I)
_DEICTIC_FOLLOW_UP = re.compile(r"\b(?:it|its|that|those|them|this|these|their)\b", re.I)
_DEICTIC_NOUN = re.compile(r"\b(?:this|that|these|those)\s+(?:project|app|document|file|paper|report|feature|service|question|method|approach|process|concept)\b", re.I)


def _is_project_question(question):
    return bool(_PROJECT_INTENT.search(question)) and not bool(_PERSONAL_DOCUMENT_INTENT.search(question))


def _is_contextual_follow_up(question):
    words = re.findall(r"\b\w+\b", question)
    if len(words) <= 6 and _FOLLOW_UP.fullmatch(question.strip()):
        return True
    return len(words) <= 9 and bool(_DEICTIC_FOLLOW_UP.search(question)) and not bool(_DEICTIC_NOUN.search(question))


def _plagiarism_context(uid, question, document_ids=None, include_recent=False):
    """Build compact context only from this owner's persisted reports and verified matches."""
    if not document_ids and not include_recent:
        return [], []
    sql = ("SELECT s.id,s.fid,s.document_name,s.status,s.report_json FROM plagiarism_scans s "
           "JOIN files f ON f.id=s.fid AND f.uid=s.uid WHERE s.uid=?")
    args = [uid]
    if document_ids:
        ids = list(dict.fromkeys(int(item) for item in document_ids if int(item) > 0))[:20]
        if not ids:
            return [], []
        sql += " AND s.fid IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    sql += " ORDER BY s.created DESC,s.id DESC LIMIT ?"
    args.append(15 if include_recent else 8)
    rows = q(sql, tuple(args))
    contexts, sources = [], []
    allowed_verification = {"accessible_full_text", "provider_match", "provider_text_match"}
    for row in rows:
        try:
            report = json.loads(row["report_json"] or "{}")
        except (ValueError, TypeError):
            continue
        ref = "P" + str(len(contexts) + 1)
        lines = [f"Saved plagiarism scan #{row['id']} for {row['document_name']}; status: {row['status']}."]
        online_status = report.get("online_search_status")
        if online_status:
            lines.append("Academic/online search status: " + str(online_status) + ".")
        if report.get("academic_coverage_percent") is not None:
            lines.append(f"Academic matched-passage coverage: {report['academic_coverage_percent']}%.")
        for label, key in (("Exact local passage matches", "exact_match_count"),
                           ("Approximate local passage matches", "approximate_match_count")):
            value = report.get(key)
            if value is not None:
                lines.append(f"{label}: {value}.")
        verified_count = 0
        verified_url = ""
        seen = set()
        for passage_match in report.get("online_matches", []) if isinstance(report.get("online_matches"), list) else []:
            if not isinstance(passage_match, dict):
                continue
            passage = passage_match.get("passage", "")
            for source in passage_match.get("sources", []) if isinstance(passage_match.get("sources"), list) else []:
                if not isinstance(source, dict) or source.get("verification") not in allowed_verification:
                    continue
                excerpt = source.get("evidence") or source.get("source_text")
                if not isinstance(excerpt, str) or not excerpt.strip():
                    continue
                marker = (str(source.get("doi") or source.get("url") or source.get("source_url")), str(passage))
                if marker in seen:
                    continue
                seen.add(marker)
                verified_count += 1
                title = str(source.get("title") or source.get("source_title") or "Source")[:180]
                authors = source.get("authors") if isinstance(source.get("authors"), list) else []
                similarity = source.get("similarity_percent")
                paper_url = str(source.get("doi") and ("https://doi.org/" + str(source["doi"]))
                                or source.get("url") or source.get("source_url") or "")
                if not verified_url and paper_url.startswith(("https://", "http://")):
                    verified_url = paper_url
                lines.append(f"Verified match: document passage: {str(passage)[:500]}; source paper: {title}; "
                             f"authors: {', '.join(str(a) for a in authors[:8])}; "
                             f"reported passage similarity: {similarity}% when available; "
                             f"source URL: {paper_url}; matched source excerpt: {excerpt[:700]}")
        for group in report.get("local_matches", []) if isinstance(report.get("local_matches"), list) else []:
            if not isinstance(group, dict):
                continue
            for match in group.get("matches", [])[:3] if isinstance(group.get("matches"), list) else []:
                if not isinstance(match, dict):
                    continue
                title = str(match.get("source_title") or group.get("document_name") or "Owned document")[:180]
                lines.append(f"Local document overlap: passage: {str(match.get('passage', ''))[:500]}; "
                             f"other owned file: {title}; source excerpt: {str(match.get('evidence', ''))[:700]}; "
                             f"similarity: {match.get('similarity_percent', match.get('similarity', 'not reported'))}.")
        if verified_count == 0 and not report.get("local_matches"):
            lines.append("This saved report contains no verified source passage excerpts to cite.")
        contexts.append({"source_ref": ref, "name": "Plagiarism report: " + row["document_name"],
                         "reference": "Scan #" + str(row["id"]), "text": "\n".join(lines)[:1800]})
        sources.append({"ref": ref, "id": row["fid"], "name": "Plagiarism report: " + row["document_name"],
                        "kind": "plagiarism_report", "category": "Plagiarism report",
                        "is_demo": bool(report.get("sample_data")), "reference": "Scan #" + str(row["id"]),
                        "snippet": "\n".join(lines)[:700], "url": verified_url or None})
    return contexts, sources


def _clean_history(value):
    if not isinstance(value, list):
        return []
    turns = []
    for item in value[-8:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            turns.append({"role": item["role"], "text": text.strip()[:1600]})
    return turns


def _clean_file_ids(value):
    if not isinstance(value, list):
        return []
    result = []
    for raw in value[:20]:
        try:
            file_id = int(raw)
        except (TypeError, ValueError):
            continue
        if file_id > 0 and file_id not in result:
            result.append(file_id)
    return result


def _is_education_question(question):
    return bool(re.search(r"\b(where|which)\b.*\b(study|studied|education|degree|MCA|MBA|university|college)\b", question, re.I))


def _education_answer(question, hits):
    """Return an actual education/institution line when the query asks where someone studied."""
    qlower = question.lower()
    degree = "mca" if re.search(r"\bmca\b|master of computer applications", qlower) else None
    identity_terms = [term for term in _question_terms(question)
                      if term not in {"study", "studied", "education", "degree", "university", "college", "master", "computer", "applications"}]
    for hit in hits:
        content = hit["text"]
        lower = content.lower()
        if identity_terms and not any(re.search(r"\b" + re.escape(term) + r"\b", lower) for term in identity_terms):
            continue
        if degree and not ("master of computer applications" in lower or re.search(r"\bmca\b", lower)):
            continue
        for line in re.split(r"(?<=[.!?])\s+|\n+", content):
            clean = line.strip(" \t:-")
            has_institution = re.search(r"\b(university|college|institute|school)\b", clean, re.I)
            has_education = re.search(r"\b(education|study|studied|degree|master of computer applications|MCA)\b", clean, re.I)
            if clean and has_institution and has_education:
                return clean[:700], hit["source_ref"]
    return None


def _recover_text(item):
    """Retry extraction for a user's stored file whose saved text is blank."""
    def failed(message):
        with transaction() as connection:
            connection.execute(
                "update files set extraction_status='failed',extraction_error=? where id=? and uid=?",
                (message, item["id"], item["uid"]),
            )
        return "", message

    path = stored_file_path(item["path"])
    ext = os.path.splitext(item["name"])[1].lower()
    kind = kind_of(ext)
    if not kind:
        return failed("This file type does not support text extraction.")
    if not path.is_file():
        return failed("The original file is missing from storage.")
    try:
        text, _ = extract(str(path), kind, ext)
    except Exception as error:
        logger.exception("Assistant text recovery failed for uid=%s file_id=%s",
                         item["uid"], item["id"])
        if isinstance(error, ModuleNotFoundError):
            return failed(f"Missing Python module '{error.name or 'required extraction library'}': {error}")
        return failed(f"Text extraction failed ({type(error).__name__}: {error})")
    text = (text or "")[:MAX_EXTRACTED_CHARS].strip()
    if not text:
        return failed("The file contains no readable text.")
    with transaction() as connection:
        connection.execute(
            "update files set text=?, summary=case when summary is null or summary='' then ? else summary end where id=? and uid=?",
            (text, summarize(text), item["id"], item["uid"]),
        )
    return text, ""


def retrieve_evidence(uid, question, file_id=None, k=5, file_ids=None):
    """Retrieve relevant owner-scoped passages from the complete cached chunk index."""
    sql = ("select id,uid,name,path,kind,category,summary,text,demo_key,"
           "extraction_status,extraction_error from files where uid=?")
    args = [uid]
    if file_id is not None:
        sql += " and id=?"
        args.append(file_id)
    elif file_ids:
        sql += " and id in (" + ",".join("?" for _ in file_ids) + ")"
        args.extend(file_ids)
    sql += " order by created desc,id desc"
    files = [dict(item) for item in q(sql, tuple(args))]
    if not files:
        return [], "keyword", []

    query_vector = None
    query_backend = ""
    try:
        query_vector = embed([question])[0]
        query_backend = embedding_backend()
        if query_backend != "hashed-v1":
            refresh_owner_embeddings(uid, query_backend)
    except Exception:
        logger.exception("Assistant query embedding failed for uid=%s", uid)

    file_by_id = {item["id"]: item for item in files}
    chunk_sql = ("select c.fid,c.text,c.emb,c.start,c.embedder from chunks c join files f on f.id=c.fid "
                 "where c.uid=? and f.uid=?")
    chunk_args = [uid, uid]
    if file_id is not None:
        chunk_sql += " and c.fid=?"
        chunk_args.append(file_id)
    elif file_ids:
        chunk_sql += " and c.fid in (" + ",".join("?" for _ in file_ids) + ")"
        chunk_args.extend(file_ids)
    chunk_sql += " order by f.created desc,c.id asc"
    stored_chunks = q(chunk_sql, tuple(chunk_args))
    candidates = []
    chunked_ids = set()
    for chunk in stored_chunks:
        item = file_by_id.get(chunk["fid"])
        if not item or not (chunk["text"] or "").strip():
            continue
        chunked_ids.add(item["id"])
        candidates.append({"fid": item["id"], "name": item["name"], "kind": item["kind"],
                           "category": item["category"], "is_demo": bool(item["demo_key"]), "text": chunk["text"],
                           "embedding": chunk["emb"] if chunk["embedder"] == query_backend else None,
                           "start": chunk["start"]})

    terms = _question_terms(question)
    summary_intent = _is_summary_question(question)
    all_documents_summary = summary_intent and _is_all_documents_summary(question)
    extraction_warnings = []
    # Previous failed uploads remain downloadable but may have blank extracted
    # text. Retry those originals now, scoped to this user's selected documents.
    for item in files:
        if item["id"] in chunked_ids or (item["text"] or "").strip():
            continue
        # OCR requires the separate Tesseract executable. Don't run it on every
        # blank image for an unrelated question; retry only when the image's
        # filename/category or an explicit document/collection scope is relevant.
        if item["kind"] == "image" and file_id is None and not file_ids and not all_documents_summary:
            metadata = (item["name"] + " " + item["category"]).lower()
            if not any(re.search(r"\b" + re.escape(term) + r"\b", metadata) for term in terms):
                continue
        if item.get("extraction_status") in {"failed", "no_text"}:
            extraction_warnings.append({
                "name": item["name"],
                "message": item.get("extraction_error") or "Text extraction is unavailable. Use Repair text index to retry.",
            })
            continue
        text, warning = _recover_text(item)
        if warning:
            extraction_warnings.append({"name": item["name"], "message": warning})
        else:
            item["text"] = text

    # Cache chunk rows for previously extracted files so they are not rebuilt
    # from the original on every question.
    for item in files:
        if item["id"] not in chunked_ids:
            content = item["text"] or item["summary"] or ""
            if content.strip():
                try:
                    index_stored_text(uid, item["id"], content)
                    chunked_ids.add(item["id"])
                    indexed = q("select text,emb,start,embedder from chunks where fid=? and uid=? order by id",
                                (item["id"], uid))
                    for chunk in indexed:
                        candidates.append({"fid": item["id"], "name": item["name"], "kind": item["kind"],
                                           "category": item["category"], "is_demo": bool(item["demo_key"]),
                                           "text": chunk["text"],
                                           "embedding": chunk["emb"] if chunk["embedder"] == query_backend else None,
                                           "start": chunk["start"]})
                    continue
                except Exception:
                    logger.exception("Assistant chunk indexing failed for uid=%s file_id=%s", uid, item["id"])
            for start, text in make_chunks(content, None):
                candidates.append({"fid": item["id"], "name": item["name"], "kind": item["kind"],
                                   "category": item["category"], "is_demo": bool(item["demo_key"]), "text": text,
                                   "embedding": None, "start": start})
    if not candidates:
        return [], "keyword", extraction_warnings

    comparison_intent = _is_comparison_question(question)
    email_intent = _is_email_question(question)
    all_documents_summary = summary_intent and _is_all_documents_summary(question)
    semantic_scores = {}
    model_available = False
    embedded_candidates = [c for c in candidates if c["embedding"]]
    if embedded_candidates and query_vector is not None and embedding_model_state() is not False:
        try:
            model_available = True
            for item in embedded_candidates:
                vector = unblob(item["embedding"])
                if vector.size == query_vector.size:
                    value = float(np.dot(vector, query_vector))
                    old = semantic_scores.get((item["fid"], item["text"]))
                    if old is None or value > old:
                        semantic_scores[(item["fid"], item["text"])] = value
        except Exception:
            model_available = False
            semantic_scores = {}

    ranked = []
    qlower = question.lower()
    for item in candidates:
        content = item["text"]
        lower = content.lower()
        matched = [term for term in terms if re.search(r"\b" + re.escape(term) + r"\b", lower)]
        metadata_text = (item["name"] + " " + item["category"]).lower()
        metadata_matches = [term for term in terms if re.search(r"\b" + re.escape(term) + r"\b", metadata_text)]
        # Broad summaries are valid only for an explicit document scope or an
        # explicit request to summarize the whole collection. Never assign a
        # relevance score to an arbitrary latest document.
        lexical_score = (len(matched) / len(terms) * 0.72) if terms else (0.30 if file_id or all_documents_summary else 0.0)
        if terms and metadata_matches:
            lexical_score = max(lexical_score, len(metadata_matches) / len(terms) * 0.28)
        if summary_intent and metadata_matches and not matched:
            lexical_score = max(lexical_score, 0.28)
        if comparison_intent and (matched or metadata_matches):
            lexical_score = max(lexical_score, 0.30 if metadata_matches else 0.28)
        email_addresses = re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", content, re.I)
        if email_intent and email_addresses:
            lexical_score = max(lexical_score, 0.75)
        if qlower in lower:
            lexical_score = min(1.0, lexical_score + 0.28)
        semantic_score = semantic_scores.get((item["fid"], content), 0.0) if model_available else 0.0
        # Exclude weak incidental keyword/metadata overlap and low-confidence
        # semantic neighbors before they can be shown as model evidence.
        semantic_hit = semantic_score >= 0.38
        # A user-selected file, a filename-matched summary, or a tightly
        # constrained education lookup can be useful at a lower lexical score;
        # unscoped ordinary questions still require stronger evidence.
        minimum_lexical_score = 0.24 if (
            file_id is not None or file_ids or (summary_intent and metadata_matches)
            or (_is_education_question(question) and matched)
        ) else 0.32
        if lexical_score < minimum_lexical_score and not semantic_hit:
            continue
        score = max(lexical_score, semantic_score if semantic_hit else 0.0)
        if semantic_hit and lexical_score:
            score = min(1.0, score + 0.04)
        ranked.append({**item, "score": score, "lexical_score": lexical_score,
                       "semantic_score": semantic_score, "match_type":
                       "semantic + keyword" if semantic_hit and lexical_score else
                       "semantic" if semantic_hit else "keyword"})
    ranked.sort(key=lambda hit: hit["score"], reverse=True)
    # Favor distinct documents, then use a second passage from a document if needed.
    chosen, seen_docs = [], set()
    for distinct_pass in (True, False):
        for hit in ranked:
            if hit in chosen or (distinct_pass and hit["fid"] in seen_docs):
                continue
            chosen.append(hit)
            seen_docs.add(hit["fid"])
            if len(chosen) >= k:
                break
        if len(chosen) >= k:
            break

    source_map, sources = {}, []
    for hit in chosen:
        if hit["fid"] not in source_map:
            ref = "S" + str(len(sources) + 1)
            source_map[hit["fid"]] = ref
            sources.append({"ref": ref, "id": hit["fid"], "name": hit["name"],
                            "kind": hit["kind"], "category": hit["category"],
                            "is_demo": hit["is_demo"],
                            "reference": _section_reference(hit["text"]),
                            "snippets": []})
        ref = source_map[hit["fid"]]
        hit["source_ref"] = ref
        source = next(src for src in sources if src["ref"] == ref)
        if len(source["snippets"]) < 2 and hit["text"] not in source["snippets"]:
            source["snippets"].append(hit["text"][:700])
    for source in sources:
        source["snippet"] = "\n\n".join(source.pop("snippets"))

    retrieval_mode = ("semantic + keyword" if any(h["match_type"] == "semantic + keyword" for h in chosen)
                      else "semantic" if any(h["match_type"] == "semantic" for h in chosen)
                      else "keyword")
    return chosen, retrieval_mode, extraction_warnings


def answer_question(b: dict, uid: int):
    question = b.get("question") if isinstance(b, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(422, "Please enter a question")
    question = question.strip()
    _assistant_debug("request", question=question, history_turns=len(b.get("history", [])) if isinstance(b, dict) and isinstance(b.get("history"), list) else 0)
    history = _clean_history(b.get("history"))
    context_file_ids = _clean_file_ids(b.get("context_file_ids"))
    small_talk = _is_small_talk(question)
    all_documents_summary = _is_summary_question(question) and _is_all_documents_summary(question)
    if len(question) > 2000:
        raise HTTPException(413, "Question is too long (maximum 2,000 characters)")
    file_id = b.get("file_id")
    if file_id is not None:
        try: file_id = int(file_id)
        except (TypeError, ValueError): raise HTTPException(422, "Invalid document ID")
        if file_id < 1: raise HTTPException(422, "Invalid document ID")

    # Small talk and general questions should not inherit an incidental file
    # selection. A short follow-up can keep that scope when chat history exists.
    if small_talk:
        file_id = None
        context_file_ids = []
    elif file_id is not None and not _WORKSPACE_INTENT.search(question) and not _is_contextual_follow_up(question):
        file_id = None

    # Run a cheap metadata pass before reading/indexing document text. This
    # keeps filename discovery working even when PDF/OCR extraction failed.
    metadata_matches = [] if small_talk else search_document_metadata(uid, question)
    if _is_document_existence_question(question):
        if not metadata_matches:
            return {"answer": "I couldn't find a matching document in your workspace.",
                    "sources": [], "citations": [], "answer_mode": "document_not_found",
                    "ai_status": "metadata_search", "extraction_warnings": [],
                    "retrieval_mode": "metadata"}
        sources = _metadata_sources(metadata_matches)
        names = "; ".join(source["name"] for source in sources)
        if len(sources) == 1:
            answer = f"Yes. I found your document: {names}."
        else:
            answer = f"Yes. I found {len(sources)} matching documents: {names}."
        return {"answer": answer, "sources": sources,
                "citations": [source["ref"] for source in sources],
                "answer_mode": "document_found", "ai_status": "metadata_search",
                "extraction_warnings": [], "retrieval_mode": "metadata"}

    # A named document request such as "Summarize my resume" should use the
    # matching file's content rather than unrelated high-scoring passages.
    summary_file_ids = None
    if file_id is None and _is_summary_question(question) and metadata_matches:
        summary_file_ids = [item["id"] for item in metadata_matches]
        if len(summary_file_ids) == 1:
            file_id = summary_file_ids[0]
            summary_file_ids = None

    project_question = _is_project_question(question)
    retrieval_question = question
    if _is_contextual_follow_up(question) and history:
        previous_question = next((turn["text"] for turn in reversed(history[-2:])
                                  if turn["role"] == "user"), "")
        if previous_question:
            retrieval_question = previous_question + "\n" + question
    if project_question and file_id is None and not context_file_ids:
        # Product questions should use the app's known feature context, not
        # happenstance matches in the user's private uploads.
        hits, retrieval_mode, extraction_warnings = [], "application", []
    else:
        hits, retrieval_mode, extraction_warnings = retrieve_evidence(
            uid, retrieval_question, file_id, file_ids=summary_file_ids or context_file_ids or None)
    _assistant_debug("retrieval", question=question, retrieval_question=retrieval_question,
                     chunks=[{"filename": hit.get("name", ""), "score": round(float(hit.get("score", 0)), 4)}
                             for hit in hits[:5]], retrieval_mode=retrieval_mode)
    related_document_ids = list(dict.fromkeys(
        ([file_id] if file_id else []) + [hit["fid"] for hit in hits]
    ))
    if project_question and not _PERSONAL_DOCUMENT_INTENT.search(question):
        plagiarism_evidence, plagiarism_sources = [], []
    elif _PLAGIARISM_INTENT.search(question):
        plagiarism_evidence, plagiarism_sources = _plagiarism_context(
            uid, question, related_document_ids or None,
            include_recent=not related_document_ids,
        )
    else:
        # Report contents are sensitive and irrelevant to ordinary file questions.
        plagiarism_evidence, plagiarism_sources = [], []
    _assistant_debug("report_context", question=question,
                     reports=[source.get("name", "") for source in plagiarism_sources])
    model_errors = []
    if not hits:
        if plagiarism_evidence:
            generated = _try_generated_answer(
                question, history, [], extraction_warnings, retrieval_mode,
                extra_evidence=plagiarism_evidence, extra_sources=plagiarism_sources,
                model_errors=model_errors,
            )
            if generated:
                return generated
            return {"answer": "Saved plagiarism report evidence:\n" + "\n\n".join(
                        item["text"] + " [" + item["source_ref"] + "]" for item in plagiarism_evidence),
                    "sources": plagiarism_sources,
                    "citations": [item["source_ref"] for item in plagiarism_evidence],
                    "answer_mode": "extractive", "ai_status": "local_extractive",
                    "ai_error": model_errors[0] if model_errors else None,
                    "extraction_warnings": extraction_warnings, "retrieval_mode": "plagiarism_report"}
        if extraction_warnings:
            names = "; ".join(f"{item['name']}: {item['message']}" for item in extraction_warnings)
            answer = "I could not search some of your documents because text extraction failed. " + names
            mode = "extraction_unavailable"
        elif _is_summary_question(question) and file_id is None and not _question_terms(question):
            answer = "Choose a document to summarize, or ask for a summary of all your documents."
            mode = "insufficient"
        elif _is_comparison_question(question):
            answer = "I could not identify two relevant documents for that comparison. Mention their filenames or choose them on the Document Comparison page."
            mode = "insufficient"
        else:
            answer = INSUFFICIENT
            mode = "insufficient"
        if (not extraction_warnings and not _WORKSPACE_INTENT.search(question)) or (
                project_question and not _PERSONAL_DOCUMENT_INTENT.search(question)):
            generated = _try_generated_answer(
                question, history, [], extraction_warnings, retrieval_mode,
                allow_general=True, model_errors=model_errors,
            )
            if generated:
                return generated
            if model_errors:
                return {"answer": model_errors[0], "sources": [], "citations": [],
                        "answer_mode": "ai_error", "ai_status": "not_available",
                        "ai_error": model_errors[0], "extraction_warnings": [],
                        "retrieval_mode": retrieval_mode}
        return {"answer": answer, "sources": [], "citations": [],
                "answer_mode": mode, "ai_status": "not_needed",
                "ai_error": model_errors[0] if model_errors else None,
                "extraction_warnings": extraction_warnings,
                "retrieval_mode": retrieval_mode}

    if _is_summary_question(question) and (file_id is not None or all_documents_summary):
        if file_id is not None:
            summary_rows = q(
                "select id,name,kind,category,text,summary,demo_key from files where uid=? and id=?",
                (uid, file_id),
            )
        else:
            summary_rows = q(
                "select id,name,kind,category,text,summary,demo_key from files where uid=? order by created,id",
                (uid,),
            )
        summary_evidence, summary_sources, sections = [], [], []
        for row in summary_rows:
            content = (row["text"] or row["summary"] or "").strip()
            if not content:
                continue
            summary = summarize(content, n=3)
            ref = "S" + str(len(summary_sources) + 1)
            extension = os.path.splitext(row["name"] or "")[1].lstrip(".").upper()
            summary_sources.append({
                "ref": ref, "id": row["id"], "name": row["name"], "kind": row["kind"],
                "file_type": extension or row["kind"], "category": row["category"],
                "is_demo": bool(row["demo_key"]), "reference": "Full document", "snippet": summary[:700],
            })
            summary_evidence.append({
                "source_ref": ref, "name": row["name"], "reference": "Full document", "text": summary,
            })
            sections.append(f"{row['name']}: {summary} [{ref}]")
        if summary_evidence:
            generated = _try_generated_answer(
                question, history, [], extraction_warnings, retrieval_mode,
                extra_evidence=summary_evidence, extra_sources=summary_sources,
                model_errors=model_errors,
            )
            if generated:
                return generated
            return {
                "answer": "Extractive summaries from the full document text:\n" + "\n\n".join(sections),
                "sources": summary_sources, "citations": [source["ref"] for source in summary_sources],
                "answer_mode": "extractive", "ai_status": "local_extractive",
                "extraction_warnings": extraction_warnings, "ai_error": model_errors[0] if model_errors else None,
                "retrieval_mode": retrieval_mode,
            }

    # If "my resume" matches more than one real document, summarize each
    # independently so the answer cannot silently describe only the newest one.
    if summary_file_ids:
        marks = ",".join("?" for _ in summary_file_ids)
        rows = q(f"select id,name,text,summary from files where uid=? and id in ({marks}) order by created desc,id desc",
                 tuple([uid] + summary_file_ids))
        refs = _metadata_sources([item for item in metadata_matches if item["id"] in set(summary_file_ids)])
        ref_by_id = {source["id"]: source for source in refs}
        sections = []
        citations = []
        for row in rows:
            source = ref_by_id.get(row["id"])
            text = (row["text"] or row["summary"] or "").strip()
            if not source or not text:
                continue
            summary = summarize(text, n=3)
            source["snippet"] = text[:700]
            source["reference"] = _section_reference(text)
            sections.append(f"{row['name']}: {summary} [{source['ref']}]")
            citations.append(source["ref"])
        if sections:
            generated = _try_generated_answer(question, history, hits, extraction_warnings, retrieval_mode,
                                              plagiarism_evidence, plagiarism_sources,
                                              model_errors=model_errors)
            if generated:
                return generated
            return {"answer": "Extractive summaries from matching documents:\n" + "\n\n".join(sections),
                    "sources": refs, "citations": citations, "answer_mode": "extractive",
                    "ai_status": "local_extractive", "extraction_warnings": extraction_warnings,
                    "ai_error": model_errors[0] if model_errors else None,
                    "retrieval_mode": retrieval_mode}

    if _is_comparison_question(question):
        documents = []
        for hit in hits:
            if hit["fid"] not in [item["id"] for item in documents]:
                documents.append({"id": hit["fid"], "name": hit["name"], "ref": hit["source_ref"]})
            if len(documents) == 2:
                break
        if len(documents) < 2:
            return {"answer": "To compare two documents, choose All my documents and ask again, or use the Document Comparison page to select both files.",
                    "sources": _assistant_sources(hits), "citations": [item["ref"] for item in documents],
                    "answer_mode": "insufficient", "ai_status": "local_extractive",
                    "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}
        report = compare_documents({"a": documents[0]["id"], "b": documents[1]["id"]}, uid)
        def display_terms(values):
            return ", ".join(values) if values else "none identified"
        answer = (f"Compared {report['a']} ({report['a_kind']}) with {report['b']} ({report['b_kind']}). "
                  f"Their extracted text has {round(report['similarity'] * 100)}% TF-IDF cosine similarity. "
                  f"Shared topics: {display_terms(report['shared'])}. "
                  f"Only in {report['a']}: {display_terms(report['only_a'])}. "
                  f"Only in {report['b']}: {display_terms(report['only_b'])}.")
        citations = [item["ref"] for item in documents]
        visible_hits = [hit for hit in hits if hit["fid"] in {item["id"] for item in documents}]
        return {"answer": answer, "sources": _assistant_sources(visible_hits), "citations": citations,
                "answer_mode": "comparison", "ai_status": "local_extractive",
                "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}

    if _is_email_question(question):
        email_hits = []
        for hit in hits:
            for address in re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", hit["text"], re.I):
                pair = (address, hit["source_ref"])
                if pair not in email_hits:
                    email_hits.append(pair)
        if email_hits:
            answer = "Email address" + ("es" if len(email_hits) > 1 else "") + " found in the documents: " + "; ".join(
                f"{address} [{ref}]" for address, ref in email_hits[:10])
            citations = list(dict.fromkeys(ref for _, ref in email_hits[:10]))
            return {"answer": answer, "sources": _assistant_sources(hits), "citations": citations,
                    "answer_mode": "extractive", "ai_status": "local_extractive",
                    "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}
        return {"answer": INSUFFICIENT, "sources": _assistant_sources(hits), "citations": [],
                "answer_mode": "insufficient", "ai_status": "local_extractive",
                "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}

    if _is_education_question(question):
        education = _education_answer(question, hits)
        if education:
            text, citation = education
            return {"answer": "The document states: " + text + " [" + citation + "]",
                    "sources": [source for source in _assistant_sources(hits) if source["ref"] == citation],
                    "citations": [citation], "answer_mode": "extractive", "ai_status": "local_extractive",
                    "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}

    terms = _question_terms(retrieval_question)
    broad_question = not terms or _is_summary_question(question)
    passages = []
    minimum_matches = max(1, int(math.ceil(len(terms) * 0.25)) if terms else 0)
    for hit in hits:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", hit["text"])
        for sentence in sentences:
            sentence = sentence.strip()
            matched = [term for term in terms if term in sentence.lower()]
            if sentence and len(sentence.split()) >= 5 and (broad_question or len(matched) >= minimum_matches):
                passages.append((len(matched), hit["score"], sentence, hit["source_ref"]))
    passages.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = []
    seen_sentences = set()
    for item in passages:
        sentence = item[2]
        if sentence not in seen_sentences:
            seen_sentences.add(sentence)
            selected.append(item)
        if len(selected) == 3:
            break
    if not selected:
        generated = _try_generated_answer(question, history, hits, extraction_warnings, retrieval_mode,
                                          plagiarism_evidence, plagiarism_sources,
                                          model_errors=model_errors)
        if generated:
            return generated
        return {"answer": INSUFFICIENT, "sources": _assistant_sources(hits), "citations": [],
                "answer_mode": "insufficient", "ai_status": "local_extractive",
                "ai_error": model_errors[0] if model_errors else None,
                "extraction_warnings": extraction_warnings, "retrieval_mode": retrieval_mode}
    generated = _try_generated_answer(question, history, hits, extraction_warnings, retrieval_mode,
                                      plagiarism_evidence, plagiarism_sources,
                                      model_errors=model_errors)
    if generated:
        return generated
    citations = list(dict.fromkeys(item[3] for item in selected))
    answer = "\n".join(item[2] for item in selected)
    if broad_question:
        answer = summarize("\n".join(hit["text"] for hit in hits), n=4)
        if all_documents_summary:
            answer = "Extractive summary of relevant passages from your documents:\n" + answer
        else:
            answer = "Extractive summary from the selected document:\n" + answer
    return {"answer": answer, "sources": _assistant_sources(hits), "citations": citations,
            "answer_mode": "extractive", "ai_status": "local_extractive",
            "ai_error": model_errors[0] if model_errors else None,
            "extraction_warnings": extraction_warnings,
            "retrieval_mode": retrieval_mode}


def _assistant_sources(hits):
    sources, seen = [], set()
    for hit in hits:
        ref = hit["source_ref"]
        if ref in seen: continue
        seen.add(ref)
        sources.append({"ref": ref, "id": hit["fid"], "name": hit["name"],
                        "kind": hit["kind"],
                        "file_type": os.path.splitext(hit["name"] or "")[1].lstrip(".").upper() or hit["kind"],
                        "category": hit["category"],
                        "is_demo": hit.get("is_demo", False),
                        "reference": _section_reference(hit["text"]),
                        "snippet": hit["text"][:700]})
    return sources

