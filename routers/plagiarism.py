"""Authenticated plagiarism scan and report endpoints."""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from reportlab.pdfbase.pdfdoc import PDFError
from reportlab.platypus.doctemplate import LayoutError

from database.repository import q
from services.auth_service import me
from services.plagiarism import compare_reports, create_scan, get_report, list_reports, run_scan
from services.plagiarism_pdf import build_plagiarism_pdf

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/plagiarism/scans", status_code=202)
def start_scan(body: dict, background_tasks: BackgroundTasks, uid: int = Depends(me)):
    if not isinstance(body, dict):
        raise HTTPException(422, "Choose a document to scan.")
    file_id = body.get("file_id")
    online_requested = body.get("online_search", False)
    if not isinstance(online_requested, bool):
        raise HTTPException(422, "Online source discovery must be explicitly enabled or disabled.")
    online_provider = body.get("online_provider", "quetext")
    if not isinstance(online_provider, str) or online_provider not in ("quetext", "academic"):
        raise HTTPException(422, "Choose a supported online source provider.")
    scan = create_scan(uid, file_id, online_requested, online_provider)
    if scan["reused"]:
        return scan
    background_tasks.add_task(
        run_scan, scan["scan_id"], uid, file_id, online_requested,
        scan.get("resume_report_id"), online_provider,
    )
    return scan


@router.get("/api/plagiarism/reports")
def reports(
    file_id: int = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    uid: int = Depends(me),
):
    if file_id is not None:
        owned = q("SELECT id FROM files WHERE id=? AND uid=?", (file_id, uid))
        if not owned:
            raise HTTPException(404, "Document not found.")
    return list_reports(uid, file_id, limit, offset)


@router.get("/api/plagiarism/comparisons/{earlier_scan_id}/{later_scan_id}")
def comparison(earlier_scan_id: int, later_scan_id: int, uid: int = Depends(me)):
    return compare_reports(uid, earlier_scan_id, later_scan_id)


@router.get("/api/plagiarism/reports/{scan_id}")
def report(scan_id: int, uid: int = Depends(me)):
    return get_report(uid, scan_id)


@router.get("/api/plagiarism/reports/{scan_id}/pdf")
def report_pdf(scan_id: int, uid: int = Depends(me)):
    saved_report = get_report(uid, scan_id)
    try:
        pdf = build_plagiarism_pdf(saved_report)
    except (OSError, UnicodeError, ValueError, PDFError, LayoutError) as error:
        logger.exception("Plagiarism PDF generation failed for scan_id=%s", scan_id)
        raise HTTPException(
            500,
            "The PDF report could not be generated. Refresh the saved report and try again.",
        ) from error
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="SmartFiles_plagiarism_report_{scan_id}.pdf"',
            "Cache-Control": "no-store",
        },
    )
