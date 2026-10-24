"""Local rule-based document categories and content tags."""
import re
from collections import Counter

CATEGORIES = (
    "Education & Assignments",
    "Career & Resumes",
    "Certificates",
    "Project Reports",
    "Technical Notes",
    "Financial Documents",
    "Presentations",
    "Images & Scanned Documents",
    "Other Documents",
)

_RULES = {
    "Education & Assignments": (
        "assignment", "coursework", "homework", "student", "students", "marks", "grades",
        "exam", "examination", "syllabus", "semester", "university", "college", "lecture",
        "academic", "thesis", "dissertation", "class", "curriculum", "research paper",
    ),
    "Career & Resumes": (
        "resume", "résumé", "curriculum vitae", "cv", "work experience", "employment history",
        "professional experience", "career objective", "job application", "cover letter", "skills summary",
    ),
    "Certificates": (
        "certificate", "certification", "certified", "credential", "award", "awarded to",
        "has successfully completed", "completion certificate", "diploma", "recognition",
    ),
    "Project Reports": (
        "project report", "project documentation", "project overview", "project objectives",
        "implementation", "methodology", "system architecture", "requirements analysis", "case study",
        "project conclusion", "project findings", "meeting report", "report",
    ),
    "Technical Notes": (
        "technical notes", "api", "database", "algorithm", "python", "javascript", "sql",
        "configuration", "debugging", "software", "server", "network", "code example", "command line",
    ),
    "Financial Documents": (
        "invoice", "receipt", "expense", "expenses", "budget", "revenue", "salary", "payroll",
        "bank statement", "account balance", "payment", "tax", "purchase order", "amount due", "financial",
        "employee records", "payroll", "compensation",
    ),
    "Presentations": (
        "presentation", "slide", "slides", "agenda", "speaker notes", "thank you for listening",
    ),
}

_FILENAME_HINTS = {
    "Education & Assignments": ("assignment", "coursework", "student marks", "syllabus", "exam"),
    "Career & Resumes": ("resume", "résumé", "curriculum vitae", "cover letter"),
    "Certificates": ("certificate", "certification", "diploma"),
    "Project Reports": ("project report", "project documentation", "meeting report"),
    "Technical Notes": ("technical notes", "developer notes", "api notes"),
    "Financial Documents": ("invoice", "receipt", "expense", "employee records", "payroll", "budget"),
    "Presentations": ("presentation", "slides", "slide deck"),
}

_TAGS = (
    "resume", "skills", "experience", "education", "assignment", "student", "marks", "research",
    "certificate", "award", "project", "architecture", "implementation", "database", "api", "python",
    "software", "meeting", "minutes", "finance", "expense", "budget", "invoice", "revenue", "employee",
    "presentation", "security", "design", "testing", "requirements", "schedule", "planning", "waterwise",
    "soil", "moisture", "sensor", "irrigation", "summary", "analysis", "report", "notes", "records",
)

STOP = set("this that with from have will your about which their there been were also into than then them they what when who not are and the for you was but can all any our has its this these those such very more most than each other file files document documents sample smartfiles fictional workbook pdf docx xlsx csv pptx txt jpg jpeg png image images".split())

_WORD_RE = re.compile(r"[a-z0-9]+(?:['’-][a-z0-9]+)?", re.I)
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value):
    return _NORMALIZE_RE.sub(" ", str(value or "").lower()).strip()


def _source(filename, text):
    return (str(filename or "") + "\n" + str(text or "")).lower()


def _match_normalized(normalized_source, phrase):
    normalized_phrase = _normalize(phrase)
    if not normalized_phrase:
        return 0
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(normalized_phrase) + r"(?![a-z0-9])", normalized_source))


def classify(text, kind, filename="", metadata=None):
    """Pick a useful category without downloads, external APIs, or model calls."""
    source = _source(filename, text)
    if metadata:
        source += "\n" + " ".join(str(value) for value in metadata.values()).lower()
    extension = str(filename or "").lower().rsplit(".", 1)[-1] if "." in str(filename or "") else ""
    normalized_source = _normalize(source)
    normalized_filename = _normalize(filename)

    if kind == "image" or extension in {"png", "jpg", "jpeg", "webp", "bmp", "gif"}:
        return "Images & Scanned Documents"
    if extension == "pptx":
        return "Presentations"

    scores = {}
    for category, phrases in _RULES.items():
        # Filename/title words are useful signals even when OCR or extraction is absent.
        scores[category] = sum(_match_normalized(normalized_source, phrase) + 2 * _match_normalized(normalized_filename, phrase)
                               for phrase in phrases)
        if any(_match_normalized(normalized_filename, hint) for hint in _FILENAME_HINTS.get(category, ())):
            scores[category] += 100
    best = max(scores, key=scores.get)
    if scores[best]:
        return best

    if extension in {"csv", "xlsx"}:
        if any(word in source for word in ("student", "mark", "grade", "course", "semester")):
            return "Education & Assignments"
        if any(word in source for word in ("employee", "staff", "salary", "payroll", "expense", "budget")):
            return "Financial Documents"
    return "Other Documents"


def keywords(text, n=6, filename=""):
    """Return concise content tags, with filename fallback for scanned/empty text."""
    source = _normalize(_source(filename, text))
    found = []
    for phrase in _TAGS:
        if _match_normalized(source, phrase):
            tag = phrase.title()
            if tag not in found:
                found.append(tag)

    counts = Counter(word.lower() for word in _WORD_RE.findall(source)
                     if len(word) > 3 and word.lower() not in STOP and not word.isdigit())
    for word, _ in counts.most_common(n * 2):
        tag = word.replace("'", "").title()
        if tag not in found:
            found.append(tag)
        if len(found) >= n:
            break
    return found[:n]
