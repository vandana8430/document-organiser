"""Release the isolated SQLite handle so Windows can clean the test directory."""
import sys
import pytest


@pytest.fixture(autouse=True)
def prevent_live_tavily_requests(monkeypatch):
    """API tests must never consume the developer's real Tavily credits."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SMARTFILES_OLLAMA_EMBEDDINGS", "0")
    # API tests must not send requests with the developer's private Gemini key.
    from services import llm_service
    monkeypatch.setattr(llm_service, "_api_key", lambda: "")


def pytest_sessionfinish(session, exitstatus):
    database = sys.modules.get("database.connection")
    if database is not None:
        database.DB.close()
