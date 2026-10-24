"""Search API route."""
from fastapi import APIRouter, Depends, HTTPException
from services.auth_service import me
from services.semantic_search import search_documents
router = APIRouter()

@router.get("/api/search")
def search(query: str = "", category: str = "", tag: str = "", file_type: str = "",
           date_from: float = None, date_to: float = None, uid: int = Depends(me)):
    try:
        return search_documents(query, uid, category, tag, file_type, date_from, date_to)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
