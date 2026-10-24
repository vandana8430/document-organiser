"""Shared validation, extraction, and persistence pipeline for uploaded files."""
import hashlib
import logging
import os
import secrets
import sqlite3
import time
import unicodedata

import numpy as np
from fastapi import HTTPException

from database.repository import UPLOAD_DIR, q, stored_file_path, transaction
from services.categorizer import classify, keywords
from services.document_processor import (
    MAX_EXTRACTED_CHARS, blob, embed, embedding_backend, extract, image_info,
    kind_of, make_chunks, validate_upload_content,
)
from services.summarizer import summarize

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
logger = logging.getLogger(__name__)


def ingest_document(filename, data, uid, demo_key=None):
    """Validate, process and store bytes through SmartFiles' common pipeline."""
    supplied_name = unicodedata.normalize(
        "NFC", (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    )
    original_name = "".join(
        char for char in supplied_name if unicodedata.category(char) not in {"Cc", "Cf"}
    )
    if not original_name or original_name in {".", ".."}:
        raise HTTPException(400, "Please upload a file with a valid filename")
    if len(original_name.encode("utf-8")) > 255:
        raise HTTPException(400, "Filenames must be 255 UTF-8 bytes or shorter")

    ext = os.path.splitext(original_name)[1].lower()
    if ext == ".doc":
        raise HTTPException(400, "Legacy .doc files are not supported safely. Save the document as .docx and upload it again.")
    if ext == ".zip":
        raise HTTPException(400, "ZIP archives are not supported. Extract the document you need and upload that file instead.")
    kind = kind_of(ext)
    if not kind:
        raise HTTPException(400, f"Unsupported file type: {ext or 'no extension'}")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Files must be 25 MB or smaller")
    if not data:
        raise HTTPException(400, "The uploaded file is empty")

    if demo_key:
        if not demo_key.startswith("samples-v1:"):
            raise HTTPException(400, "Invalid demo document key")
        existing = q("select id,name from files where uid=? and demo_key=?", (uid, demo_key))
        if existing:
            return {"id": existing[0]["id"], "already_imported": True,
                    "name": existing[0]["name"], "is_demo": True}

    try:
        validate_upload_content(data, ext)
    except ValueError as error:
        raise HTTPException(400, str(error))

    sha = hashlib.sha256(data).hexdigest()
    storage_dir = UPLOAD_DIR / "demo" / str(uid) if demo_key else UPLOAD_DIR
    storage_dir.mkdir(parents=True, exist_ok=True)
    path = None
    for _ in range(3):
        candidate = str(storage_dir / f"{secrets.token_hex(16)}{ext}")
        try:
            with open(candidate, "xb") as stored_file:
                stored_file.write(data)
            path = candidate
            break
        except FileExistsError:
            continue
    if path is None:
        raise HTTPException(503, "Could not allocate a unique storage name; please retry the upload")

    try:
        processing_warning = ""
        extraction_status = "unknown"
        extraction_error = ""
        cached = q("select text from files where uid=? and sha=? and text is not null and text!='' "
                   "and coalesce(extraction_status,'indexed') not in ('failed','no_text') order by id limit 1",
                   (uid, sha))
        extraction_reused = bool(cached)
        try:
            if cached:
                text, segs = cached[0]["text"], None
            else:
                text, segs = extract(path, kind, ext)
        except Exception as error:
            text, segs = "", None
            extraction_status = "failed"
            extraction_error = f"{type(error).__name__}: {error}"
            logger.exception("Text extraction failed for uid=%s file_type=%s", uid, ext)
            if isinstance(error, ModuleNotFoundError):
                missing_module = error.name or "an unavailable Python module"
                processing_warning = (
                    f"Text extraction failed ({type(error).__name__}: {error}). "
                    f"Install the missing Python module '{missing_module}'. "
                    "The file is saved and can still be downloaded."
                )
            elif isinstance(error, ImportError):
                processing_warning = (
                    f"Text extraction failed (ImportError: {error}). "
                    "Check the package installation and active Python environment. "
                    "The file is saved and can still be downloaded."
                )
            elif type(error).__name__ == "TesseractNotFoundError":
                processing_warning = (
                    "OCR is not configured: install the Tesseract executable and restart SmartFiles. "
                    "The file is saved and can still be downloaded."
                )
                extraction_error = processing_warning
            else:
                processing_warning = (
                    f"Text extraction failed ({type(error).__name__}: {error}); "
                    "the file is saved and can still be downloaded."
                )
        if len(text) > MAX_EXTRACTED_CHARS:
            text = text[:MAX_EXTRACTED_CHARS]
            limit_warning = (
                "Text extraction was limited to the first 2,000,000 characters for safe indexing. "
                "The full original file is available to download."
            )
            processing_warning = (processing_warning + " " + limit_warning).strip()
        if not text.strip() and not processing_warning:
            processing_warning = (
                "No readable text was extracted; the file is saved and can still be opened or downloaded."
            )

        chunks = make_chunks(text, segs)
        if text.strip():
            extraction_status = "indexed" if chunks else "extracted"
        elif extraction_status != "failed":
            extraction_status = "no_text"
            extraction_error = processing_warning
        vectors = embed([chunk[1] for chunk in chunks]) if chunks else np.zeros((0, 384), np.float32)
        mean_vector = vectors.mean(0) if len(vectors) else embed([original_name])[0]
        vector_backend = embedding_backend()
        tags = keywords(text, filename=original_name)
        if demo_key:
            tags = ["Sample", "Demo"] + tags
        clip_vector = None
        if kind == "image":
            try:
                clip_vector, visual_tags = image_info(path)
                tags = visual_tags + tags
            except Exception:
                pass
        duplicate = q("select name from files where uid=? and sha=?", (uid, sha))
        if duplicate:
            tags.append("duplicate")

        with transaction() as connection:
            cursor = connection.execute(
                "insert into files(uid,name,path,kind,sha,category,tags,summary,text,emb,created,clip,embedder,demo_key,extraction_status,extraction_error) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, original_name, path, kind, sha,
                 classify(text, kind, original_name, {"file_type": ext, "kind": kind}), ",".join(tags),
                 summarize(text) if text else "", text, blob(mean_vector), time.time(),
                 blob(clip_vector) if clip_vector is not None else None, vector_backend, demo_key,
                 extraction_status, extraction_error or None),
            )
            file_id = cursor.lastrowid
            connection.executemany(
                "insert into chunks(fid,uid,text,start,emb,embedder) values(?,?,?,?,?,?)",
                [(file_id, uid, chunk_text, start, blob(vector), vector_backend)
                 for (start, chunk_text), vector in zip(chunks, vectors)],
            )
    except sqlite3.IntegrityError:
        try:
            os.remove(path)
        except OSError:
            pass
        if demo_key:
            existing = q("select id,name from files where uid=? and demo_key=?", (uid, demo_key))
            if existing:
                return {"id": existing[0]["id"], "already_imported": True,
                        "name": existing[0]["name"], "is_demo": True}
        raise
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    return {"id": file_id, "duplicate_of": duplicate[0]["name"] if duplicate else None,
            "text_extracted": bool(text.strip()), "processing_warning": processing_warning,
            "extraction_status": extraction_status, "extraction_error": extraction_error or None,
            "extraction_reused": extraction_reused,
            "is_demo": bool(demo_key), "name": original_name}


def reprocess_documents(uid, file_id=None):
    """Re-extract this user's original files and atomically replace their text index.

    A failed extraction leaves the existing file row and chunks untouched. When
    called without an ID, only rows missing extracted text or searchable chunks
    are retried.
    """
    sql = "select * from files where uid=?"
    args = [uid]
    if file_id is not None:
        sql += " and id=?"
        args.append(file_id)
    sql += " order by created,id"
    rows = q(sql, tuple(args))
    if file_id is not None and not rows:
        raise HTTPException(404, "Document not found")

    result = {"processed": [], "errors": [], "skipped": []}
    for row in rows:
        name = row["name"]
        try:
            existing_chunks = q("select 1 from chunks where fid=? and uid=? limit 1", (row["id"], uid))
            if file_id is None and (row["text"] or "").strip() and existing_chunks:
                result["skipped"].append({"id": row["id"], "name": name, "reason": "Already indexed"})
                continue

            path = stored_file_path(row["path"])
            if not path.is_file():
                raise ValueError("Original file is missing from storage")
            ext = os.path.splitext(name)[1].lower()
            kind = kind_of(ext)
            if not kind:
                raise ValueError("This file type does not support text extraction")

            text, segments = extract(str(path), kind, ext)
            text = (text or "")[:MAX_EXTRACTED_CHARS].strip()
            if not text:
                raise ValueError("No readable text was extracted; the previous index was preserved")

            chunks = make_chunks(text, segments)
            if not chunks:
                raise ValueError("Text was extracted but no searchable chunks could be created")
            vectors = embed([chunk_text for _, chunk_text in chunks])
            mean_vector = vectors.mean(0) if len(vectors) else embed([name])[0]
            backend = embedding_backend()
            category = classify(text, row["kind"], name, {"file_type": ext, "kind": row["kind"]})
            tags = keywords(text, filename=name)
            if row["demo_key"]:
                tags = ["Sample", "Demo"] + tags

            with transaction() as connection:
                # Replace the index only after extraction and vector creation succeeded.
                connection.execute("delete from chunks where fid=? and uid=?", (row["id"], uid))
                connection.executemany(
                    "insert into chunks(fid,uid,text,start,emb,embedder) values(?,?,?,?,?,?)",
                    [(row["id"], uid, chunk_text, start, blob(vector), backend)
                     for (start, chunk_text), vector in zip(chunks, vectors)],
                )
                connection.execute(
                    "update files set text=?,summary=?,category=?,tags=?,emb=?,embedder=?,extraction_status='indexed',extraction_error=NULL where id=? and uid=?",
                    (text, summarize(text), category, ",".join(tags), blob(mean_vector), backend, row["id"], uid),
                )
            result["processed"].append({"id": row["id"], "name": name, "characters": len(text),
                                        "chunks": len(chunks)})
        except Exception as error:
            logger.exception("Document reprocessing failed for uid=%s file_id=%s",
                             uid, row["id"])
            error_message = f"{type(error).__name__}: {error}"
            if not (row["text"] or "").strip():
                with transaction() as connection:
                    connection.execute(
                        "update files set extraction_status='failed',extraction_error=? where id=? and uid=?",
                        (error_message, row["id"], uid),
                    )
            result["errors"].append({"id": row["id"], "name": name,
                                     "error": error_message})
    result["counts"] = {key: len(result[key]) for key in ("processed", "errors", "skipped")}
    return result
