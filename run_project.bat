
@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "SMARTFILES_PYTHON=.venv311\Scripts\python.exe"
if not exist "%SMARTFILES_PYTHON%" (
  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    echo Creating the SmartFiles Python 3.11 environment...
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" -m venv .venv311
  ) else (
    echo Creating the SmartFiles Python 3.11 environment...
    py -3.11 -m venv .venv311
  )
  if errorlevel 1 (
    echo Could not create .venv311. Install Python 3.11 from python.org and retry.
    pause
    exit /b 1
  )
)
"%SMARTFILES_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo SmartFiles needs a working Python 3.10 or newer environment.
  echo The selected .venv311 is invalid. Remove only the .venv311 folder and run this launcher again.
  pause
  exit /b 1
)
"%SMARTFILES_PYTHON%" -c "import fastapi, uvicorn, numpy, pypdf, fitz, docx, openpyxl, pptx, PIL, pytesseract"
if errorlevel 1 (
  echo Installing the dependencies declared in requirements.txt...
  "%SMARTFILES_PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Run .venv311\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
  )
  "%SMARTFILES_PYTHON%" -c "import fastapi, uvicorn, numpy, pypdf, fitz, docx, openpyxl, pptx, PIL, pytesseract"
  if errorlevel 1 (
    echo Required packages are still unavailable in .venv311.
    pause
    exit /b 1
  )
)
where tesseract >nul 2>&1
if errorlevel 1 if not exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" echo Optional OCR is unavailable: install the Tesseract executable to extract text from images and scanned PDF pages.
netstat -ano | findstr /C:":8000" >nul
if errorlevel 1 (
  echo.
  echo SmartFiles will open at: http://127.0.0.1:8000/
  echo API docs: http://127.0.0.1:8000/docs
  echo Keep this terminal open. Press Ctrl+C to stop the server.
  start "" "http://127.0.0.1:8000/"
  "%SMARTFILES_PYTHON%" -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
) else (
  echo SmartFiles is already running. Open: http://127.0.0.1:8000/
  echo API docs: http://127.0.0.1:8000/docs
  start "" "http://127.0.0.1:8000/"
)
