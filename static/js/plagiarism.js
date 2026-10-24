/* Plagiarism check: upload, display verified passages and source links. */
const plagiarismFile = $("plagiarismFile");
const plagiarismText = $("plagiarismText");
const plagiarismScanButton = $("plagiarismScanButton");
const plagiarismProgress = $("plagiarismProgress");
const plagiarismNotice = $("plagiarismNotice");
const plagiarismReport = $("plagiarismReport");
let plagiarismBusy = false;

function plagiarismElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function plagiarismSafeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function plagiarismOnlineFailure(report) {
  const status = report.online_search_status;
  if (status === "unavailable_missing_api_key") return "Online checking is not configured. Set up the source-check service for SmartFiles and restart it.";
  if (status === "invalid_api_key") return "The source-check service rejected its API key. Check the key configuration and try again.";
  if (status === "rate_limited") return "The source-check service rate limit was reached. Wait briefly and try again.";
  if (status === "quota_exhausted") return "The source-check service has no API credits, so this document could not be checked for website sources. Add credits or use a previously saved completed report for this exact document.";
  if (status === "insufficient_text") return "At least 20 words are required to run an online plagiarism check.";
  if (status === "document_too_large") return "This document exceeds the online checker’s 50,000 word limit.";
  if (status === "provider_timeout") return "The online check is still processing. SmartFiles saved its report ID; retrying resumes this check without submitting it again.";
  if (status === "provider_error") return "The online checker could not return this report. Check your connection and service availability, then retry; SmartFiles will resume the saved report when possible.";
  if (status === "access_denied") return "The configured account does not have access to this API operation.";
  return "The online check could not be completed. Please try again.";
}

function plagiarismVerifiedMatches(report) {
  const verifiedUrls = new Set((Array.isArray(report.sources) ? report.sources : [])
    .filter(source => source && source.provider === "quetext" && source.verification === "provider_match")
    .map(source => plagiarismSafeUrl(source.url))
    .filter(Boolean));
  const found = [];
  const seen = new Set();
  for (const passageMatch of (Array.isArray(report.online_matches) ? report.online_matches : [])) {
    if (!passageMatch || typeof passageMatch.passage !== "string") continue;
    for (const source of (Array.isArray(passageMatch.sources) ? passageMatch.sources : [])) {
      if (!source || source.provider !== "quetext" || source.verification !== "provider_match") continue;
      const url = plagiarismSafeUrl(source && source.url);
      if (!url || !verifiedUrls.has(url)) continue;
      const passage = typeof source.matched_text === "string" && source.matched_text.trim() ? source.matched_text : passageMatch.passage;
      const key = `${passage}\n${url}\n${source.evidence}`;
      if (seen.has(key)) continue;
      seen.add(key);
      found.push({
        passage,
        evidence: typeof source.evidence === "string" ? source.evidence.trim() : "",
        title: typeof source.title === "string" && source.title.trim() ? source.title.trim() : new URL(url).hostname,
        url,
        matchType: source.match_type || passageMatch.match_type || "exact",
        similarity: Number.isFinite(Number(source.similarity)) && source.similarity !== null && source.similarity !== undefined
          ? Math.max(0, Math.min(1, Number(source.similarity))) : null,
        similarityPercent: Number.isFinite(Number(source.similarity_percent)) && source.similarity_percent !== null && source.similarity_percent !== undefined
          ? Math.max(0, Math.min(100, Number(source.similarity_percent))) : null
      });
    }
  }
  return found;
}

function renderPlagiarismResult(report) {
  plagiarismReport.replaceChildren();
  if (report.status === "failed") {
    plagiarismReport.appendChild(plagiarismElement("p", "notice error", "The plagiarism check could not be completed. Please try again."));
    return;
  }
  const matches = plagiarismVerifiedMatches(report);
  const overallScore = Number(report.quetext_score_percent);
  if (report.quetext_score_percent !== null && report.quetext_score_percent !== undefined && Number.isFinite(overallScore)) {
    plagiarismReport.appendChild(plagiarismElement("p", "notice", `Overall similarity score: ${Math.max(0, Math.min(100, overallScore)).toFixed(1)}%`));
  }
  const completeStatuses = ["completed", "completed_with_errors", "no_suitable_passages"];
  if (!completeStatuses.includes(report.online_search_status) && !matches.length) {
    return;
  }
  const incomplete = [
    "unavailable_missing_api_key", "invalid_api_key", "access_denied", "rate_limited",
    "quota_exhausted", "provider_error", "provider_timeout", "insufficient_text",
    "document_too_large", "completed_with_errors"
  ].includes(report.online_search_status);
  if (incomplete && !matches.length) {
    return;
  }
  if (!matches.length) {
    if (report.online_search_status === "no_suitable_passages") {
      plagiarismReport.appendChild(plagiarismElement("p", "notice", "No matching content was found."));
    } else {
      plagiarismReport.appendChild(plagiarismElement("p", "notice", "No matching website content was found."));
    }
    return;
  }

  if (incomplete) {
    plagiarismReport.appendChild(plagiarismElement("p", "notice error", "The online check was incomplete. Matches shown below were returned by the source-check service."));
  }
  const sources = new Map();
  matches.forEach(match => {
    if (!sources.has(match.url)) sources.set(match.url, {title: match.title, url: match.url, matches: []});
    sources.get(match.url).matches.push(match);
  });
  const list = plagiarismElement("div", "plagiarism-match-list");
  sources.forEach(source => {
    const card = plagiarismElement("article", "plagiarism-match-card");
    card.appendChild(plagiarismElement("h3", "plagiarism-section-title", "Website source"));
    card.appendChild(plagiarismElement("p", "", source.title));
    const link = plagiarismElement("a", "plagiarism-source-url", source.url);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.appendChild(link);
    source.matches.forEach(match => {
      card.appendChild(plagiarismElement("h4", "", "Topic passage from your document"));
      card.appendChild(plagiarismElement("blockquote", "plagiarism-passage-text", match.passage));
      card.appendChild(plagiarismElement("h4", "", "Matching source content"));
      if (match.evidence) {
        card.appendChild(plagiarismElement("blockquote", "plagiarism-passage-text", match.evidence));
      } else {
        card.appendChild(plagiarismElement("p", "muted", "No separate source excerpt was provided for this match."));
      }
      if (match.matchType === "approximate") {
        card.appendChild(plagiarismElement("small", "muted", "The source-check service reported a similar passage."));
      }
      if (match.similarityPercent !== null || match.similarity !== null) {
        const percent = match.similarityPercent !== null ? match.similarityPercent : match.similarity * 100;
        card.appendChild(plagiarismElement("p", "muted", `Similarity: ${Number(percent.toFixed(1))}%`));
      }
    });
    list.appendChild(card);
  });
  plagiarismReport.appendChild(list);
}

async function waitForPlagiarismResult(scanId) {
  let networkErrors = 0;
  while (true) {
    let report;
    try {
      report = await requestJSON(`${API.plagiarismReports}/${encodeURIComponent(scanId)}`);
      networkErrors = 0;
    } catch (error) {
      networkErrors += 1;
      if (networkErrors >= 8) throw new Error(`Could not reach SmartFiles to retrieve the scan result: ${error.message}`);
      await new Promise(resolve => window.setTimeout(resolve, Math.min(10000, 1000 * (2 ** (networkErrors - 1)))));
      continue;
    }
    if (["completed", "completed_with_warnings", "failed"].includes(report.status)) return report;
    const progress = Number(report.quetext_progress_percent);
    const progressText = Number.isFinite(progress) ? `Online checker is processing the report (${progress.toFixed(0)}%).` : "Online checker is processing the report.";
    plagiarismProgress.setAttribute("aria-valuetext", progressText);
    notice("plagiarismNotice", progressText);
    if (report.updated_at) {
      const updated = Date.parse(report.updated_at);
      if (Number.isFinite(updated) && Date.now() - updated > 20 * 60 * 1000) {
        throw new Error("SmartFiles has not updated this scan for 20 minutes. The backend worker may have stopped; start the check again to resume the saved report.");
      }
    }
    await new Promise(resolve => window.setTimeout(resolve, 2000));
  }
}

plagiarismScanButton.addEventListener("click", async () => {
  const selectedFile = plagiarismFile.files && plagiarismFile.files[0];
  const pastedText = plagiarismText ? plagiarismText.value.trim() : "";
  if (plagiarismBusy) return;
  if (selectedFile && pastedText) {
    notice("plagiarismNotice", "Choose a file or paste text, not both.", "error");
    return;
  }
  const file = selectedFile || (pastedText ? new File([pastedText], "pasted-text.txt", {type: "text/plain"}) : null);
  if (!file) {
    notice("plagiarismNotice", "Choose a PDF, DOCX, or TXT file, or paste text first.", "error");
    return;
  }
  if (!/\.(pdf|docx|txt)$/i.test(file.name || "")) {
    notice("plagiarismNotice", "Plagiarism checking supports PDF, DOCX, and TXT files.", "error");
    return;
  }

  plagiarismBusy = true;
  plagiarismScanButton.disabled = true;
  plagiarismScanButton.textContent = "Checking...";
  plagiarismProgress.classList.remove("hidden");
  plagiarismProgress.setAttribute("aria-valuetext", "Uploading document and checking for matches");
  plagiarismReport.replaceChildren();
  notice("plagiarismNotice", "Uploading document and checking for matches...");
  try {
    const form = new FormData();
    form.append("file", file);
    const uploaded = await requestJSON(API.upload, {method: "POST", body: form});
    if (!uploaded || !uploaded.id) throw new Error("The uploaded document could not be prepared for checking.");
    if (uploaded.text_extracted === false) {
      throw new Error(uploaded.processing_warning || "No readable text could be extracted from this file.");
    }
    if (typeof loadFiles === "function") await loadFiles();
    notice("plagiarismNotice", "Submitting the document to the online checker and retrieving its report...");
    const scan = await requestJSON(API.plagiarismScans, {
      method: "POST",
      body: JSON.stringify({file_id: uploaded.id, online_search: true})
    });
    const report = await waitForPlagiarismResult(scan.scan_id);
    renderPlagiarismResult(report);
    const hasMatches = plagiarismVerifiedMatches(report).length > 0;
    if (report.online_search_status === "completed" && hasMatches) {
      notice("plagiarismNotice", "Verified matching website content found.", "success");
    } else if (report.online_search_status === "completed") {
      notice("plagiarismNotice", "Check complete.");
    } else if (report.status === "completed_with_warnings" && hasMatches) {
      if (report.online_search_status === "completed") {
        notice("plagiarismNotice", "The online check completed. SmartFiles limited some local passage coverage.", "warning");
      } else {
        notice("plagiarismNotice", plagiarismOnlineFailure(report), "warning");
      }
    } else if (report.status !== "failed") {
      notice("plagiarismNotice", plagiarismOnlineFailure(report), "error");
    }
  } catch (error) {
    notice("plagiarismNotice", `Plagiarism check could not be completed: ${error.message}`, "error");
  } finally {
    plagiarismBusy = false;
    plagiarismProgress.classList.add("hidden");
    plagiarismProgress.setAttribute("aria-valuetext", "Scan in progress");
    plagiarismScanButton.disabled = false;
    plagiarismScanButton.textContent = "Check Plagiarism";
  }
});
