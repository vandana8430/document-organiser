"""Release the isolated SQLite handle so Windows can clean the test directory."""
import sys
import pytest


@pytest.fixture(autouse=True)
def prevent_live_tavily_requests(monkeypatch):
    """API tests must never consume the developer's real Tavily credits."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def pytest_sessionfinish(session, exitstatus):
    database = sys.modules.get("database.connection")
    if database is not None:
        database.DB.close()
