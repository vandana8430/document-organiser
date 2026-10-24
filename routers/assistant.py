"""AI Assistant and comparison API routes."""
from fastapi import APIRouter, Depends
from services.auth_service import me
from services.rag_assistant import answer_question
from services.llm_service import model_status
from services.document_comparison import compare_documents
router = APIRouter()

@router.post("/api/chat")
@router.post("/api/ask", include_in_schema=False)
def ask(b: dict, uid: int = Depends(me)):
    return answer_question(b, uid)

@router.get("/api/assistant/status")
def assistant_status(uid: int = Depends(me)):
    # Authentication is required even though this exposes only configuration state.
    return model_status()

@router.post("/api/compare")
def compare(b: dict, uid: int = Depends(me)):
    return compare_documents(b, uid)
