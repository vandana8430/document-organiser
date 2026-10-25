from services import academic_sources


def test_academic_match_requires_accessible_body_text(monkeypatch):
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms.", "start_offset": 0, "end_offset": 95}
    paper = {"doi": "10.5555/example", "title": "Distributed database resilience", "authors": ["A Researcher"], "url": "https://example.org/paper", "locations": ["https://example.org/paper.pdf"]}
    monkeypatch.setattr(academic_sources, "_search", lambda client, provider, query: [dict(paper, provider=provider)])
    monkeypatch.setattr(academic_sources, "_source_text", lambda client, url: " ".join([passage["text"]] * 12))
    report = {"errors": []}
    exact, approximate = academic_sources.compare([passage], report)
    assert report["online_search_status"] == "completed"
    assert len(report["sources"]) == 1
    assert report["sources"][0]["doi"] == "10.5555/example"
    assert exact[0] and not approximate


def test_titles_without_accessible_source_text_are_not_matches(monkeypatch):
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    paper = {"doi": "10.5555/example", "title": "A very similar title", "authors": [], "url": "https://example.org/paper", "locations": ["https://example.org/paper.pdf"]}
    monkeypatch.setattr(academic_sources, "_search", lambda client, provider, query: [dict(paper, provider=provider)])
    monkeypatch.setattr(academic_sources, "_source_text", lambda client, url: "")
    report = {"errors": []}
    exact, approximate = academic_sources.compare([passage], report)
    assert report["sources"] == []
    assert report["online_matches"] == []
    assert exact == {} and approximate == {}


def test_unrelated_accessible_source_text_does_not_create_a_match(monkeypatch):
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    paper = {"doi": "10.5555/unrelated", "title": "An unrelated accessible paper", "authors": [],
             "url": "https://example.org/unrelated", "locations": ["https://example.org/unrelated.pdf"]}
    unrelated = "Astronomical observations describe stellar formation across distant galaxies and instruments." * 12
    monkeypatch.setattr(academic_sources, "_search", lambda _client, _provider, _query: [dict(paper)])
    monkeypatch.setattr(academic_sources, "_source_text", lambda _client, _url: unrelated)
    report = {"errors": []}

    exact, approximate = academic_sources.compare([passage], report)

    assert exact == {} and approximate == {}
    assert report["sources"] == []
    assert report["online_matches"] == []
    assert report["candidate_matches"] == 0
    assert report["final_matches"] == 0


def test_relevant_passage_below_old_jaccard_cutoff_matches_with_source_evidence(monkeypatch):
    passage_text = "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."
    evidence = "Distributed databases improve resilience through replicated storage and coordinated methodology protocols techniques."
    paper = {"doi": "10.5555/near-match", "title": "A related database paper", "authors": [],
             "url": "https://example.org/near-match", "locations": ["https://example.org/near-match.pdf"]}
    monkeypatch.setattr(academic_sources, "_search", lambda _client, _provider, _query: [dict(paper)])
    monkeypatch.setattr(academic_sources, "_source_text", lambda _client, _url: " ".join([evidence] * 4))
    report = {"errors": []}

    exact, approximate = academic_sources.compare([{"index": 0, "text": passage_text}], report)

    assert exact == {}
    assert approximate[0]
    match = approximate[0][0]
    assert match["evidence"] == evidence
    assert match["source_title"] == paper["title"]
    assert match["source_url"] == paper["locations"][0]
    assert 62 <= match["similarity_percent"] < 72


def test_empty_extraction_report_is_terminal_and_keeps_academic_schema(monkeypatch):
    from services import plagiarism

    saved = {}
    monkeypatch.setattr(plagiarism, "_store", lambda scan_id, uid, status, report: saved.update(
        scan_id=scan_id, uid=uid, status=status, report=dict(report)
    ))

    plagiarism._empty_report(42, 7, "empty.pdf", True, "No readable text was extracted.",
                             online_provider="academic")

    assert saved["status"] == "completed_with_warnings"
    assert saved["report"]["online_search_provider"] == "crossref+openalex+semantic_scholar"
    assert saved["report"]["online_search_status"] == "not_run"
    assert saved["report"]["errors"] == ["No readable text was extracted."]


def test_supported_formats_extract_and_documents_are_checked_independently(tmp_path, monkeypatch):
    from docx import Document
    from reportlab.pdfgen import canvas
    from services.document_processor import extract

    body = "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms. " * 12
    txt = tmp_path / "paper.txt"
    txt.write_text(body, encoding="utf-8")
    docx = tmp_path / "paper.docx"
    document = Document()
    document.add_paragraph(body)
    document.save(str(docx))
    pdf = tmp_path / "paper.pdf"
    output = canvas.Canvas(str(pdf))
    output.drawString(45, 750, body[:180])
    output.save()

    for path, ext in ((txt, ".txt"), (docx, ".docx"), (pdf, ".pdf")):
        text, _ = extract(str(path), "document", ext)
        assert len(text.split()) >= 12

    calls = []
    monkeypatch.setattr(academic_sources, "_search", lambda client, provider, query: calls.append(query) or [])
    reports = []
    for document_name in ("first.txt", "second.docx"):
        passage = {"index": 0, "text": body[:100]}
        report = {"document_name": document_name, "errors": []}
        academic_sources.compare([passage], report)
        reports.append(report)
    assert [report["document_name"] for report in reports] == ["first.txt", "second.docx"]
    assert len(calls) == 6


def test_provider_searches_parse_only_mocked_responses(monkeypatch):
    import httpx

    payloads = {
        "api.crossref.org": {"message": {"items": [{"DOI": "10.1000/test", "title": ["Crossref title"], "author": [{"given": "Ada", "family": "Lovelace"}], "URL": "https://doi.org/10.1000/test"}]}},
        "api.openalex.org": {"results": [{"doi": "https://doi.org/10.1000/test", "title": "OpenAlex title", "authorships": [{"author": {"display_name": "Ada Lovelace"}}], "locations": [{"landing_page_url": "https://papers.invalid/paper", "pdf_url": "https://papers.invalid/paper.pdf"}], "primary_location": {"pdf_url": "https://papers.invalid/paper.pdf"}}]},
        "api.semanticscholar.org": {"data": [{"title": "Semantic title", "authors": [{"name": "Ada Lovelace"}], "externalIds": {"DOI": "10.1000/test"}, "openAccessPdf": {"url": "https://papers.invalid/paper.pdf"}, "url": "https://www.semanticscholar.org/paper/test"}]},
    }

    def handler(request):
        import json
        return httpx.Response(200, json=payloads[request.url.host])

    monkeypatch.setattr(academic_sources, "_CACHE", academic_sources.OrderedDict())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        crossref = academic_sources._search(client, "crossref", "unique crossref query 49001")
        openalex = academic_sources._search(client, "openalex", "unique openalex query 49002")
        semantic = academic_sources._search(client, "semantic_scholar", "unique semantic query 49003")
    assert crossref[0]["doi"] == "10.1000/test"
    assert crossref[0]["authors"] == ["Ada Lovelace"]
    assert openalex[0]["locations"] == ["https://papers.invalid/paper.pdf", "https://papers.invalid/paper"]
    assert semantic[0]["locations"] == ["https://papers.invalid/paper.pdf"]


def test_rate_limit_retries_once_and_timeout_or_http_failure_is_safe(monkeypatch):
    import httpx

    sleeps = []
    monkeypatch.setattr(academic_sources.time, "sleep", sleeps.append)
    calls = []

    def limited_then_ok(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"works": []})

    with httpx.Client(transport=httpx.MockTransport(limited_then_ok)) as client:
        assert academic_sources._get(client, "crossref", "https://api.crossref.org/works") == {"works": []}
    assert len(calls) == 2
    assert sleeps[-1] == 3.0

    def timeout(_request):
        raise httpx.ReadTimeout("mock timeout")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        assert academic_sources._source_text(client, "https://papers.invalid/timeout-49004") == ""

    def server_error(_request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(server_error)) as client:
        try:
            academic_sources._get(client, "openalex", "https://api.openalex.org/works")
        except httpx.HTTPStatusError as error:
            assert error.response.status_code == 503
        else:
            raise AssertionError("HTTP 503 should be reported to the caller")


def test_compare_reports_provider_failures_as_partial_coverage(monkeypatch):
    import httpx

    def failed_search(_client, provider, _query):
        if provider == "crossref":
            raise httpx.ReadTimeout("mock provider timeout")
        raise httpx.HTTPStatusError("mock API failure", request=httpx.Request("GET", "https://invalid"), response=httpx.Response(503))

    monkeypatch.setattr(academic_sources, "_search", failed_search)
    report = {"errors": []}
    exact, approximate = academic_sources.compare([
        {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    ], report)
    assert (exact, approximate) == ({}, {})
    assert report["searches_attempted"] == 3
    assert report["searches_completed"] == 0
    assert report["searches_failed"] == 3
    assert report["online_search_status"] == "completed_with_errors"
    assert "partial" in report["errors"][0]


def test_compare_persists_progress_after_matching_source_text(monkeypatch):
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    paper = {"doi": "10.5555/progress", "title": "Distributed database resilience", "authors": [],
             "url": "https://example.org/paper", "locations": ["https://example.org/paper.pdf"]}
    monkeypatch.setattr(academic_sources, "_search", lambda _client, _provider, _query: [dict(paper)])
    monkeypatch.setattr(academic_sources, "_source_text", lambda _client, _url: " ".join([passage["text"]] * 12))
    report = {"errors": []}
    progress_snapshots = []

    academic_sources.compare([passage], report, progress_callback=lambda: progress_snapshots.append(
        (report.get("academic_progress"), len(report.get("online_matches", [])))
    ))

    assert report["academic_passages_searched"] == 1
    assert report["online_search_status"] == "completed"
    assert any(matches for _stage, matches in progress_snapshots)
    assert progress_snapshots[-1][0] == "Academic search finished"


def test_provider_searches_run_concurrently_and_duplicate_queries_are_reused(monkeypatch):
    import threading

    barrier = threading.Barrier(3)
    calls = []
    def parallel_search(_client, provider, _query):
        calls.append(provider)
        barrier.wait(timeout=3)
        return []

    monkeypatch.setattr(academic_sources, "_search", parallel_search)
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    report = {"errors": []}

    exact, approximate = academic_sources.compare([passage, dict(passage)], report)

    assert exact == {} and approximate == {}
    assert sorted(calls) == ["crossref", "openalex", "semantic_scholar"]
    assert report["searches_attempted"] == 6
    assert report["searches_completed"] == 6


def test_provider_duplicates_merge_open_access_locations(monkeypatch):
    passage = {"index": 0, "text": "Distributed databases improve resilience through replicated storage and coordinated recovery mechanisms."}
    responses = {
        "crossref": [{"doi": "10.5555/merged", "title": "Merged academic paper",
                      "url": "https://doi.org/10.5555/merged", "locations": []}],
        "openalex": [{"doi": "10.5555/merged", "title": "Merged academic paper",
                       "url": "https://papers.invalid/landing", "locations": ["https://papers.invalid/fulltext.pdf"]}],
        "semantic_scholar": [],
    }
    retrieved = []
    monkeypatch.setattr(academic_sources, "_search", lambda _client, provider, _query: responses[provider])
    monkeypatch.setattr(academic_sources, "_source_text", lambda _client, url: retrieved.append(url) or
                        (" ".join([passage["text"]] * 12) if url.endswith(".pdf") else "Abstract only."))
    report = {"errors": []}

    exact, approximate = academic_sources.compare([passage], report)

    assert exact[0]
    assert not approximate
    assert report["sources"][0]["url"] == "https://papers.invalid/fulltext.pdf"
    assert "https://papers.invalid/fulltext.pdf" in retrieved


def test_source_text_extracts_visible_html_text(monkeypatch, caplog):
    import httpx
    import logging

    url = "https://papers.invalid/html-fulltext-progress-check"
    caplog.set_level(logging.INFO, logger=academic_sources.__name__)
    monkeypatch.setattr(academic_sources, "_CACHE", academic_sources.OrderedDict())
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"},
        text="<html><script>not evidence</script><article><h2>Abstract</h2><p>Abstract only is not evidence.</p><h2>Introduction</h2><p>Visible paper passage.</p></article></html>",
    ))) as client:
        text = academic_sources._source_text(client, url)
    assert text == "Introduction\nVisible paper passage."
    assert "not evidence" not in text
    assert "Abstract only is not evidence." not in text
    assert "chars=35 words=4" in caplog.text

    monkeypatch.setattr(academic_sources, "_CACHE", academic_sources.OrderedDict())
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"},
        text="<html><article><h2>Abstract</h2><p>Only an abstract is available here.</p></article></html>",
    ))) as client:
        assert academic_sources._source_text(client, url) == ""


def test_api_uploads_pdf_docx_txt_and_scans_each_document_independently(tmp_path, monkeypatch):
    import uuid
    from docx import Document
    from reportlab.pdfgen import canvas
    from test_api import client

    from services import academic_sources

    email = "academic-" + uuid.uuid4().hex + "@example.test"
    assert client.post("/api/register", json={"email": email, "password": "academic-test-123"}).status_code == 200
    excerpts = {
        "text": "REDWOOD replicated systems preserve transaction history during coordinated recovery. ",
        "word": "BLUEBIRD replicated systems preserve transaction history during coordinated recovery. ",
        "pdf": "GOLDENROD replicated systems preserve transaction history during coordinated recovery. ",
    }
    contents = {}
    contents[".txt"] = (excerpts["text"] * 14).encode("utf-8")
    docx_path = tmp_path / "report.docx"
    document = Document()
    document.add_paragraph(excerpts["word"] * 14)
    document.save(str(docx_path))
    contents[".docx"] = docx_path.read_bytes()
    pdf_path = tmp_path / "report.pdf"
    pdf = canvas.Canvas(str(pdf_path))
    for line, y in zip([excerpts["pdf"] * 3] * 8, range(760, 600, -20)):
        pdf.drawString(45, y, line)
    pdf.save()
    contents[".pdf"] = pdf_path.read_bytes()

    searches = []

    def mocked_search(_client, provider, query):
        searches.append((provider, query))
        matched = next((key for key, phrase in excerpts.items() if phrase.split()[0].casefold() in query.casefold()), None)
        if not matched:
            return []
        return [{"doi": "10.5555/" + matched, "title": "Research on " + matched,
                 "authors": ["Test Author"], "url": "https://papers.invalid/" + matched,
                 "locations": ["https://papers.invalid/" + matched + ".pdf"], "provider": provider}]

    monkeypatch.setattr(academic_sources, "_search", mocked_search)
    monkeypatch.setattr(academic_sources, "_source_text", lambda _client, url: " ".join([excerpts[next(k for k in excerpts if k in url)] * 14]))
    uploaded_ids = []
    for ext, filename, content_type in ((".txt", "redwood.txt", "text/plain"), (".docx", "bluebird.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), (".pdf", "goldenrod.pdf", "application/pdf")):
        response = client.post("/api/upload", files={"file": (filename, contents[ext], content_type)})
        assert response.status_code == 200, response.text
        uploaded_ids.append((response.json()["id"], filename))

    for file_id, filename in uploaded_ids:
        scan = client.post("/api/plagiarism/scans", json={"file_id": file_id, "online_search": True, "online_provider": "academic"})
        assert scan.status_code == 202, scan.text
        result = client.get("/api/plagiarism/reports/" + str(scan.json()["scan_id"])).json()
        assert result["document_name"] == filename
        assert result["online_search_status"] == "completed"
        assert result["sources"]
        assert result["sources"][0]["verification"] == "accessible_full_text"
        expected_key = {"redwood.txt": "text", "bluebird.docx": "word", "goldenrod.pdf": "pdf"}[filename]
        assert result["sources"][0]["doi"] == "10.5555/" + expected_key
    assert len(uploaded_ids) == 3
    assert len(searches) >= 9 and len(searches) % 3 == 0
    assert {provider for provider, _ in searches} == {"crossref", "openalex", "semantic_scholar"}
    assert all(any(term in query.casefold() for _, query in searches) for term in ("redwood", "bluebird", "goldenrod"))
