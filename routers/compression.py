"""Authenticated compression and storage optimization API."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database.repository import q, stored_file_path
from services.auth_service import me
from services.file_compression import (
    compress_and_store,
    compress_document,
    compression_history,
    compression_result,
)
from services.storage_analytics import optimizer_suggestions

router = APIRouter()


@router.post("/api/compress")
async def compress(
    file: UploadFile = File(None),
    file_id: int = Form(None),
    quality: int = Form(85),
    max_dimension: int = Form(0),
    mode: str = Form("balanced"),
    uid: int = Depends(me),
):
    if (file is None) == (file_id is None):
        raise HTTPException(422, "Choose one existing document or upload one file to compress.")
    if file_id is not None:
        return compress_document(uid, file_id, quality, max_dimension, mode)
    data = await file.read(25 * 1024 * 1024 + 1)
    return compress_and_store(uid, file.filename or "", data, quality, max_dimension, mode=mode)


@router.get("/api/compression/history")
def history(uid: int = Depends(me)):
    return compression_history(uid)


@router.get("/api/compression/{result_id}/raw")
def compressed_raw(result_id: int, uid: int = Depends(me), dl: int = 0):
    result = compression_result(uid, result_id)
    path = stored_file_path(result["compressed_path"])
    if not path.is_file():
        raise HTTPException(404, "The compressed result is missing from storage.")
    filename = f"compressed-{result['original_name']}"
    return FileResponse(
        str(path), filename=filename if dl else None,
        content_disposition_type="attachment" if dl else "inline",
    )


@router.get("/api/compression/{result_id}/original")
def original_raw(result_id: int, uid: int = Depends(me)):
    result = compression_result(uid, result_id)
    if result["original_fid"] is not None:
        rows = q("SELECT path FROM files WHERE id=? AND uid=?", (result["original_fid"], uid))
        if not rows:
            raise HTTPException(404, "The source document is no longer in your workspace.")
        path = stored_file_path(rows[0]["path"])
    else:
        if not result["original_path"]:
            raise HTTPException(404, "The original file is no longer available.")
        path = stored_file_path(result["original_path"])
    if not path.is_file():
        raise HTTPException(404, "The original file is missing from storage.")
    return FileResponse(str(path), filename=result["original_name"], content_disposition_type="attachment")


@router.get("/api/storage-optimizer")
def optimizer(
    category: str = "",
    file_type: str = "",
    date_from: float = None,
    date_to: float = None,
    uid: int = Depends(me),
):
    return optimizer_suggestions(uid, category, file_type, date_from, date_to)
