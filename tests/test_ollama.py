import httpx
import pytest

from services import llm_service


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


def test_gemini_chat_sends_valid_ordered_history_and_bounded_evidence(monkeypatch):
    from types import SimpleNamespace
    from google import genai
    from google.genai import types

    monkeypatch.setattr(llm_service, "_api_key", lambda: "test-key")
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            for content in kwargs["contents"]:
                types.Content.model_validate(content)
            return SimpleNamespace(text="The experiment compared three models [S1].")

    class FakeClient:
        models = FakeModels()
        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(genai, "Client", lambda **kwargs: FakeClient())
    evidence = [{"source_ref": f"S{i}", "name": f"study{i}.pdf", "reference": "Page 2",
                 "text": f"Relevant study excerpt {i}."} for i in range(1, 6)]
    answer = llm_service.generate_answer(
        "What is the method?",
        history=[{"role": "user", "text": "Summarize my paper"},
                 {"role": "assistant", "text": "The paper studies three models."}],
        evidence=evidence,
    )
    assert answer == "The experiment compared three models [S1]."
    assert captured["model"] == "gemini-3.8-flash"
    assert [turn["role"] for turn in captured["contents"]] == ["user", "model", "user"]
    assert captured["contents"][-1]["parts"][0]["text"].startswith("The following retrieved excerpts")
    assert "Current user question: What is the method?" in captured["contents"][-1]["parts"][0]["text"]
    assert "study4.pdf" in captured["contents"][-1]["parts"][0]["text"]
    assert "study5.pdf" not in captured["contents"][-1]["parts"][0]["text"]
    assert captured["config"].temperature == 0.1
    assert captured["closed"] is True


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

