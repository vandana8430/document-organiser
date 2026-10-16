"""SmartFiles FastAPI entry point. Run with: python -m uvicorn app:app --reload"""
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
PROJECT_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass
from database import connection
from routers import assistant, auth, demo, documents, duplicates, search
app = FastAPI(title="SmartFiles", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
app.include_router(auth.router)
app.include_router(demo.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(duplicates.router)
app.include_router(assistant.router)
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(str(PROJECT_ROOT / "templates" / "index.html"))
