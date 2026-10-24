"""Extractive summaries based on document sentence frequency."""
import re
from collections import Counter

def summarize(text, n=3):
    text = (text or "").strip()
    if not text:
        return ""
    # Bound frequency scoring for long uploads while sampling every section.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) > 5]
    if len(text) <= 24000:
        return _summarize_sentences(sentences, text, n)
    sections, current, size = [], [], 0
    for sentence in sentences:
        if size + len(sentence) > 12000 and current:
            sections.append(" ".join(current)); current, size = [], 0
        current.append(sentence); size += len(sentence)
    if current:
        sections.append(" ".join(current))
    section_summaries = [_summarize_sentences(
        [s.strip() for s in re.split(r"(?<=[.!?])\s+", section) if len(s.split()) > 5], section, 2
    ) for section in sections]
    section_sentences = [[s.strip() for s in re.split(r"(?<=[.!?])\s+", value) if len(s.split()) > 5]
                         for value in section_summaries if value]
    # Keep at least one extractive sentence from each sampled section so a
    # long document's ending is not discarded just because its topic changes.
    chosen_sections = list(range(len(section_sentences)))
    if len(chosen_sections) > n:
        chosen_sections = sorted({round(i * (len(section_sentences) - 1) / max(1, n - 1)) for i in range(n)})
    representatives = [section_sentences[index][0] for index in chosen_sections if section_sentences[index]]
    condensed = " ".join(value for group in section_sentences for value in group)
    condensed_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", condensed) if len(s.split()) > 5]
    if len(representatives) >= n:
        return " ".join(representatives[:n])
    extra_text = _summarize_sentences(condensed_sentences, condensed, n)
    extras = [s.strip() for s in re.split(r"(?<=[.!?])\s+", extra_text)
              if s.strip() and all(s.strip() not in chosen for chosen in representatives)]
    return " ".join((representatives + extras)[:n])


def _summarize_sentences(sents, text, n):
    if len(sents) <= n: return " ".join(sents) or text[:300]
    f = Counter(re.findall(r"[a-z]{4,}", text.lower()))
    sc = [sum(f[w] for w in re.findall(r"[a-z]{4,}", s.lower())) / len(s.split()) for s in sents]
    top = sorted(sorted(range(len(sents)), key=lambda i: -sc[i])[:n])
    return " ".join(sents[i] for i in top)

