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

<<<<<<< HEAD
### Run the AI Assistant with local Ollama

Install Ollama and the model you want to use. Run `ollama list` and copy the exact model name/tag into `.env` as `OLLAMA_MODEL=<installed-model-name>`. SmartFiles does not assume a model name or download models. Ollama's local API must be available at `http://localhost:11434`.

If Ollama is not already running, start it in one terminal:

```powershell
ollama serve
```

In another terminal, start FastAPI from the project folder using the setup commands above (or `run_project.bat`). Sign in and open **AI Assistant**. The authenticated `POST /api/chat` accepts messages and recent conversation history; `POST /api/ask` remains a backward-compatible alias. `GET /api/assistant/status` checks whether Ollama is responding and the configured model is installed. `OLLAMA_TIMEOUT_SECONDS` controls the inference timeout (5–300 seconds; default 120).

=======
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15
Or double-click `run_project.bat`; it uses `.venv311`, checks the dependencies, and can install the packages from `requirements.txt` when needed. Python 3.7 and the Microsoft Store execution alias are not supported. Use the full python.org interpreter path shown above if `py -3.11` selects the Store runtime.

## Optional processing features

- Search supports filename and extracted-text queries plus category, tag, type, and upload-date filters. Keyword search works without an external AI key. Sentence-transformers MiniLM semantic search and CLIP visual similarity are optional; install `sentence-transformers` in the project virtual environment to enable them. The models download on first use.
<<<<<<< HEAD
- The assistant uses owner-scoped document retrieval and saved plagiarism reports as evidence, then sends bounded context and recent conversation history to the local model named by `OLLAMA_MODEL`. If Ollama is unavailable, document answers fall back to exact extracted passages, summaries, and clear insufficient-evidence responses. No hosted or paid model API is used.
- In **My Documents**, choose **Repair text index** to retry text extraction for documents with missing text or chunks. The response lists recovered files, skipped records, and errors. A failed attempt preserves its prior index and original upload. The protected API is `POST /api/reprocess`; optionally pass `?file_id=<your-document-id>` to retry one owned document. Uploading identical bytes again reuses the successful extracted text while retaining a separate file record.
- Duplicate Finder groups byte-identical files by SHA-256 and reports potential text near-duplicates using MinHash candidate buckets followed by Jaccard similarity over normalized three-word phrases. The score is a review aid, not proof of identity. Optional similar-image detection requires stored CLIP vectors.
- **Plagiarism Checker** uses deterministic text comparison (not an AI classifier) on extracted PDF, DOCX, and TXT text, comparing the selected document against all other documents with extracted text that belong to the signed-in user. It excludes the selected document itself and never searches other users' files. Select an existing document or upload a PDF, DOCX, or TXT file directly from the checker; uploads use the same authenticated pipeline as the Upload Documents page. Local comparison does not call any external API or require an API key. Reports persist in the existing SQLite database and are private to their owner. Reports on imported fictional demo documents are prominently labeled as sample data. Use **Download PDF Report** on an opened report to generate a multi-page PDF from the owner's saved scan; the server does not accept report contents from the browser.
- Plagiarism reports show exact-word coverage and approximate-only coverage separately, each calculated as the words in matched analyzed passages divided by all words in the analyzed passages (sentences with at least five words). Approximate coverage excludes passages with an exact match and is not added to exact coverage. Matching preserves punctuation and normalizes Unicode (NFKC), case, and whitespace; approximate matches use word-set Jaccard overlap of at least 0.78. At most 300 passages from the selected document are analyzed and local matching uses at most the first 1,000 sentences in each source document; reports disclose source truncation. A zero result means only that the configured comparison method found no match, not that a document is original.
- Optional online plagiarism checks use the Quetext Partner API when **Check Plagiarism** is selected. It requires `QUETEXT_API_KEY`; SmartFiles sends the key only in the server-side `X-API-Key` header. Quetext accepts 20 to 50,000 words per report and returns matching input passages, source URLs, snippets, and similarity scores. Its API uses a prepaid wallet (currently documented at $0.10 per 1,000 words); online scans can incur charges. If the key is missing or the provider request fails, the report explains why the online check did not complete and still includes local comparison. See the [official Quetext API documentation](https://www.quetext.com/developers-api).
- Downloadable plagiarism reports use ReportLab (declared in `requirements.txt`) to create a blue-and-white, page-numbered PDF with scan details, coverage summaries and limitations, matching passages, and source links where available. Sensitive credential patterns are redacted from report text, and sensitive URL query parameters are omitted.
- **Previous reports** displays saved scans in pages of 50, including stored match summaries and analyzed/unchecked coverage. Select two scans of the same uploaded document and choose **Compare selected scans** to view observed local and online match/source changes and coverage differences. Comparisons use the saved report snapshots and warn about differing methods, coverage, online availability, or source sets; a decrease in matches is not evidence that a document became more original. History, report viewing, comparisons, and PDF downloads are authenticated and owner-scoped. This uses the existing `plagiarism_scans` table and indexes; no database migration or data reset is required.
- Open a saved plagiarism report to search passage text, source titles, and domains; filter by local/online or exact/approximate matches; and sort by available similarity scores. Headline report totals remain unchanged by filters. Previous reports can be searched by document name and filtered by scan status; match-count sorting applies to the currently loaded history pages, and totals that are unavailable are not treated as zero. **Understand Your Results** explains checked and unchecked coverage, confirmed matches, possible sources, and review/citation suggestions using saved report data only. It is template-based and does not generate AI conclusions.
- To try a local comparison, create `plagiarism_reference.txt` containing `The coastal sensor network records water quality readings and sends the measurements to a central monitoring dashboard.` and `plagiarism_draft.txt` containing that same first sentence followed by `Review matching passages manually because textual overlap alone does not prove plagiarism.` Upload both in SmartFiles, open **Plagiarism Checker**, choose the draft, and scan without online checking. The match is test material, not a real plagiarism finding. For online checks, configure `QUETEXT_API_KEY` and optionally `TAVILY_API_KEY` as described below, restart FastAPI, and inspect the returned matches and source links.
- **File Compressor** creates separate, user-scoped optimized copies of PDFs and JPG/JPEG, PNG, or WebP images. PDF streams and repeated objects are optimized with pypdf; PNG optimization is lossless, while JPEG/WebP quality reduction is explicitly lossy and image resizing is optional. Password-protected/corrupt/unsupported inputs are rejected. Original uploads remain untouched, results are validated before recording, and both before/after measurements and history are available through `POST /api/compress` and `GET /api/compression/history`.
- The Dashboard's category, format, monthly-upload, processing, storage, duplicate, and largest-file charts are computed from the authenticated user's SQLite data. Category/type/date filters reload the API data. **Smart Storage Optimizer** shows real largest-file sizes, exact/near duplicate matches, and measured compression results; it does not estimate unmeasured savings or delete/compress files automatically.
- The AI assistant answers general questions locally and grounds claims about the user's files and plagiarism reports in retrieved text and verified saved report evidence. Follow-up questions include bounded conversation history. Unsupported claims about private files return an insufficient-evidence response.
- PDF, DOCX, XLSX, PPTX, CSV and plain-text extraction use the packages in `requirements.txt`. OCR for scanned PDF pages and PNG/JPG images needs the Tesseract executable installed and available on `PATH`. Video transcription is optional and needs `faster-whisper` plus FFmpeg.
- Legacy binary `.doc` files are rejected with a conversion message; save them as `.docx` first. `.zip` archives are also rejected rather than extracted. OOXML files are validated as bounded ZIP containers before the spreadsheet or presentation parser reads them.
- Copy `.env.example` to `.env` to set `OLLAMA_MODEL`, `QUETEXT_API_KEY` for the legacy Quetext workflow, or override database/upload paths. Alternatively, set these values in the backend process environment. SmartFiles also loads `services/key.env` as a local fallback; both `.env` and that file are excluded by `.gitignore`.

### Configure Quetext on Windows

In PowerShell, from the project folder:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Paste your Quetext API key after `QUETEXT_API_KEY=` in `.env` or `services/key.env` (or set it in the backend environment). Create the API key under Quetext Account > API Keys. Keep it private; do not paste it into frontend files or commit it. Quetext API scans use prepaid credits. To enable web search if Quetext is not configured or its wallet is empty, set `TAVILY_API_KEY` in the same backend environment; SmartFiles checks returned page text before reporting any match. Tavily results do not claim an overall plagiarism score; the UI shows the measured coverage of verified passages in the searched results. Restart FastAPI:

```powershell
.\.venv311\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

Then open `http://127.0.0.1:8000/`, sign in, and upload a PDF, DOCX, or TXT file in **Plagiarism Checker**. Online checking submits the extracted document text to Quetext; local comparison is also performed and does not require an API key.
=======
- The assistant uses owner-scoped retrieval and a local extractive fallback; there is no configured LLM or generation API. It returns relevant source sentences, extractive summaries, exact email addresses found in the user's documents, and clear insufficient-evidence responses. PDF page labels are included in source references where extracted.
- In **My Documents**, choose **Repair text index** to retry text extraction for documents with missing text or chunks. The response lists recovered files, skipped records, and errors. A failed attempt preserves its prior index and original upload. The protected API is `POST /api/reprocess`; optionally pass `?file_id=<your-document-id>` to retry one owned document. Uploading identical bytes again reuses the successful extracted text while retaining a separate file record.
- Duplicate Finder groups byte-identical files by SHA-256 and reports potential text near-duplicates using MinHash candidate buckets followed by Jaccard similarity over normalized three-word phrases. The score is a review aid, not proof of identity. Optional similar-image detection requires stored CLIP vectors.
- PDF, DOCX, XLSX, PPTX, CSV and plain-text extraction use the packages in `requirements.txt`. PNG/JPG image OCR needs the Tesseract executable installed and available on `PATH`. Video transcription is optional and needs `faster-whisper` plus FFmpeg.
- Legacy binary `.doc` files are rejected with a conversion message; save them as `.docx` first. `.zip` archives are also rejected rather than extracted. OOXML files are validated as bounded ZIP containers before the spreadsheet or presentation parser reads them.
- Copy `.env.example` to `.env` to override database or upload paths. Never commit `.env`.
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15

## Demo documents

After signing in, select **Load Demo Documents** on the Dashboard. SmartFiles imports the fictional files in `sample_documents/` through the same validation, extraction, categorization, summary, and indexing pipeline used for uploads. Imports are private to the signed-in account, are stored under `uploads/demo/<user-id>/`, and are marked **Sample data** throughout the app. Repeated clicks skip files already imported for that account. There is no bulk reset button; you can remove a sample document individually from its details page without touching your other files.

The collection includes searchable resumes, project and meeting reports, certificates, assignments, technical notes, student/employee/expense data, a multi-sheet workbook, a presentation, and labeled diagram/certificate images. Two meeting-note text files are intentionally byte-identical for the duplicate finder. All people, institutions, project results, and records are fictional.

## Automatic categories and tags

Uploads and demo imports share the same local, rule-based categorizer. It considers the filename, extension/file kind, and extracted text; image files are categorized as images even when OCR is unavailable. Categories and content tags are stored in the existing `files.category` and `files.tags` columns. The document list shows category/topic labels, the detail view shows tags and lets the signed-in owner change a category, and the Dashboard category totals filter My Documents. My Documents can also sort by date, name, or category. This does not call a paid AI service. Existing records keep their stored categories until edited; new uploads and newly imported demo files use the new category set.

## Project structure

```text
app.py                         FastAPI entry point, route registration and static mount
<<<<<<< HEAD
routers/                       Authentication, documents, search, duplicates, assistant, compression APIs
=======
routers/                       Authentication, documents, search, duplicates, assistant APIs
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15
services/
  auth_service.py               Authentication and session helpers
  document_processor.py         File validation, text extraction, embeddings and chunks
  categorizer.py                Categories and keywords
  summarizer.py                 Extractive summaries
  semantic_search.py            Owner-scoped keyword, filter, and optional embedding search
  duplicate_detector.py         Content hashes, text near-duplicates, optional image pairs
  rag_assistant.py              Owner-scoped retrieval and evidence-grounded answers
  document_comparison.py        Local comparison of extracted text
<<<<<<< HEAD
  file_compression.py           Validated PDF/image optimization and private result history
  plagiarism.py                 Local passage comparison and optional Quetext report retrieval
  plagiarism_pdf.py             Owner-report PDF document generation
  storage_analytics.py          Owner-scoped dashboard metrics and optimizer suggestions
=======
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15
database/
  connection.py                 SQLite connection, schema migration and transactions
  repository.py                 Locked query access to SQLite
services/document_ingestion.py Shared safe upload/import processing pipeline
services/demo_documents.py     Per-user idempotent sample importer
routers/demo.py                Authenticated demo import route
<<<<<<< HEAD
routers/plagiarism.py          Authenticated plagiarism scans and private reports
sample_documents/              Fictional PDF, DOCX, TXT, CSV, XLSX, PPTX, PNG and JPG files
 templates/index.html           Active single-page frontend
 static/css/style.css            Frontend styles
 static/js/                     Navigation, documents, search, analytics, compression, duplicates, assistant and comparison
=======
sample_documents/              Fictional PDF, DOCX, TXT, CSV, XLSX, PPTX, PNG and JPG files
 templates/index.html           Active single-page frontend
 static/css/style.css            Frontend styles
 static/js/                     Navigation, documents, search, duplicates, assistant and comparison
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15
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

<<<<<<< HEAD
For a browser walkthrough, sign in and select **Load Demo Documents**. Confirm the charts reflect your documents; filter by category or date; search for `soil moisture below 28 percent`; scan Duplicate Finder; compare two text-bearing documents; and ask the assistant about the WaterWise threshold. Open **Smart Storage Optimizer** to review duplicate groups and largest files, then use **File Compressor** to optimize a PDF or image. Confirm before/after sizes, preview/download the output, and verify the original remains unchanged. Click **Load Demo Documents** again to confirm existing samples are skipped.
=======
For a browser walkthrough, sign in and select **Load Demo Documents**. Confirm they appear on the Dashboard and in My Documents with **Sample data** labels. Search for `soil moisture below 28 percent`, scan Duplicate Finder for the matching meeting notes, open a demo PDF or image, compare two text-bearing documents, ask the assistant about the WaterWise threshold, then download or delete one demo file. Click **Load Demo Documents** again to confirm existing samples are skipped. Browser DevTools' Console and Network panels show frontend errors and API responses.
>>>>>>> 2922d5d1b3218f61a8050ce3be58ab4c8c017d15
