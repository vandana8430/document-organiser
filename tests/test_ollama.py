import httpx
import pytest

from services import llm_service


def test_ollama_chat_uses_configured_model_history_and_bounded_evidence(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "local-research:7b-q4")
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return httpx.Response(200, json={"model": "local-research:7b-q4",
                                         "message": {"role": "assistant", "content": "Found in the paper [S1]."},
                                         "done": True})

    monkeypatch.setattr(llm_service.httpx, "post", fake_post)
    answer = llm_service.generate_answer(
        "What is the method?", history=[{"role": "user", "text": "Summarize my paper"}],
        evidence=[{"source_ref": "S1", "name": "study.pdf", "reference": "Page 2",
                   "text": "The experiment compared three models."}],
    )
    assert answer == "Found in the paper [S1]."
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["payload"]["model"] == "local-research:7b-q4"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["messages"][1] == {"role": "user", "content": "Summarize my paper"}
    prompt = captured["payload"]["messages"][-1]["content"]
    assert "[S1] study.pdf (Page 2)" in prompt
    assert "The experiment compared three models." in prompt


def test_ollama_requires_model_from_environment(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setattr(llm_service.httpx, "post", lambda *_args, **_kwargs: pytest.fail("must not call Ollama"))
    with pytest.raises(llm_service.ModelUnavailable, match="Set OLLAMA_MODEL"):
        llm_service.generate_answer("Hello")
    status = llm_service.model_status()
    assert status["configured"] is False
    assert "OLLAMA_MODEL" in status["message"]


@pytest.mark.parametrize("status_code,expected", [
    (404, "could not find model"),
    (500, "HTTP 500"),
])
def test_ollama_http_errors_are_actionable(monkeypatch, status_code, expected):
    monkeypatch.setenv("OLLAMA_MODEL", "missing-local-model")
    message = "model not found" if status_code == 404 else "service temporarily unavailable"
    monkeypatch.setattr(llm_service.httpx, "post", lambda *_args, **_kwargs: httpx.Response(status_code, json={"error": message}))
    with pytest.raises(llm_service.ModelUnavailable, match=expected):
        llm_service.generate_answer("Hello")


@pytest.mark.parametrize("exception,expected", [
    (httpx.ReadTimeout("slow"), "took too long"),
    (httpx.ConnectError("offline"), "Start Ollama"),
])
def test_ollama_timeout_and_connection_errors_are_actionable(monkeypatch, exception, expected):
    monkeypatch.setenv("OLLAMA_MODEL", "local-model")

    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(llm_service.httpx, "post", fail)
    with pytest.raises(llm_service.ModelUnavailable, match=expected):
        llm_service.generate_answer("Hello")


def test_ollama_empty_response_and_model_status(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "local-model:latest")
    monkeypatch.setattr(llm_service.httpx, "post", lambda *_args, **_kwargs: httpx.Response(200, json={"message": {"content": "  "}}))
    with pytest.raises(llm_service.ModelUnavailable, match="empty answer"):
        llm_service.generate_answer("Hello")

    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={"models": [{"name": "local-model:latest"}]}))
    assert llm_service.model_status() == {
        "configured": True, "provider": "Ollama", "model": "local-model:latest",
        "message": "Ollama model local-model:latest is ready. Document answers use retrieved evidence.",
    }
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={"models": []}))
    missing = llm_service.model_status()
    assert missing["configured"] is False
    assert "not installed" in missing["message"]


def test_plagiarism_report_context_contains_only_verified_source_evidence(monkeypatch):
    import json
    from services import rag_assistant

    report = {"online_search_status": "completed", "academic_coverage_percent": 25,
              "online_matches": [{"passage": "A passage from the paper.", "sources": [
                  {"verification": "accessible_full_text", "doi": "10.5555/valid", "title": "Verified paper",
                   "authors": ["A. Author"], "similarity_percent": 82.5, "evidence": "Matched sentence from accessible full text."},
                  {"verification": "metadata_only", "title": "Unverified metadata", "evidence": "Abstract only."},
              ]}]}
    row = {"id": 91, "fid": 24, "document_name": "research.pdf", "status": "completed",
           "report_json": json.dumps(report)}
    captured = {}

    def fake_query(sql, args):
        captured["sql"] = sql
        captured["args"] = args
        return [row]

    monkeypatch.setattr(rag_assistant, "q", fake_query)
    evidence, sources = rag_assistant._plagiarism_context(7, "What did my plagiarism report find?", [24])
    assert captured["args"] == (7, 24, 8)
    assert "s.uid=?" in captured["sql"] and "f.uid=s.uid" in captured["sql"]
    assert len(evidence) == len(sources) == 1
    assert "Matched sentence from accessible full text." in evidence[0]["text"]
    assert "82.5%" in evidence[0]["text"]
    assert "Abstract only" not in evidence[0]["text"]
    assert sources[0]["ref"] == "P1"
    assert sources[0]["url"] == "https://doi.org/10.5555/valid"

