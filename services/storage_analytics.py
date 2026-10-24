"""Owner-scoped dashboard measurements and storage optimization suggestions."""
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from database.repository import q, stored_file_path
from services.duplicate_detector import find_duplicates
from services.file_compression import SUPPORTED_TYPES


def _within_filters(row, category, file_type, date_from, date_to):
    if category and row["category"] != category:
        return False
    extension = Path(row["name"] or "").suffix.lower()
    if file_type and extension != file_type:
        return False
    created = float(row["created"] or 0)
    if date_from is not None and created < date_from:
        return False
    if date_to is not None and created > date_to:
        return False
    return True


def _size(row):
    try:
        return stored_file_path(row["path"]).stat().st_size
    except OSError:
        return None


def storage_snapshot(uid, category="", file_type="", date_from=None, date_to=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "The start date must not be later than the end date.")
    category = category.strip()
    file_type = file_type.strip().lower()
    if file_type and not file_type.startswith("."):
        file_type = "." + file_type

    all_files = q("SELECT * FROM files WHERE uid=? ORDER BY created DESC", (uid,))
    selected = [item for item in all_files if _within_filters(item, category, file_type, date_from, date_to)]
    selected_ids = {item["id"] for item in selected}
    extensions = Counter(Path(item["name"] or "").suffix.lower() for item in selected)
    categories = Counter(item["category"] or "Uncategorized" for item in selected)
    statuses = Counter(
        item["extraction_status"] or ("indexed" if (item["text"] or "").strip() else "needs_attention")
        for item in selected
    )

    now = datetime.now()
    months = []
    for offset in range(11, -1, -1):
        month_index = now.month - offset
        year = now.year + (month_index - 1) // 12
        month = (month_index - 1) % 12 + 1
        months.append(f"{year:04d}-{month:02d}")
    month_counts = Counter(
        datetime.fromtimestamp(float(item["created"] or 0)).strftime("%Y-%m")
        for item in selected
    )
    monthly_uploads = [{"month": month, "count": month_counts[month]} for month in months]

    sized = []
    missing_originals = 0
    original_storage_bytes = 0
    for item in selected:
        size = _size(item)
        if size is None:
            missing_originals += 1
            continue
        original_storage_bytes += size
        sized.append({
            "id": item["id"], "name": item["name"], "kind": item["kind"],
            "category": item["category"], "size_bytes": size,
            "file_type": Path(item["name"] or "").suffix.lstrip(".").upper() or "Unknown",
            "can_compress": Path(item["name"] or "").suffix.lower() in SUPPORTED_TYPES,
        })
    sized.sort(key=lambda item: item["size_bytes"], reverse=True)

    history = q(
        "SELECT id,original_fid,original_name,original_path,compressed_path,file_type,original_size,"
        "compressed_size,space_saved,percent_reduction,status,created "
        "FROM compression_history WHERE uid=? ORDER BY created DESC", (uid,)
    )
    measured = []
    for item in history:
        ext = Path(item["original_name"] or "").suffix.lower()
        if date_from is not None and float(item["created"] or 0) < date_from:
            continue
        if date_to is not None and float(item["created"] or 0) > date_to:
            continue
        if file_type and ext != file_type:
            continue
        if item["original_fid"] is not None and item["original_fid"] not in selected_ids:
            continue
        if item["original_fid"] is None and category:
            continue
        compressed_path = stored_file_path(item["compressed_path"])
        try:
            compressed_size = compressed_path.stat().st_size
        except OSError:
            continue
        measured.append((item, compressed_size))
        if item["original_fid"] is None and item["original_path"]:
            try:
                original_storage_bytes += stored_file_path(item["original_path"]).stat().st_size
            except OSError:
                pass

    compressed_storage_bytes = sum(size for _, size in measured)
    confirmed_saved_bytes = sum(max(0, int(item["space_saved"] or 0)) for item, _ in measured)

    duplicate_data = find_duplicates(uid, include_images=False)
    exact = [
        {"files": [file for file in group["files"] if file["id"] in selected_ids]}
        for group in duplicate_data["exact"]
    ]
    exact = [group for group in exact if len(group["files"]) > 1]
    near = [
        pair for pair in duplicate_data["near_duplicates"]
        if all(file["id"] in selected_ids for file in pair["files"])
    ]

    return {
        "files": selected,
        "total": len(selected),
        "demo_total": sum(bool(item["demo_key"]) for item in selected),
        "documents": {
            "document": sum(item["kind"] == "document" for item in selected),
            "image": sum(item["kind"] == "image" for item in selected),
            "video": sum(item["kind"] == "video" for item in selected),
        },
        "extensions": dict(extensions),
        "available_file_types": sorted({
            Path(item["name"] or "").suffix.lower()
            for item in all_files if Path(item["name"] or "").suffix
        }),
        "categories": dict(categories),
        "processing": {
            "indexed": statuses["indexed"],
            "extracted": statuses["extracted"],
            "failed": statuses["failed"],
            "needs_attention": sum(
                statuses[key] for key in ("needs_attention", "no_text", "pending", "processing", "unknown")
            ),
        },
        "monthly_uploads": monthly_uploads,
        "storage": {
            "original_bytes": original_storage_bytes,
            "compressed_bytes": compressed_storage_bytes,
            "confirmed_saved_bytes": confirmed_saved_bytes,
            "missing_original_files": missing_originals,
        },
        "compression_history": [
            {
                "id": item["id"], "original_fid": item["original_fid"],
                "original_name": item["original_name"], "file_type": item["file_type"],
                "original_size": item["original_size"], "compressed_size": size,
                "space_saved": item["space_saved"],
                "percent_reduction": item["percent_reduction"],
                "status": item["status"], "created": item["created"],
            }
            for item, size in measured
        ],
        "duplicates": {
            "exact_groups": len(exact),
            "potential_near_pairs": len(near),
        },
        "exact_duplicate_files": exact,
        "near_duplicates": near,
        "largest_documents": sized[:10],
        "compressible_documents": [item for item in sized if item["can_compress"]],
        "category_options": sorted({item["category"] for item in all_files if item["category"]}),
    }


def optimizer_suggestions(uid, category="", file_type="", date_from=None, date_to=None):
    snapshot = storage_snapshot(uid, category, file_type, date_from, date_to)
    return {
        "largest_documents": snapshot["largest_documents"],
        "compressible_documents": snapshot["compressible_documents"],
        "exact_duplicates": snapshot["exact_duplicate_files"],
        "potential_near_duplicates": snapshot["near_duplicates"],
        "compression_history": snapshot["compression_history"],
        "storage": snapshot["storage"],
        "estimated_savings_bytes": None,
        "note": "Only confirmed savings from completed compression runs are counted. Possible duplicate removal savings are not estimated.",
    }
