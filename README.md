# SmartFiles

SmartFiles is a FastAPI document organizer with a browser dashboard. The existing SQLite database and uploads remain in the project root and are resolved relative to the project files, independent of the terminal's current directory.

## Run on Windows

Open the workspace folder in VS Code. In its PowerShell terminal, run:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
.\.venv311\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

Open `http://127.0.0.1:8000/`. FastAPI's endpoint explorer is at `http://127.0.0.1:8000/docs`. The `run_project.bat` launcher changes to the project folder before starting Uvicorn and opening the browser.

Or double-click `run_project.bat`; it uses `.venv311`, checks the dependencies, and can install the packages from `requirements.txt` when needed. Python 3.7 and the Microsoft Store execution alias are not supported. Use the full python.org interpreter path shown above if `py -3.11` selects the Store runtime.

## Optional processing features

- Search supports filename and extracted-text queries plus category, tag, type, and upload-date filters. Keyword search works without an external AI key. Sentence-transformers MiniLM semantic search and CLIP visual similarity are optional; install `sentence-transformers` in the project virtual environment to enable them. The models download on first use.
- The assistant uses owner-scoped retrieval and a local extractive fallback; there is no configured LLM or generation API. It returns relevant source sentences, extractive summaries, exact email addresses found in the user's documents, and clear insufficient-evidence responses. PDF page labels are included in source references where extracted.
- In **My Documents**, choose **Repair text index** to retry text extraction for documents with missing text or chunks. The response lists recovered files, skipped records, and errors. A failed attempt preserves its prior index and original upload. The protected API is `POST /api/reprocess`; optionally pass `?file_id=<your-document-id>` to retry one owned document. Uploading identical bytes again reuses the successful extracted text while retaining a separate file record.
- Duplicate Finder groups byte-identical files by SHA-256 and reports potential text near-duplicates using MinHash candidate buckets followed by Jaccard similarity over normalized three-word phrases. The score is a review aid, not proof of identity. Optional similar-image detection requires stored CLIP vectors.
- PDF, DOCX, XLSX, PPTX, CSV and plain-text extraction use the packages in `requirements.txt`. PNG/JPG image OCR needs the Tesseract executable installed and available on `PATH`. Video transcription is optional and needs `faster-whisper` plus FFmpeg.
- Legacy binary `.doc` files are rejected with a conversion message; save them as `.docx` first. `.zip` archives are also rejected rather than extracted. OOXML files are validated as bounded ZIP containers before the spreadsheet or presentation parser reads them.
- Copy `.env.example` to `.env` to override database or upload paths. Never commit `.env`.

## Demo documents

After signing in, select **Load Demo Documents** on the Dashboard. SmartFiles imports the fictional files in `sample_documents/` through the same validation, extraction, categorization, summary, and indexing pipeline used for uploads. Imports are private to the signed-in account, are stored under `uploads/demo/<user-id>/`, and are marked **Sample data** throughout the app. Repeated clicks skip files already imported for that account. There is no bulk reset button; you can remove a sample document individually from its details page without touching your other files.

The collection includes searchable resumes, project and meeting reports, certificates, assignments, technical notes, student/employee/expense data, a multi-sheet workbook, a presentation, and labeled diagram/certificate images. Two meeting-note text files are intentionally byte-identical for the duplicate finder. All people, institutions, project results, and records are fictional.

## Automatic categories and tags

Uploads and demo imports share the same local, rule-based categorizer. It considers the filename, extension/file kind, and extracted text; image files are categorized as images even when OCR is unavailable. Categories and content tags are stored in the existing `files.category` and `files.tags` columns. The document list shows category/topic labels, the detail view shows tags and lets the signed-in owner change a category, and the Dashboard category totals filter My Documents. My Documents can also sort by date, name, or category. This does not call a paid AI service. Existing records keep their stored categories until edited; new uploads and newly imported demo files use the new category set.

## Project structure

```text
app.py                         FastAPI entry point, route registration and static mount
routers/                       Authentication, documents, search, duplicates, assistant APIs
services/
  auth_service.py               Authentication and session helpers
  document_processor.py         File validation, text extraction, embeddings and chunks
  categorizer.py                Categories and keywords
  summarizer.py                 Extractive summaries
  semantic_search.py            Owner-scoped keyword, filter, and optional embedding search
  duplicate_detector.py         Content hashes, text near-duplicates, optional image pairs
  rag_assistant.py              Owner-scoped retrieval and evidence-grounded answers
  document_comparison.py        Local comparison of extracted text
database/
  connection.py                 SQLite connection, schema migration and transactions
  repository.py                 Locked query access to SQLite
services/document_ingestion.py Shared safe upload/import processing pipeline
services/demo_documents.py     Per-user idempotent sample importer
routers/demo.py                Authenticated demo import route
sample_documents/              Fictional PDF, DOCX, TXT, CSV, XLSX, PPTX, PNG and JPG files
 templates/index.html           Active single-page frontend
 static/css/style.css            Frontend styles
 static/js/                     Navigation, documents, search, duplicates, assistant and comparison
 tests/                         Existing isolated API integration test
 uploads/                       Existing user-uploaded files (kept in place)
 data.db                        Existing SQLite database (kept in place)
backups/                        Pre-refactor and pre-organization source backups
requirements.txt                Runtime and test dependencies
.env.example                    Optional database/upload path settings
.gitignore                      Local data, environments and generated-file exclusions
README.md                       Project setup and verification notes
run_project.bat                 Windows launcher
```

The UI remains a single page because its sections share navigation, session state, and JavaScript. No duplicate login/dashboard HTML pages or unused model modules were created. The database uses SQLite directly, so schema and connection code lives in `database/connection.py` rather than an unused ORM `models.py`. Existing API paths and ownership checks remain in the routers and services.

## Verify

```powershell
.\.venv311\Scripts\python.exe -m compileall -q app.py routers services database tests
.\.venv311\Scripts\python.exe -m pytest -q tests
```

For a browser walkthrough, sign in and select **Load Demo Documents**. Confirm they appear on the Dashboard and in My Documents with **Sample data** labels. Search for `soil moisture below 28 percent`, scan Duplicate Finder for the matching meeting notes, open a demo PDF or image, compare two text-bearing documents, ask the assistant about the WaterWise threshold, then download or delete one demo file. Click **Load Demo Documents** again to confirm existing samples are skipped. Browser DevTools' Console and Network panels show frontend errors and API responses.
