"""Document search, including lexical ranking and optional MiniLM scoring."""
import re
import numpy as np
from database.repository import q
from services.document_processor import embed, embedding_model_state, unblob

# ---------- Semantic search ----------
def search_documents(query: str, uid: int, category: str = "", tag: str = "",
                     file_type: str = "", date_from: float = None, date_to: float = None):
    query = (query or "").strip()
    category, tag = category.strip().casefold(), tag.strip().casefold()
    file_type = file_type.strip().lower().lstrip(".")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from must be earlier than or equal to date_to")
    methods = ["filename", "category", "tags", "summary", "extracted_text"]
    if not (query or category or tag or file_type or date_from is not None or date_to is not None):
        return {"query": query, "results": [], "search_methods": methods, "semantic_status": "not_requested"}

    # Lexical search always works, even if sentence-transformers is absent.
    # It looks at the filename and stored metadata as well as extracted text.
    terms = list(dict.fromkeys(re.findall(r"\w+", query.lower())))
    files_by_id = {r["id"]: r for r in q(
        "select id,name,kind,category,tags,summary,text,created,demo_key from files where uid=?", (uid,))}
    files_by_id = {fid: item for fid, item in files_by_id.items()
                   if (not category or category in (item["category"] or "").casefold())
                   and (not tag or tag in [v.strip().casefold() for v in (item["tags"] or "").split(",")])
                   and (not file_type or (item["name"] or "").lower().endswith("." + file_type))
                   and (date_from is None or float(item["created"] or 0) >= date_from)
                   and (date_to is None or float(item["created"] or 0) <= date_to)}
    lexical = {}
    phrase = query.lower()
    for fid, item in files_by_id.items():
        name = (item["name"] or "").lower()
        category = (item["category"] or "").lower()
        tags = (item["tags"] or "").lower()
        summary = (item["summary"] or "").lower()
        content = (item["text"] or "").lower()
        score = 0.0
        matched = []
        for term in terms:
            weight = 4 if term in name else 3 if term in category or term in tags else 2 if term in summary else 1 if term in content else 0
            if weight:
                score += weight
                matched.append(term)
        score = min(1.0, score / max(1, 4 * len(terms)))
        if phrase and (phrase in name or phrase in category or phrase in tags or phrase in summary or phrase in content):
            score = min(1.0, score + 0.2)
        if score or not terms:
            lexical[fid] = {"score": score, "terms": matched}

    # Compare stored vectors only when the real MiniLM model is available.
    # The hashed bag-of-words fallback is deliberately never labeled semantic.
    chunks = q("select c.fid,c.text,c.emb,c.embedder from chunks c join files f on f.id=c.fid "
               "where c.uid=? and f.uid=?", (uid, uid))
    chunks = [chunk for chunk in chunks if chunk["fid"] in files_by_id]
    semantic = {}
    semantic_status = "no_compatible_embeddings"
    compatible_chunks = [chunk for chunk in chunks if chunk["embedder"] == "minilm"]
    if query and compatible_chunks:
        if embedding_model_state() is False:
            semantic_status = "model_unavailable"
        else:
            try:
                query_vector = embed([query])[0]
                if embedding_model_state() is False:
                    semantic_status = "model_unavailable"
                else:
                    vectors = np.stack([unblob(c["emb"]) for c in compatible_chunks])
                    scores = vectors @ query_vector
                    for i, score in enumerate(scores):
                        chunk = compatible_chunks[i]
                        fid = chunk["fid"]
                        score = float(score)
                        if fid not in semantic or score > semantic[fid]["score"]:
                            semantic[fid] = {"score": score, "snippet": chunk["text"] or ""}
                    semantic_status = "available"
            except Exception:
                semantic_status = "model_unavailable"
                semantic = {}

    results = []
    for fid, item in files_by_id.items():
        lex = lexical.get(fid, {"score": 0.0, "terms": []})
        sem = semantic.get(fid)
        semantic_score = sem["score"] if sem else 0.0
        semantic_hit = sem if sem and semantic_score >= 0.22 else None
        filter_only_match = not terms and bool(category or tag or file_type or date_from is not None or date_to is not None)
        if not lex["score"] and not semantic_hit and not filter_only_match:
            continue

        source = item["text"] or ""
        term_positions = [source.lower().find(term) for term in lex["terms"] if term in source.lower()]
        if term_positions:
            start = max(0, min(term_positions) - 85)
            snippet = source[start:start + 240].strip()
            if start:
                snippet = "…" + snippet
            if start + 240 < len(source):
                snippet += "…"
        elif semantic_hit and semantic_hit["snippet"]:
            snippet = semantic_hit["snippet"][:240]
        elif item["summary"]:
            snippet = item["summary"][:240]
        elif filter_only_match:
            snippet = "Matches the selected document filters."
        elif lex["terms"]:
            snippet = "Filename or document metadata match: " + (item["name"] or "")
        else:
            snippet = "Relevant passage found in the document."

        match_type = ("filter" if filter_only_match else
                      "semantic + keyword" if semantic_hit and lex["score"] else "semantic" if semantic_hit else "keyword")
        combined = max(lex["score"], min(1.0, max(0.0, semantic_score)) if semantic_hit else 0.0)
        if semantic_hit and lex["score"]:
            combined = min(1.0, combined + 0.05)
        results.append({"id": fid, "name": item["name"], "kind": item["kind"],
                        "category": item["category"], "score": round(combined, 3),
                        "match_type": match_type, "snippet": snippet,
                        "created": item["created"], "is_demo": bool(item["demo_key"])})

    results.sort(key=lambda result: (result["score"], result["created"] or 0), reverse=True)
    search_methods = methods + (["MiniLM semantic embeddings"] if semantic_status == "available" else [])
    return {"query": query, "results": results[:30], "search_methods": search_methods,
            "semantic_status": semantic_status}

