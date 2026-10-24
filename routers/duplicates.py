"""Duplicate Finder API route."""
from fastapi import APIRouter, Depends
from services.auth_service import me
from services.duplicate_detector import find_duplicates
router = APIRouter()

@router.get("/api/duplicates")
def duplicates(uid: int = Depends(me)):
    return find_duplicates(uid)
