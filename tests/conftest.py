"""Release the isolated SQLite handle so Windows can clean the test directory."""
import sys
import pytest


@pytest.fixture(autouse=True)
def prevent_live_tavily_requests(monkeypatch):
    """Keep all provider calls mocked and prevent tests from reading private keys."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SMARTFILES_OLLAMA_EMBEDDINGS", "0")
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("SMARTFILES_OLLAMA_MODEL", "")
    from services import llm_service
    monkeypatch.setattr(llm_service, "_api_key", lambda: "")
    monkeypatch.setattr(llm_service, "_gemini_quota_until", 0.0)


def pytest_sessionfinish(session, exitstatus):
    database = sys.modules.get("database.connection")
    if database is not None:
        database.DB.close()
