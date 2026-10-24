"""Release the isolated SQLite handle so Windows can clean the test directory."""
import sys


def pytest_sessionfinish(session, exitstatus):
    database = sys.modules.get("database.connection")
    if database is not None:
        database.DB.close()
