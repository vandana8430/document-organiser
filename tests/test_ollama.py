import httpx
import pytest
import sys
from types import ModuleType, SimpleNamespace

from services import llm_service


def _mock_genai(monkeypatch, client_factory):
    google = ModuleType("google")
    genai = ModuleType("google.genai")
    types = ModuleType("google.genai.types")
    genai.Client = client_factory
    types.HttpOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    types.HttpRetryOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    types.GenerateContentConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", types)


def test_embeddinggemma_indexes_with_local_ollama_embeddings(monkeypatch):
    import numpy as np
    from services import document_processor

    monkeypatch.setenv("SMARTFILES_OLLAMA_EMBEDDINGS", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "embeddinggemma")
    monkeypatch.setattr(document_processor, "_m", False)
    monkeypatch.setattr(document_processor, "_active_embedding_backend", None)
    monkeypatch.setattr(document_processor, "_ollama_embedding_retry_after", 0.0)
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"embeddings": [[3.0, 4.0], [0.0, 2.0]]})

    monkeypatch.setattr(document_processor.httpx, "post", fake_post)
    vectors = document_processor.embed(["first passage", "second passage"])
    assert vectors.shape == (2, 2)
    assert np.allclose(np.linalg.norm(vectors, axis=1), [1.0, 1.0])
    assert document_processor.embedding_backend() == "ollama:embeddinggemma"
    assert calls[0][0] == "http://localhost:11434/api/embed"
    assert calls[0][1] == {"model": "embeddinggemma", "input": ["first passage", "second passage"]}


def test_ollama_is_primary_and_receives_history_and_document_evidence(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(llm_service, "_api_key", lambda: pytest.fail("Gemini must not be called"))
    monkeypatch.setattr(llm_service, "_gemini_quota_until", 0.0)
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, request=httpx.Request("GET", "http://localhost:11434/api/tags"),
        json={"models": [{"name": "llama3.2:3b"}]},
    ))
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"message": {"content": "The method is described in the source [S1]."}})

    monkeypatch.setattr(llm_service.httpx, "post", fake_post)
    provider = {}
    answer = llm_service.generate_answer(
        "What is the method?", history=[{"role": "user", "text": "Summarize my paper"}],
        evidence=[{"source_ref": "S1", "name": "study.pdf", "reference": "Page 2",
                   "text": "The experiment compared three models."}], provider_out=provider,
    )
    assert answer == "The method is described in the source [S1]."
    assert provider["provider"] == "ollama"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["payload"]["model"] == "llama3.2:3b"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["payload"]["messages"][1] == {
        "role": "user", "content": "Summarize my paper"
    }
    assert "[S1] study.pdf (Page 2)" in captured["payload"]["messages"][-1]["content"]
    assert "The experiment compared three models." in captured["payload"]["messages"][-1]["content"]


def test_local_status_requires_the_configured_model_to_be_installed(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "missing-model:1b")
    monkeypatch.setattr(llm_service, "_api_key", lambda: "")
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, request=httpx.Request("GET", "http://localhost:11434/api/tags"),
        json={"models": [{"name": "llama3.2:3b"}]},
    ))
    status = llm_service.model_status()
    assert status["configured"] is False
    assert status["ollama_available"] is False
    assert status["model"] == "missing-model:1b"
    assert "unavailable" in status["message"].lower()


def test_provider_unavailability_is_friendly_and_does_not_leak_provider_details(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setattr(llm_service, "_api_key", lambda: "")

    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("private connection detail")

    monkeypatch.setattr(llm_service.httpx, "get", unavailable)
    with pytest.raises(llm_service.ModelUnavailable) as error:
        llm_service.generate_answer("What is photosynthesis?")
    assert str(error.value) == llm_service.FRIENDLY_UNAVAILABLE
    assert "private connection detail" not in str(error.value)


def test_gemini_quota_exhaustion_is_not_retried_during_cooldown(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setattr(llm_service, "_api_key", lambda: "test-secret-key")
    monkeypatch.setattr(llm_service, "_gemini_quota_until", 0.0)
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, request=httpx.Request("GET", "http://localhost:11434/api/tags"), json={"models": []}
    ))
    calls = []

    class QuotaError(Exception):
        code = 429
        message = "quota exhausted; test-secret-key must never appear in a response"

    class FakeModels:
        def generate_content(self, **_kwargs):
            calls.append("request")
            raise QuotaError("quota exhausted")

    class FakeClient:
        models = FakeModels()
        def close(self):
            pass

    _mock_genai(monkeypatch, lambda **_kwargs: FakeClient())
    for _ in range(2):
        with pytest.raises(llm_service.ModelUnavailable) as error:
            llm_service.generate_answer("Explain a common topic")
        assert str(error.value) == llm_service.FRIENDLY_UNAVAILABLE
        assert "test-secret-key" not in str(error.value)
    assert calls == ["request"]
    assert llm_service._gemini_quota_until > 0


def test_gemini_retries_once_only_for_transient_service_errors(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("GEMINI_MODEL", "test-flash")
    monkeypatch.setattr(llm_service, "_api_key", lambda: "test-secret-key")
    monkeypatch.setattr(llm_service, "_gemini_quota_until", 0.0)
    calls = []

    class BusyError(Exception):
        code = 503

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise BusyError("temporary overload")
            return SimpleNamespace(text="A successful fallback answer.")

    class FakeClient:
        models = FakeModels()
        def close(self):
            pass

    _mock_genai(monkeypatch, lambda **_kwargs: FakeClient())
    assert llm_service.generate_answer("Explain a common topic") == "A successful fallback answer."
    assert calls == ["test-flash", "test-flash"]


def test_gemini_timeout_retry_is_bounded_and_returns_a_friendly_error(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setattr(llm_service, "_api_key", lambda: "test-secret-key")
    monkeypatch.setattr(llm_service, "_gemini_quota_until", 0.0)
    calls = []

    class SlowModels:
        def generate_content(self, **_kwargs):
            calls.append("request")
            raise TimeoutError("private timeout detail")

    class FakeClient:
        models = SlowModels()
        def close(self):
            pass

    _mock_genai(monkeypatch, lambda **_kwargs: FakeClient())
    with pytest.raises(llm_service.ModelUnavailable) as error:
        llm_service.generate_answer("Explain a common topic")
    assert str(error.value) == llm_service.FRIENDLY_UNAVAILABLE
    assert calls == ["request", "request"]


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
