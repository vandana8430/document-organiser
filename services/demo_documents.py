"""Per-user, idempotent import of the checked-in fictional demo collection."""
import os
from pathlib import Path

from fastapi import HTTPException

from database.connection import PROJECT_ROOT
from database.repository import q
from services.document_ingestion import MAX_UPLOAD_BYTES, ingest_document
from services.document_processor import kind_of

SAMPLE_ROOT = PROJECT_ROOT / "sample_documents"


def import_demo_documents(uid):
    if not SAMPLE_ROOT.is_dir():
        raise HTTPException(503, "The sample_documents collection is not installed")

    root = SAMPLE_ROOT.resolve()
    samples = []
    for path in sorted(SAMPLE_ROOT.rglob("*")):
        if path.is_symlink() or not path.is_file() or not kind_of(path.suffix.lower()):
            continue
        resolved = path.resolve()
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            continue
        samples.append(path)

    if not samples:
        raise HTTPException(503, "No supported sample documents were found")

    imported, skipped, errors, warnings = 0, 0, [], []
    for path in samples:
        relative_name = path.relative_to(SAMPLE_ROOT).as_posix()
        demo_key = "samples-v1:" + relative_name
        prior = q("select id from files where uid=? and demo_key=?", (uid, demo_key))
        if prior:
            skipped += 1
            continue
        try:
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                errors.append({"name": path.name, "message": "Sample files must be 25 MB or smaller"})
                continue
            data = path.read_bytes()
            result = ingest_document(path.name, data, uid, demo_key=demo_key)
            if result.get("already_imported"):
                skipped += 1
            else:
                imported += 1
                if result.get("processing_warning"):
                    warnings.append({"name": path.name, "message": result["processing_warning"]})
        except HTTPException as error:
            errors.append({"name": path.name, "message": str(error.detail)})
        except OSError as error:
            errors.append({"name": path.name, "message": f"Could not read sample file ({type(error).__name__})"})

    return {"total": len(samples), "imported": imported, "skipped": skipped,
            "errors": errors, "warnings": warnings}
