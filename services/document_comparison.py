"""Owner-scoped comparison of actual extracted document text."""
from collections import Counter
from difflib import unified_diff
import math
import os
import re
from itertools import islice

from fastapi import HTTPException

from database.repository import q, stored_file_path, transaction
from services.categorizer import keywords
from services.document_processor import extract, kind_of
from services.summarizer import summarize

MAX_COMPARE_CHARS = 250_000
TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _text_for(row):
    """Use saved extraction, retrying supported files if the saved text is blank."""
    content = (row["text"] or "").strip()
    if content:
        return content
    path = stored_file_path(row["path"])
    ext = os.path.splitext(row["name"])[1].lower()
    kind = kind_of(ext)
    if not kind or not path.is_file():
        raise HTTPException(422, f"Text is unavailable for {row['name']}; its file is missing or unsupported.")
    try:
        content, _ = extract(str(path), kind, ext)
    except Exception as error:
        if isinstance(error, ModuleNotFoundError):
            module = error.name or "a required extraction module"
            raise HTTPException(422, f"Could not extract text from {row['name']}: install the missing Python module '{module}'.")
        raise HTTPException(422, f"Could not extract text from {row['name']} ({type(error).__name__}).")
    content = (content or "")[:MAX_COMPARE_CHARS].strip()
    if not content:
        raise HTTPException(422, f"No readable text could be extracted from {row['name']}.")
    # Save a successful retry so search and the assistant can use it too.
    with transaction() as connection:
        connection.execute("update files set text=?, summary=case when summary is null or summary='' then ? else summary end where id=? and uid=?",
                           (content, summarize(content), row["id"], row["uid"]))
    return content


def _tokens(text):
    return TOKEN_RE.findall(text.lower())


def _tfidf_cosine(left, right):
    """Cosine similarity of two document TF-IDF vectors (IDF over this pair)."""
    first, second = Counter(_tokens(left)), Counter(_tokens(right))
    if not first or not second:
        return 1.0 if left.strip() == right.strip() else 0.0
    vocabulary = first.keys() | second.keys()
    vectors = []
    for counts in (first, second):
        vec = {word: (1 + math.log(count)) * (1 + math.log(2 / (1 + int(word in first) + int(word in second))))
               for word, count in counts.items()}
        vectors.append(vec)
    dot = sum(vectors[0].get(word, 0) * vectors[1].get(word, 0) for word in vocabulary)
    norm_a = math.sqrt(sum(value * value for value in vectors[0].values()))
    norm_b = math.sqrt(sum(value * value for value in vectors[1].values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _sentences(text):
    return [value.strip() for value in re.split(r"(?<=[.!?])\s+|\n+", text) if len(value.split()) >= 4]


def compare_documents(b: dict, uid: int):
    if not isinstance(b, dict) or "a" not in b or "b" not in b:
        raise HTTPException(422, "Choose two documents to compare")
    try:
        first_id, second_id = int(b["a"]), int(b["b"])
    except (TypeError, ValueError):
        raise HTTPException(422, "Document IDs must be integers")
    if first_id == second_id:
        raise HTTPException(422, "Choose two different documents")
    rows = [q("select * from files where id=? and uid=?", (file_id, uid)) for file_id in (first_id, second_id)]
    if not all(rows):
        raise HTTPException(404, "One or both documents were not found")
    a, c = rows[0][0], rows[1][0]
    text_a, text_b = _text_for(a), _text_for(c)
    # Compare bounded text to keep CPU and memory predictable for large files.
    sample_a, sample_b = text_a[:MAX_COMPARE_CHARS], text_b[:MAX_COMPARE_CHARS]
    identical = bool(a["sha"] and a["sha"] == c["sha"]) or text_a == text_b
    if identical:
        similarity = 1.0
    else:
        similarity = _tfidf_cosine(sample_a, sample_b)
    topics_a = set(keywords(sample_a, 20))
    topics_b = set(keywords(sample_b, 20))
    common_sentences = []
    b_sentences = {s.casefold(): s for s in _sentences(sample_b)}
    for sentence in _sentences(sample_a):
        if sentence.casefold() in b_sentences and sentence not in common_sentences:
            common_sentences.append(sentence)
        if len(common_sentences) == 5:
            break
    diff_rows = list(islice(unified_diff(sample_a.splitlines(), sample_b.splitlines(),
                                         fromfile=a["name"], tofile=c["name"], lineterm="", n=2), 301))
    diff_truncated = len(diff_rows) > 300
    text_truncated = len(text_a) > MAX_COMPARE_CHARS or len(text_b) > MAX_COMPARE_CHARS
    ext_a = os.path.splitext(a["name"])[1].lstrip(".").upper() or a["kind"].upper()
    ext_b = os.path.splitext(c["name"])[1].lstrip(".").upper() or c["kind"].upper()
    return {
        "a": a["name"], "b": c["name"], "a_kind": ext_a, "b_kind": ext_b,
        "similarity": round(similarity, 4),
        "similarity_sampled": text_truncated,
        "similarity_method": "TF-IDF cosine similarity",
        "similarity_explanation": "The percentage is cosine similarity between word-frequency vectors weighted by inverse document frequency across these two documents. It measures shared vocabulary and frequency, not meaning; identical file hashes score 100%.",
        "identical": identical,
        "shared": sorted(topics_a & topics_b), "only_a": sorted(topics_a - topics_b), "only_b": sorted(topics_b - topics_a),
        "common_text": common_sentences,
        "text_truncated": text_truncated,
        "analysis": "The report compares extracted text. Exact repeated sentences are shown under common text; the diff shows line-level changes.",
        "diff": diff_rows[:300], "truncated": diff_truncated,
    }
