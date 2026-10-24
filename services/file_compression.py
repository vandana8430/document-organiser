"""Safe PDF and image compression with owner-scoped result storage."""
import io
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageOps
from pypdf import PdfReader

from database.repository import UPLOAD_DIR, q, stored_file_path, transaction
from services.document_ingestion import MAX_UPLOAD_BYTES
from services.document_processor import validate_upload_content

logger = logging.getLogger(__name__)
SUPPORTED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_TYPES = SUPPORTED_IMAGE_TYPES | {".pdf"}
MAX_IMAGE_PIXELS = 50_000_000


def _compress_pdf(data, mode="balanced", quality=85, max_dimension=0):
    try:
        import fitz
        from PIL import Image

        document = fitz.open(stream=data, filetype="pdf")
        if document.needs_pass:
            raise HTTPException(422, "Password-protected PDFs cannot be compressed.")
        if not document.page_count or document.page_count > 5000:
            raise HTTPException(422, "The PDF has no pages or exceeds the 5,000-page safety limit.")
        if mode != "lossless":
            # Re-encode oversized embedded images only; PDF text, vectors,
            # fonts, links, and page geometry remain PDF objects.
            target_dimension = max_dimension or (1400 if mode == "maximum" else 2200)
            target_quality = min(quality, 55) if mode == "maximum" else quality
            seen = set()
            for page in document:
                for image_info in page.get_images(full=True):
                    xref = image_info[0]
                    smask = image_info[1]
                    if xref in seen or smask:  # Keep separate soft masks/transparency intact.
                        continue
                    seen.add(xref)
                    extracted = document.extract_image(xref)
                    if not extracted:
                        continue
                    if extracted.get("width", 0) * extracted.get("height", 0) > MAX_IMAGE_PIXELS:
                        logger.info("Skipping unusually large PDF image xref=%s", xref)
                        continue
                    try:
                        with Image.open(io.BytesIO(extracted["image"])) as source:
                            source.load()
                            if "A" in source.getbands():
                                continue
                            source.thumbnail((target_dimension, target_dimension), Image.Resampling.LANCZOS)
                            replacement = io.BytesIO()
                            source.convert("RGB").save(replacement, "JPEG", quality=target_quality, optimize=True, progressive=True)
                        page.replace_image(xref, stream=replacement.getvalue())
                    except (KeyError, OSError, ValueError):
                        logger.info("Skipping PDF image xref=%s that could not be re-encoded", xref)
        # Remove descriptive metadata, preserving structural content and links.
        document.set_metadata({})
        try:
            document.del_xml_metadata()
        except (AttributeError, RuntimeError):
            pass
        output = io.BytesIO()
        document.save(output, garbage=4, clean=True, deflate=True,
                      deflate_images=True, deflate_fonts=True, use_objstms=1)
        result = output.getvalue()
        document.close()
        verified = fitz.open(stream=result, filetype="pdf")
        if verified.needs_pass or not verified.page_count:
            verified.close()
            raise ValueError("The optimized PDF did not pass output validation")
        verified.close()
        return result
    except HTTPException:
        raise
    except ModuleNotFoundError as error:
        if error.name in {"fitz", "pymupdf"}:
            logger.exception("PyMuPDF is required for PDF compression")
            raise HTTPException(503, "PDF compression is unavailable because PyMuPDF is missing. Install requirements.txt and restart SmartFiles.") from error
        raise
    except Exception as error:
        logger.warning("PDF compression failed (%s)", type(error).__name__)
        raise HTTPException(422, "The PDF is corrupted, unreadable, or cannot be safely optimized.") from error

def _compress_image(data, extension, quality, max_dimension, mode="balanced"):
    try:
        with Image.open(io.BytesIO(data)) as source:
            if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) != 1:
                raise HTTPException(422, "Animated images are not supported; the original remains unchanged.")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "Images must be 50 megapixels or smaller.")
            source.load()
            image = ImageOps.exif_transpose(source)
            effective_dimension = max_dimension or (1800 if mode == "maximum" else 3000 if mode == "balanced" else 0)
            if effective_dimension and mode != "lossless":
                image.thumbnail((effective_dimension, effective_dimension), Image.Resampling.LANCZOS)
            candidates = []
            if extension in {".jpg", ".jpeg", ".webp"}:
                # Try the requested setting first, then stronger lossy quality
                # levels. Keep the smallest valid candidate; size is measured
                # from its actual encoded bytes, not an estimate.
                qualities = ([quality] if mode == "lossless" else
                             sorted({quality, *[q for q in ((55, 45, 35, 25) if mode == "maximum" else (80, 70, 60, 50)) if q < quality]}, reverse=True))
                if mode == "lossless":
                    return data
                for candidate_quality in qualities:
                    output = io.BytesIO()
                    candidate_image = image
                    if extension in {".jpg", ".jpeg"}:
                        if candidate_image.mode not in {"RGB", "L"}:
                            candidate_image = candidate_image.convert("RGB")
                        candidate_image.save(output, format="JPEG", quality=candidate_quality, optimize=True, progressive=True)
                    else:
                        candidate_image.save(output, format="WEBP", quality=candidate_quality, method=6)
                    candidates.append(output.getvalue())
            else:
                candidate_image = image
                if candidate_image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                    candidate_image = candidate_image.convert("RGBA" if "A" in candidate_image.getbands() else "RGB")
                output = io.BytesIO()
                candidate_image.save(output, format="PNG", optimize=True, compress_level=9)
                candidates.append(output.getvalue())
                # Palette PNGs can be substantially smaller for diagrams and
                # screenshots. Preserve transparency and include a lossless
                # full-color candidate above as a fallback.
                if mode != "lossless" and candidate_image.mode not in {"1", "L", "LA", "P"}:
                    color_steps = (64, 32) if mode == "maximum" else (256, 128)
                    for colors in color_steps:
                        if "A" in candidate_image.getbands():
                            quantized = candidate_image.convert("RGB").quantize(
                                colors=colors, method=Image.Quantize.MEDIANCUT
                            ).convert("RGBA")
                            quantized.putalpha(candidate_image.getchannel("A"))
                        else:
                            quantized = candidate_image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
                        output = io.BytesIO()
                        quantized.save(output, format="PNG", optimize=True, compress_level=9)
                        candidates.append(output.getvalue())

            valid = []
            for candidate in candidates:
                try:
                    with Image.open(io.BytesIO(candidate)) as verified:
                        verified.verify()
                    valid.append(candidate)
                except (OSError, ValueError):
                    continue
            if not valid:
                raise ValueError("No valid optimized image candidate was produced")
            if mode == "maximum":
                return min(valid, key=len)
            # Balanced mode keeps the highest-quality candidate that achieves
            # a real reduction. Continue to lower quality only when needed.
            return next((candidate for candidate in valid if len(candidate) < len(data)), min(valid, key=len))
    except HTTPException:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        logger.warning("Image compression rejected an unreadable input (%s)", type(error).__name__)
        raise HTTPException(422, "The image is corrupted or cannot be safely compressed.") from error


def compress_and_store(uid, filename, data, quality=85, max_dimension=0, original_fid=None, mode="balanced"):
    if not data:
        raise HTTPException(400, "The selected file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Files must be 25 MB or smaller.")
    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(400, "A filename is required.")
    filename = os.path.basename(filename.replace("\\", "/")).strip()
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_TYPES:
        raise HTTPException(415, "Supported compression formats are PDF, JPG, PNG, and WebP.")
    if mode not in {"lossless", "balanced", "maximum"}:
        raise HTTPException(422, "Compression mode must be Lossless, Balanced, or Maximum compression.")
    if extension in SUPPORTED_IMAGE_TYPES and not 20 <= quality <= 95:
        raise HTTPException(422, "Image quality must be between 20 and 95.")
    if max_dimension and not 256 <= max_dimension <= 12000:
        raise HTTPException(422, "Maximum image dimension must be between 256 and 12,000 pixels.")
    try:
        validate_upload_content(data, extension)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    compressed = (_compress_pdf(data, mode, quality, max_dimension) if extension == ".pdf"
                  else _compress_image(data, extension, quality, max_dimension, mode))
    if not compressed:
        raise HTTPException(500, "The optimizer produced an empty output; no result was saved.")
    # A valid rewrite can still be larger than its source. In that case serve
    # an exact copy of the original and report zero savings, never a larger
    # file as a successful compression result.
    if len(compressed) >= len(data):
        compressed = data

    if original_fid is None:
        original_dir = UPLOAD_DIR / "compression-originals" / str(uid)
        original_dir.mkdir(parents=True, exist_ok=True)
        original_path = original_dir / f"{secrets.token_hex(16)}{extension}"
    else:
        original_path = None
    result_dir = UPLOAD_DIR / "compressed" / str(uid)
    result_dir.mkdir(parents=True, exist_ok=True)
    compressed_path = result_dir / f"{secrets.token_hex(16)}{extension}"
    saved_original = False
    saved_compressed = False
    try:
        if original_path is not None:
            with original_path.open("xb") as target:
                target.write(data)
            saved_original = True
        with compressed_path.open("xb") as target:
            target.write(compressed)
        saved_compressed = True

        if extension == ".pdf":
            check = PdfReader(str(compressed_path), strict=True)
            if check.is_encrypted or not check.pages:
                raise ValueError("The saved PDF failed validation")
        else:
            with Image.open(compressed_path) as check:
                check.verify()

        original_size = len(data)
        compressed_size = compressed_path.stat().st_size
        space_saved = max(0, original_size - compressed_size)
        percent_reduction = (space_saved / original_size * 100) if original_size else 0.0
        status = "compressed" if space_saved > 0 else "not_reduced"
        created = time.time()
        with transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO compression_history(uid,original_fid,original_name,original_path,compressed_path,"
                "file_type,original_size,compressed_size,space_saved,percent_reduction,status,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, original_fid, filename, str(original_path) if original_path else None,
                 str(compressed_path), extension.lstrip(".").upper(), original_size, compressed_size,
                 space_saved, percent_reduction, status, created),
            )
            result_id = cursor.lastrowid
    except Exception as error:
        for path, was_saved in ((compressed_path, saved_compressed), (original_path, saved_original)):
            if path is not None and was_saved:
                try:
                    path.unlink()
                except OSError:
                    logger.exception("Could not clean up failed compression output")
        if isinstance(error, HTTPException):
            raise
        logger.exception("Could not store validated compression result for uid=%s", uid)
        raise HTTPException(500, "The compression result could not be safely saved.") from error

    return {
        "id": result_id,
        "original_name": filename,
        "file_type": extension.lstrip(".").upper(),
        "original_size_bytes": original_size,
        "compressed_size_bytes": compressed_size,
        "space_saved_bytes": space_saved,
        "percent_reduction": round(percent_reduction, 2),
        "status": status,
        "message": ("Compression completed; the optimized copy is smaller." if space_saved > 0 else
                    "No smaller valid version was found at these settings. The original is available unchanged; try Maximum compression if more image quality loss is acceptable."),
        "download_url": f"/api/compression/{result_id}/raw?dl=1",
        "preview_url": f"/api/compression/{result_id}/raw",
    }


def compress_document(uid, file_id, quality, max_dimension, mode="balanced"):
    rows = q("SELECT id,name,path FROM files WHERE id=? AND uid=?", (file_id, uid))
    if not rows:
        raise HTTPException(404, "Document not found.")
    path = stored_file_path(rows[0]["path"])
    if not path.is_file():
        raise HTTPException(404, "The original document is missing from storage.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Files must be 25 MB or smaller.")
    try:
        data = path.read_bytes()
    except OSError as error:
        logger.exception("Could not read compression source uid=%s file_id=%s", uid, file_id)
        raise HTTPException(500, "The selected original could not be read.") from error
    return compress_and_store(uid, rows[0]["name"], data, quality, max_dimension, file_id, mode)


def compression_history(uid):
    rows = q(
        "SELECT id,original_fid,original_name,file_type,original_size,compressed_size,space_saved,"
        "percent_reduction,status,created FROM compression_history WHERE uid=? ORDER BY created DESC",
        (uid,),
    )
    return [dict(row) for row in rows]


def compression_result(uid, result_id):
    rows = q("SELECT * FROM compression_history WHERE id=? AND uid=?", (result_id, uid))
    if not rows:
        raise HTTPException(404, "Compression result not found.")
    return rows[0]
