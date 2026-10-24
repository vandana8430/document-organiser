"""Build downloadable plagiarism reports from saved scan results."""
import io
import re
from importlib.resources import files
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Table,
    TableStyle,
)

BLUE = colors.HexColor("#2858ae")
NAVY = colors.HexColor("#18345f")
PALE_BLUE = colors.HexColor("#edf4ff")
PALE_RED = colors.HexColor("#fff0f2")
PALE_AMBER = colors.HexColor("#fff6e5")
PALE_GREEN = colors.HexColor("#eaf8f0")
PALE_GREY = colors.HexColor("#f1f3f6")
INK = colors.HexColor("#283b57")
MUTED = colors.HexColor("#64748b")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|session(?:[_ -]?token)?|password|passwd|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"\bfc-[A-Za-z0-9_-]{16,}\b"),
)
_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "code",
    "key", "password", "secret", "session", "sessionid", "signature", "token",
}


def _font_name():
    regular = files("reportlab").joinpath("fonts", "Vera.ttf")
    bold = files("reportlab").joinpath("fonts", "VeraBd.ttf")
    pdfmetrics.registerFont(TTFont("SmartFilesVera", str(regular)))
    pdfmetrics.registerFont(TTFont("SmartFilesVera-Bold", str(bold)))
    pdfmetrics.registerFontFamily(
        "SmartFilesVera",
        normal="SmartFilesVera",
        bold="SmartFilesVera-Bold",
        italic="SmartFilesVera",
        boldItalic="SmartFilesVera-Bold",
    )
    return pdfmetrics.getFont("SmartFilesVera")


def _safe_text(value, font):
    text = str(value if value is not None else "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    glyphs = font.face.charToGlyph
    return "".join(character if ord(character) in glyphs else "?" for character in text)


def _safe_url(value, font):
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
        query = urlencode([
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _SENSITIVE_QUERY_KEYS
            and not any(secret in key.casefold() for secret in ("token", "secret", "password", "session", "signature"))
        ])
        host = parts.hostname.lower()
        netloc = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        url = urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
        return _safe_text(url, font)
    except (TypeError, ValueError):
        return ""


def _paragraph(value, style, font):
    text = _safe_text(value, font)
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _display(value, font):
    return _safe_text(value, font).strip() or "Unavailable"


def _count(value, font):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,}"
    return _display(value, font) if value is not None else "Unavailable"


def _percentage(value, font):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.1f}%"
    return _display(value, font) if value is not None else "Unavailable"


def _source_by_url(report):
    return {
        source.get("url"): source
        for source in report.get("sources") or []
        if isinstance(source, dict) and isinstance(source.get("url"), str)
    }


def _local_matches(report):
    # An online plagiarism report should list websites returned by the provider,
    # not matches against another PDF or document in the account.
    if report.get("online_search_requested"):
        return
    fallback = report.get("local_fallback")
    if isinstance(fallback, dict) and fallback.get("status") == "completed":
        for source in fallback.get("matches", []):
            if not isinstance(source, dict):
                continue
            for match in source.get("matches", []):
                if isinstance(match, dict) and isinstance(match.get("passage"), str):
                    yield match["passage"], {
                        **match,
                        "document_name": source.get("document_name"),
                    }, match.get("match_type", "approximate")
        return

    matches = report.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if not isinstance(match, dict) or not isinstance(match.get("passage"), str):
                continue
            for key, match_type in (("exact_sources", "exact"), ("approximate_sources", "approximate")):
                sources = match.get(key)
                if not isinstance(sources, list):
                    continue
                for source in sources:
                    if isinstance(source, dict):
                        yield match["passage"], source, match_type
        return

    for source in report.get("local_matches") or []:
        if not isinstance(source, dict):
            continue
        for match in source.get("matches", []):
            if isinstance(match, dict) and isinstance(match.get("passage"), str):
                yield match["passage"], {
                    **match,
                    "document_name": source.get("document_name"),
                }, match.get("match_type", "approximate")


def _online_matches(report):
    if report.get("online_search_mode") != "quetext_report_v1":
        return
    sources = _source_by_url(report)
    for match in report.get("online_matches") or []:
        if not isinstance(match, dict) or not isinstance(match.get("passage"), str):
            continue
        for source in match.get("sources", []):
            if not isinstance(source, dict):
                continue
            page = sources.get(source.get("url"))
            if not isinstance(page, dict) or page.get("verification") != "provider_match":
                continue
            match_type = source.get("match_type", "approximate")
            yield source.get("matched_text") or match["passage"], source, page, match_type


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "SmartFilesTitle", parent=base["Title"], fontName="SmartFilesVera-Bold",
            fontSize=21, leading=26, textColor=NAVY, spaceAfter=5,
        ),
        "subtitle": ParagraphStyle(
            "SmartFilesSubtitle", parent=base["Normal"], fontName="SmartFilesVera",
            fontSize=9, leading=13, textColor=MUTED, spaceAfter=14,
        ),
        "section": ParagraphStyle(
            "SmartFilesSection", parent=base["Heading2"], fontName="SmartFilesVera-Bold",
            fontSize=13, leading=17, textColor=NAVY, spaceBefore=13, spaceAfter=7,
            keepWithNext=True,
        ),
        "match": ParagraphStyle(
            "SmartFilesMatch", parent=base["Heading3"], fontName="SmartFilesVera-Bold",
            fontSize=10, leading=14, textColor=NAVY, spaceBefore=9, spaceAfter=4,
            keepWithNext=True,
        ),
        "label": ParagraphStyle(
            "SmartFilesLabel", parent=base["Normal"], fontName="SmartFilesVera-Bold",
            fontSize=8, leading=11, textColor=MUTED, spaceBefore=5, spaceAfter=3,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "SmartFilesBody", parent=base["BodyText"], fontName="SmartFilesVera",
            fontSize=8.5, leading=12, textColor=INK, spaceAfter=5, splitLongWords=True,
            allowWidows=0, allowOrphans=0,
        ),
        "small": ParagraphStyle(
            "SmartFilesSmall", parent=base["BodyText"], fontName="SmartFilesVera",
            fontSize=7.5, leading=10, textColor=MUTED, spaceAfter=3, splitLongWords=True,
        ),
        "table_head": ParagraphStyle(
            "SmartFilesTableHead", parent=base["Normal"], fontName="SmartFilesVera-Bold",
            fontSize=8, leading=10, textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "SmartFilesTableCell", parent=base["Normal"], fontName="SmartFilesVera",
            fontSize=8, leading=10, textColor=INK, splitLongWords=True,
        ),
        "passage_exact": ParagraphStyle(
            "SmartFilesExactPassage", parent=base["BodyText"], fontName="SmartFilesVera",
            fontSize=8.5, leading=12, textColor=INK, backColor=PALE_RED,
            borderColor=colors.HexColor("#efc2c8"), borderWidth=0.5, borderPadding=7,
            spaceAfter=7, splitLongWords=True, allowWidows=0, allowOrphans=0,
        ),
        "passage_approximate": ParagraphStyle(
            "SmartFilesApproxPassage", parent=base["BodyText"], fontName="SmartFilesVera",
            fontSize=8.5, leading=12, textColor=INK, backColor=PALE_AMBER,
            borderColor=colors.HexColor("#f0d49b"), borderWidth=0.5, borderPadding=7,
            spaceAfter=7, splitLongWords=True, allowWidows=0, allowOrphans=0,
        ),
        "link": ParagraphStyle(
            "SmartFilesLink", parent=base["BodyText"], fontName="SmartFilesVera",
            fontSize=7.5, leading=10, textColor=BLUE, spaceAfter=4, splitLongWords=True,
        ),
        "footer": ParagraphStyle(
            "SmartFilesFooter", parent=base["Normal"], fontName="SmartFilesVera",
            fontSize=7, textColor=MUTED,
        ),
    }


def build_plagiarism_pdf(report):
    """Return a PDF containing only fields from a saved, owner-scoped scan report."""
    font = _font_name()
    styles = _styles()
    output = io.BytesIO()

    class ReportDocument(BaseDocTemplate):
        def __init__(self, stream):
            super().__init__(
                stream,
                pagesize=letter,
                leftMargin=0.68 * inch,
                rightMargin=0.68 * inch,
                topMargin=0.78 * inch,
                bottomMargin=0.62 * inch,
                title="SmartFiles Plagiarism Report",
                author="SmartFiles",
                subject="Saved plagiarism scan results",
            )
            frame = Frame(
                self.leftMargin, self.bottomMargin, self.width, self.height,
                id="report-content", leftPadding=0, rightPadding=0,
                topPadding=0, bottomPadding=0,
            )
            self.addPageTemplates(PageTemplate(id="report", frames=[frame], onPage=self._page_header_footer))

        def _page_header_footer(self, canvas, doc):
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor("#dce5f2"))
            canvas.setLineWidth(0.6)
            canvas.line(doc.leftMargin, letter[1] - 0.52 * inch, letter[0] - doc.rightMargin, letter[1] - 0.52 * inch)
            canvas.setFont("SmartFilesVera-Bold", 8)
            canvas.setFillColor(BLUE)
            canvas.drawString(doc.leftMargin, letter[1] - 0.42 * inch, "SMARTFILES  |  PLAGIARISM REPORT")
            canvas.setStrokeColor(colors.HexColor("#dce5f2"))
            canvas.line(doc.leftMargin, 0.48 * inch, letter[0] - doc.rightMargin, 0.48 * inch)
            canvas.setFont("SmartFilesVera", 7)
            canvas.setFillColor(MUTED)
            canvas.drawRightString(letter[0] - doc.rightMargin, 0.31 * inch, f"Page {doc.page}")
            canvas.restoreState()

    document = ReportDocument(output)
    story = [
        _paragraph("Plagiarism Scan Report", styles["title"], font),
        _paragraph(
            "Generated from the saved SmartFiles scan report. Similarity is a review signal, not a plagiarism verdict.",
            styles["subtitle"], font,
        ),
    ]
    document_name = _display(report.get("document_name"), font)
    scan_date = _display(report.get("scan_date"), font)
    status = _display(report.get("status"), font)

    metrics = [
        ("Document", document_name),
        ("Scan date", scan_date),
        ("Scan status", status),
        ("Analyzed words", _count(report.get("eligible_words_analyzed"), font)),
        ("Local exact passages", _count(report.get("exact_match_count"), font)),
        ("Local approximate-only passages", _count(report.get("approximate_match_count"), font)),
        (
            "Online exact passages",
            _count(report.get("online_exact_match_count"), font)
            if report.get("online_search_requested") else "Not requested",
        ),
        (
            "Online approximate passages",
            _count(report.get("online_approximate_match_count"), font)
            if report.get("online_search_requested") else "Not requested",
        ),
        (
            "Online pages checked",
            _count(report.get("online_pages_analyzed"), font)
            if report.get("online_search_requested") else "Not requested",
        ),
    ]
    table_data = [[
        _paragraph("Summary metric", styles["table_head"], font),
        _paragraph("Value", styles["table_head"], font),
        _paragraph("Summary metric", styles["table_head"], font),
        _paragraph("Value", styles["table_head"], font),
    ]]
    for index in range(0, len(metrics), 2):
        left = metrics[index]
        right = metrics[index + 1] if index + 1 < len(metrics) else ("", "")
        table_data.append([
            _paragraph(left[0], styles["table_cell"], font),
            _paragraph(left[1], styles["table_cell"], font),
            _paragraph(right[0], styles["table_cell"], font),
            _paragraph(right[1], styles["table_cell"], font),
        ])
    summary = Table(table_data, colWidths=[1.42 * inch, 1.38 * inch, 1.42 * inch, 1.38 * inch], repeatRows=1, hAlign="LEFT")
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_BLUE]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dce5f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([
        _paragraph("Scan summary", styles["section"], font),
        summary,
        _paragraph("Coverage and limitations", styles["section"], font),
    ])

    analyzed = report.get("analyzed_passages")
    skipped = report.get("skipped_passages")
    local_passages = report.get("matches")
    if (
        isinstance(analyzed, int) and not isinstance(analyzed, bool) and analyzed >= 0
        and isinstance(skipped, int) and not isinstance(skipped, bool) and skipped >= 0
        and isinstance(local_passages, list)
    ):
        matched_texts = {
            match.get("passage") for match in local_passages
            if isinstance(match, dict) and isinstance(match.get("passage"), str)
        }
        matched_count = len(matched_texts)
        if matched_count <= analyzed:
            checked_clear = analyzed - matched_count
            total = analyzed + skipped
            coverage_rows = [
                [
                    _paragraph("Local passage coverage", styles["table_head"], font),
                    _paragraph("Passages", styles["table_head"], font),
                    _paragraph("Share of eligible + skipped local passages", styles["table_head"], font),
                ],
            ]
            for label, value in (
                ("Matched locally", matched_count),
                ("Checked, no local match detected", checked_clear),
                ("Unchecked locally", skipped),
            ):
                share = f"{100 * value / total:.1f}%" if total else "Unavailable"
                coverage_rows.append([
                    _paragraph(label, styles["table_cell"], font),
                    _paragraph(_count(value, font), styles["table_cell"], font),
                    _paragraph(share, styles["table_cell"], font),
                ])
            coverage_table = Table(coverage_rows, colWidths=[2.25 * inch, 1.0 * inch, 2.35 * inch], repeatRows=1, hAlign="LEFT")
            coverage_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BLUE),
                ("BACKGROUND", (0, 1), (-1, 1), PALE_RED),
                ("BACKGROUND", (0, 2), (-1, 2), PALE_GREEN),
                ("BACKGROUND", (0, 3), (-1, 3), PALE_GREY),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dce5f2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(coverage_table)
        else:
            story.append(_paragraph("Passage coverage counts were inconsistent and could not be summarized.", styles["body"], font))
    else:
        story.append(_paragraph("Passage coverage metrics are unavailable in this saved report.", styles["body"], font))

    limitations = [
        f"Local comparison analyzed {_count(analyzed, font)} passages; "
        f"{_count(skipped, font)} eligible passages were skipped or not analyzed locally.",
    ]
    if report.get("online_search_requested"):
        limitations.append(
            f"Online check status: {_display(report.get('online_search_status'), font)}. "
            f"Source pages returned: {_count(report.get('online_pages_analyzed'), font)}."
        )
        if isinstance(report.get("local_fallback"), dict):
            limitations.append("Free local fallback compared saved SmartFiles documents; no website search was performed.")
    else:
        limitations.append("Online source discovery was not requested.")
    if report.get("sample_data"):
        limitations.append("This report is based on fictional demo/test data.")
    for message in limitations:
        story.append(_paragraph(f"• {message}", styles["body"], font))
    if isinstance(report.get("errors"), list):
        for error in report["errors"]:
            story.append(_paragraph(f"• {error}", styles["body"], font))
    if report.get("method"):
        story.append(_paragraph("Method", styles["label"], font))
        story.append(_paragraph(report["method"], styles["small"], font))
    if report.get("disclaimer"):
        story.append(_paragraph(report["disclaimer"], styles["small"], font))

    story.append(_paragraph("Matching passages", styles["section"], font))
    local_count = 0
    for passage, source, match_type in _local_matches(report):
        if match_type not in {"exact", "approximate"}:
            continue
        local_count += 1
        exact = match_type == "exact"
        evidence_style = styles["passage_exact" if exact else "passage_approximate"]
        header = _paragraph(
            f"Local match {local_count} - {'Exact' if exact else 'Approximate'}",
            styles["match"], font,
        )
        source_name = _display(source.get("document_name"), font)
        score = source.get("word_jaccard")
        details = f"Source document: {source_name}"
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            details += f"  |  Word-set Jaccard similarity: {_percentage(score * 100, font)}"
        story.append(KeepTogether([header, _paragraph(details, styles["small"], font)]))
        story.append(_paragraph("Original document passage", styles["label"], font))
        story.append(_paragraph(passage, evidence_style, font))
        story.append(_paragraph("Matching passage in uploaded document", styles["label"], font))
        story.append(_paragraph(source.get("evidence"), evidence_style, font))

    online_count = 0
    for passage, source, page, match_type in _online_matches(report):
        online_count += 1
        exact = match_type == "exact"
        evidence_style = styles["passage_exact" if exact else "passage_approximate"]
        url = _safe_url(source.get("url"), font)
        source_name = _display(source.get("title") or page.get("title"), font)
        domain = _display(page.get("domain"), font)
        details = f"Source: {source_name}  |  Domain: {domain}"
        score = source.get("word_jaccard")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            details += f"  |  Word-set Jaccard similarity: {_percentage(score * 100, font)}"
        header = _paragraph(
            f"Online match {online_count} - {'Exact' if exact else 'Approximate'}",
            styles["match"], font,
        )
        story.append(KeepTogether([header, _paragraph(details, styles["small"], font)]))
        if url:
            escaped_url = escape(url, {'"': "&quot;"})
            story.append(Paragraph(f'<link href="{escaped_url}" color="#2858ae">{escape(url)}</link>', styles["link"]))
        story.append(_paragraph("Original document passage", styles["label"], font))
        story.append(_paragraph(passage, evidence_style, font))
        story.append(_paragraph("Matching passage in retrieved webpage text", styles["label"], font))
        story.append(_paragraph(source.get("evidence"), evidence_style, font))

    if not local_count and not online_count:
        story.append(_paragraph("No matching passages were saved in this scan report.", styles["body"], font))
    story.append(_paragraph("Possible sources (not confirmed matches)", styles["section"], font))
    matching_urls = {
        source.get("url")
        for _, source, _, _ in _online_matches(report)
        if isinstance(source.get("url"), str)
    }
    candidates = [
        source for source in report.get("sources") or []
        if isinstance(source, dict) and source.get("url") not in matching_urls
    ]
    if not candidates:
        story.append(_paragraph("No additional possible sources were saved.", styles["body"], font))
    else:
        for index, source in enumerate(candidates, start=1):
            source_title = _display(source.get("title"), font)
            domain = _display(source.get("domain"), font)
            verification = (
                "The online checker returned a matching passage from this source."
                if source.get("verification") == "provider_match"
                else "No matching passage was verified for this source."
            )
            story.append(KeepTogether([
                _paragraph(f"Possible source {index}: {source_title}", styles["match"], font),
                _paragraph(f"Domain: {domain}. {verification}", styles["small"], font),
            ]))
            url = _safe_url(source.get("url"), font)
            if url:
                escaped_url = escape(url, {'"': "&quot;"})
                story.append(Paragraph(f'<link href="{escaped_url}" color="#2858ae">{escape(url)}</link>', styles["link"]))
            if source.get("evidence"):
                story.append(_paragraph("Returned search/page text excerpt", styles["label"], font))
                story.append(_paragraph(source["evidence"], styles["small"], font))

    document.build(story)
    return output.getvalue()
