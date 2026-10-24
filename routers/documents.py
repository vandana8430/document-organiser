"""Document upload, listing, preview, deletion, and dashboard endpoints."""
import logging
import os, time
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from services.auth_service import me
from services.document_processor import unblob
from services.document_ingestion import MAX_UPLOAD_BYTES, ingest_document, reprocess_documents
from services.categorizer import CATEGORIES
from database.repository import q, stored_file_path
from services.storage_analytics import storage_snapshot
router = APIRouter()
logger = logging.getLogger(__name__)

def row(r, full=False):
    d = {k: r[k] for k in ("id", "name", "kind", "category", "summary", "created", "opened")}
    extension = os.path.splitext(r["name"] or "")[1].lower()
    d["file_type"] = extension.lstrip(".").upper() or "Unknown"
    d["is_demo"] = bool(r["demo_key"])
    d["tags"] = r["tags"].split(",") if r["tags"] else []
    d["has_text"] = bool((r["text"] or "").strip())
    d["extraction_status"] = r["extraction_status"] or ("indexed" if d["has_text"] else "needs_attention")
    d["extraction_error"] = r["extraction_error"] or ""
    try: d["size_bytes"] = os.path.getsize(stored_file_path(r["path"]))
    except OSError: d["size_bytes"] = None
    if full: d["text"] = (r["text"] or "")[:5000]
    return d

@router.post("/api/upload")
async def upload(file: UploadFile = File(...), uid: int = Depends(me)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return ingest_document(file.filename, data, uid)
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Document upload failed for uid=%s", uid)
        raise HTTPException(500, "The upload could not be processed. Check the server terminal for the error details.") from error

@router.post("/api/reprocess")
def reprocess(file_id: int = None, uid: int = Depends(me)):
    """Repair this user's missing text/chunk index from original stored files."""
    return reprocess_documents(uid, file_id)

@router.get("/api/files")
def files(uid: int = Depends(me), category: str = "", kind: str = ""):
    r = q("select * from files where uid=? order by created desc", (uid,))
    return [row(x) for x in r if (not category or x["category"] == category) and (not kind or x["kind"] == kind)]

@router.get("/api/files/{fid}")
def detail(fid: int, uid: int = Depends(me)):
    r = q("select * from files where id=? and uid=?", (fid, uid))
    if not r: raise HTTPException(404)
    if not stored_file_path(r[0]["path"]).is_file():
        raise HTTPException(404, "Document file is missing from storage")
    q("update files set opened=opened+1, last=? where id=?", (time.time(), fid))
    others = q("select * from files where uid=? and id!=? and embedder is ?",
               (uid, fid, r[0]["embedder"]))
    v = unblob(r[0]["emb"])
    compatible = [o for o in others if unblob(o["emb"]).shape == v.shape]
    rel = sorted(compatible, key=lambda o: -float(unblob(o["emb"]) @ v))[:3]
    return {**row(r[0], True), "related": [{"id": o["id"], "name": o["name"]} for o in rel]}

@router.patch("/api/files/{fid}/category")
def set_category(fid: int, body: dict, uid: int = Depends(me)):
    category = body.get("category") if isinstance(body, dict) else None
    if category not in CATEGORIES:
        raise HTTPException(400, "Choose one of the available document categories")
    if not q("select id from files where id=? and uid=?", (fid, uid)):
        raise HTTPException(404, "Document not found")
    q("update files set category=? where id=? and uid=?", (category, fid, uid))
    return {"id": fid, "category": category}

@router.get("/api/files/{fid}/raw")
def raw(fid: int, uid: int = Depends(me), dl: int = 0):
    r = q("select * from files where id=? and uid=?", (fid, uid))
    if not r: raise HTTPException(404)
    path = stored_file_path(r[0]["path"])
    if not path.is_file(): raise HTTPException(404, "Document file is missing from storage")
    return FileResponse(str(path), filename=r[0]["name"] if dl else None, content_disposition_type="attachment" if dl else "inline")

@router.delete("/api/files/{fid}")
def delete(fid: int, uid: int = Depends(me)):
    q("update compression_history set original_fid=NULL where original_fid=? and uid=?", (fid, uid))
    q("delete from plagiarism_scans where fid=? and uid=?", (fid, uid))
    for r in q("select path from files where id=? and uid=?", (fid, uid)):
        path = stored_file_path(r[0])
        if path.is_file(): path.unlink()
    q("delete from chunks where fid=? and uid=?", (fid, uid)); q("delete from files where id=? and uid=?", (fid, uid)); return {"ok": 1}

@router.get("/api/dashboard")
def dashboard(uid: int = Depends(me), category: str = "", file_type: str = "",
              date_from: float = None, date_to: float = None):
    snapshot = storage_snapshot(uid, category, file_type, date_from, date_to)
    r = snapshot["files"]
    return {key: value for key, value in snapshot.items() if key not in {"files", "exact_duplicate_files", "near_duplicates", "largest_documents", "compressible_documents", "compression_history"}} | {
        **snapshot["documents"],
        "category_options": list(CATEGORIES),
        "recent": [row(x) for x in sorted(r, key=lambda x: -x["created"])[:5]],
        "frequent": [row(x) for x in sorted(r, key=lambda x: -x["opened"]) if x["opened"]][:5],
    }
