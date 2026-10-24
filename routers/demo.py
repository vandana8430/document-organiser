"""Authenticated access to the fictional sample-document collection."""
from fastapi import APIRouter, Depends

from services.auth_service import me
from services.demo_documents import import_demo_documents

router = APIRouter()


@router.post("/api/demo/import")
def load_demo_documents(uid: int = Depends(me)):
    return import_demo_documents(uid)
