/* SmartFiles documents feature handlers. */
const categoryChoices = [
  "Education & Assignments", "Career & Resumes", "Certificates", "Project Reports",
  "Technical Notes", "Financial Documents", "Presentations", "Images & Scanned Documents", "Other Documents"
];
let documentTypeFilter = "";

function updateCategoryChoices(available) {
  const filter = $("documentCategoryFilter");
  if (!filter) return;
  const selected = filter.value;
  const choices = [...new Set([...categoryChoices, ...(available || []), ...documents.map(file => file.category).filter(Boolean)])];
  filter.replaceChildren(new Option("All categories", ""));
  choices.sort((a, b) => a.localeCompare(b)).forEach(category => filter.add(new Option(category, category)));
  if (choices.includes(selected)) filter.value = selected;
}

function prettyDate(value) {
  if (!value) return "Unknown";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function extractionStatusLabel(status) {
  return ({indexed: "Text indexed", extracted: "Text extracted; index incomplete",
    failed: "Extraction failed", no_text: "No readable text", needs_attention: "Needs reprocessing",
    unknown: "Status unavailable"})[status] || "Status unavailable";
}

async function openDocument(id, returnView = "documents") {
  if (id == null) {
    notice("apiNotice", "This result has no document ID, so it cannot be opened.", "error");
    return;
  }
  previousView = returnView;
  searchPanelWasOpen = !$("searchResultsPanel").classList.contains("hidden");
  $("searchResultsPanel").classList.add("hidden");
  showView("detail");
  notice("detailNotice", "Loading document…");
  $("documentDetail").replaceChildren();
  const download = $("downloadDocument");
  download.classList.add("hidden");
  download.removeAttribute("href");
  $("deleteDocumentButton").disabled = true;
  $("documentDetail").removeAttribute("data-file-id");

  try {
    const file = await requestJSON(`/api/files/${encodeURIComponent(id)}`);
    $("documentDetail").dataset.fileId = file.id;
    $("deleteDocumentButton").disabled = false;
    const name = file.name || "Untitled document";
    const ext = name.split(".").pop().toLowerCase();
    const rawURL = `/api/files/${encodeURIComponent(id)}/raw`;
    download.href = rawURL + "?dl=1";
    download.download = name;
    download.classList.remove("hidden");

    const root = $("documentDetail");
    const title = document.createElement("h2");
    title.textContent = name;
    root.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "detail-meta";
    [["Type", file.file_type || file.kind || ext.toUpperCase()], ["Category", file.category || "Other Documents"],
     ["Uploaded", prettyDate(file.created)], ["Size", file.size_bytes == null ? "Unavailable" : `${(file.size_bytes / 1024).toFixed(1)} KB`],
     ["Record", file.is_demo ? "Fictional sample data" : "Uploaded document"],
     ["Text status", extractionStatusLabel(file.extraction_status)],
     ["Opened", String(file.opened ?? 0)],
     ["Tags", Array.isArray(file.tags) && file.tags.length ? file.tags.join(", ") : "None"]]
      .forEach(([label, value]) => {
        const cell = document.createElement("div");
        const small = document.createElement("small"); small.textContent = label;
        const valueEl = document.createElement("strong"); valueEl.textContent = value;
        if (label === "Category") valueEl.id = "detailCategoryValue";
        cell.append(small, valueEl); meta.appendChild(cell);
      });
    root.appendChild(meta);

    if (file.extraction_error) {
      const extractionNotice = document.createElement("p");
      extractionNotice.className = "notice error";
      extractionNotice.textContent = `${extractionStatusLabel(file.extraction_status)}: ${file.extraction_error} Use Repair text index to retry extraction.`;
      root.appendChild(extractionNotice);
    }

    const categoryEditor = document.createElement("div");
    categoryEditor.className = "category-editor";
    const editorLabel = document.createElement("label"); editorLabel.htmlFor = "detailCategory";
    editorLabel.textContent = "Change category";
    const editorSelect = document.createElement("select"); editorSelect.id = "detailCategory";
    categoryChoices.forEach(category => editorSelect.add(new Option(category, category)));
    if (!categoryChoices.includes(file.category)) editorSelect.add(new Option(file.category, file.category));
    editorSelect.value = file.category || "Other Documents";
    const saveCategory = document.createElement("button");
    saveCategory.type = "button"; saveCategory.className = "secondary"; saveCategory.textContent = "Save category";
    saveCategory.disabled = !categoryChoices.includes(editorSelect.value);
    editorSelect.addEventListener("change", () => { saveCategory.disabled = !categoryChoices.includes(editorSelect.value); });
    saveCategory.addEventListener("click", () => saveDocumentCategory(file.id, editorSelect.value));
    categoryEditor.append(editorLabel, editorSelect, saveCategory);
    root.appendChild(categoryEditor);

    const visibleTags = Array.isArray(file.tags) ? file.tags.filter(tagName => !["sample", "demo", "duplicate"].includes(String(tagName).toLowerCase())) : [];
    if (visibleTags.length) {
      const tagsPanel = document.createElement("div"); tagsPanel.className = "document-tags";
      const tagsHeading = document.createElement("strong"); tagsHeading.textContent = "Tags";
      tagsPanel.appendChild(tagsHeading);
      visibleTags.forEach(tagName => {
        const tag = document.createElement("span"); tag.className = "topic-tag"; tag.textContent = tagName;
        tagsPanel.appendChild(tag);
      });
      root.appendChild(tagsPanel);
    }

    if (file.summary) {
      const summary = document.createElement("div"); summary.className = "panel";
      const heading = document.createElement("h3"); heading.textContent = "Summary";
      const body = document.createElement("p"); body.textContent = file.summary;
      summary.append(heading, body); root.appendChild(summary);
    }

    const preview = document.createElement("div"); preview.className = "document-preview";
    if (ext === "pdf") {
      const frame = document.createElement("iframe");
      frame.src = rawURL; frame.title = "PDF preview: " + name;
      preview.appendChild(frame);
    } else if (["png", "jpg", "jpeg", "webp", "bmp", "gif"].includes(ext)) {
      const image = document.createElement("img");
      image.src = rawURL; image.alt = name;
      image.onerror = () => { preview.textContent = "The image preview could not be loaded. Use Download to open the file."; };
      preview.appendChild(image);
    } else if (["txt", "md", "csv", "docx", "xlsx", "pptx"].includes(ext)) {
      const pre = document.createElement("pre");
      pre.textContent = file.text || "No text could be extracted from this file.";
      preview.appendChild(pre);
      if (!file.text) {
        const hint = document.createElement("p"); hint.className = "muted";
        hint.textContent = "Text extraction was unavailable for this file. You can still download the original.";
        preview.appendChild(hint);
      }
      if ((file.text || "").length >= 5000) {
        const hint = document.createElement("p"); hint.className = "muted";
        hint.textContent = "Preview is limited to the first 5,000 characters. Download the file to view all content.";
        preview.appendChild(hint);
      }
    } else if (["mp4", "mkv", "mov", "avi", "webm"].includes(ext)) {
      const video = document.createElement("video");
      video.controls = true; video.preload = "metadata"; video.src = rawURL;
      video.style.maxWidth = "100%"; preview.appendChild(video);
    } else {
      preview.className = "empty";
      preview.textContent = "Preview is not available for this format. Download the document to open it.";
    }
    root.appendChild(preview);
    notice("detailNotice", "");
  } catch (error) {
    download.classList.add("hidden");
    notice("detailNotice", error.message.includes("404") || error.message.toLowerCase().includes("not found")
      ? "This document is missing or was deleted. Refresh your document list."
      : "Could not open this document: " + error.message + ". Check that you are signed in and the backend is running.", "error");
  }
}

$("detailBack").addEventListener("click", () => {
  showView(previousView);
  if (searchPanelWasOpen && $("searchInput").value.trim()) {
    $("searchResultsPanel").classList.remove("hidden");
  }
  searchPanelWasOpen = false;
  if (previousView === "documents" || previousView === "home") loadFiles();
});

document.querySelectorAll(".nav-btn[data-view]").forEach(btn => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});
$("topUpload").addEventListener("click", () => showView("upload"));
$("viewAll").addEventListener("click", () => showView("documents"));
$("refreshFiles").addEventListener("click", loadFiles);
$("reprocessDocuments").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Repairing index...";
  notice("reprocessNotice", "Re-extracting documents with missing text or search chunks...");
  try {
    const result = await requestJSON(API.reprocess, {method: "POST"});
    await loadFiles();
    const names = result.processed.map(item => item.name).join(", ");
    const failures = result.errors.map(item => `${item.name}: ${item.error}`).join("; ");
    let message = `${result.counts.processed} document(s) reprocessed; ${result.counts.skipped} already indexed.`;
    if (names) message += ` Recovered: ${names}.`;
    if (failures) message += ` Still unavailable: ${failures}`;
    notice("reprocessNotice", message, result.counts.errors ? "error" : "success");
  } catch (error) {
    notice("reprocessNotice", "Could not repair the text index: " + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Repair text index";
  }
});

$("deleteDocumentButton").addEventListener("click", async () => {
  const id = $("documentDetail").dataset.fileId;
  if (!id) return;
  const name = $("documentDetail").querySelector("h2")?.textContent || "this document";
  if (!window.confirm(`Delete ${name} from your workspace?`)) return;
  const button = $("deleteDocumentButton");
  button.disabled = true;
  notice("detailNotice", "Deleting document...");
  try {
    await requestJSON(`/api/files/${encodeURIComponent(id)}`, {method: "DELETE"});
    $("documentDetail").replaceChildren();
    showView(previousView === "home" ? "home" : "documents");
    await loadFiles();
    notice("apiNotice", `Deleted ${name}.`, "success");
  } catch (error) {
    notice("detailNotice", "Could not delete document: " + error.message, "error");
    button.disabled = false;
  }
});
function fileType(file) {
  const name = (file.name || "").toLowerCase();
  const type = (file.type || "").toLowerCase();
  if (name.endsWith(".pdf") || type.includes("pdf")) return "PDF";
  if (name.endsWith(".docx") || type.includes("word")) return "Word";
  if (/\.(xlsx|csv)$/.test(name)) return "Spreadsheet";
  if (name.endsWith(".pptx")) return "PowerPoint";
  if (/\.(png|jpe?g|webp|gif)$/.test(name) || type.startsWith("image/")) return "Image";
  if (name.endsWith(".txt") || type.startsWith("text/")) return "Text";
  return "Other";
}

function iconFor(type) {
  return ({PDF:"PDF", Word:"DOC", Spreadsheet:"XLS", PowerPoint:"PPT", Image:"IMG", Text:"TXT", Other:"FILE"})[type] || "FILE";
}

function filteredDocuments() {
  const category = $("documentCategoryFilter")?.value || "";
  const sort = $("documentSort")?.value || "newest";
  let list = documents.filter(file => {
    const type = fileType(file);
    const typeMatch = !documentTypeFilter || (documentTypeFilter === "Other"
      ? !["PDF", "Word", "Image"].includes(type) : type === documentTypeFilter);
    return (!category || file.category === category) && typeMatch;
  });
  list = [...list];
  if (sort === "oldest") list.sort((a, b) => Number(a.created || 0) - Number(b.created || 0));
  else if (sort === "name-asc") list.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  else if (sort === "name-desc") list.sort((a, b) => (b.name || "").localeCompare(a.name || ""));
  else if (sort === "category") list.sort((a, b) => (a.category || "").localeCompare(b.category || "") || (a.name || "").localeCompare(b.name || ""));
  else list.sort((a, b) => Number(b.created || 0) - Number(a.created || 0));
  return list;
}

function renderFiles() {
  $("totalCount").textContent = documents.length;
  $("pdfCount").textContent = documents.filter(f => fileType(f) === "PDF").length;
  $("wordCount").textContent = documents.filter(f => fileType(f) === "Word").length;
  $("otherCount").textContent = documents.filter(f => !["PDF","Word"].includes(fileType(f))).length;

  const targets = [$("recentFiles"), $("documentFiles")];
  targets.forEach((target, index) => {
    target.replaceChildren();
    const rows = index === 0
      ? [...documents].sort((a, b) => Number(b.created || 0) - Number(a.created || 0)).slice(0, 5)
      : filteredDocuments();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = documents.length
        ? "No documents match the selected category or filters."
        : API.files
        ? "No documents found. Upload a document to get started."
        : "Your documents will appear here once the document-list API is connected.";
      target.appendChild(empty);
      return;
    }

    rows.forEach(file => {
      const type = fileType(file);
      const row = document.createElement("div");
      row.className = "file-row";

      const icon = document.createElement("div");
      icon.className = "file-icon file-icon-" + type.toLowerCase();
      icon.textContent = iconFor(type);

      const info = document.createElement("div");
      info.className = "file-info";
      const name = document.createElement("button");
      name.type = "button";
      name.className = "file-open";
      name.textContent = file.name || file.filename || "Untitled document";
      name.addEventListener("click", event => { event.stopPropagation(); openDocument(file.id, index === 0 ? "home" : "documents"); });
      const meta = document.createElement("small");
      const size = file.size_bytes == null ? "" : `${(Number(file.size_bytes) / 1024).toFixed(1)} KB`;
      meta.textContent = [file.file_type || type, file.category || "", file.created ? prettyDate(file.created) : "", size].filter(Boolean).join(" | ");
      info.append(name, meta);
      const visibleTags = Array.isArray(file.tags) ? file.tags.filter(tagName => !["sample", "demo", "duplicate"].includes(String(tagName).toLowerCase())) : [];
      if (visibleTags.length) {
        const topics = document.createElement("div"); topics.className = "file-topic-tags";
        visibleTags.slice(0, 4).forEach(tagName => {
          const topic = document.createElement("span"); topic.className = "topic-tag";
          topic.textContent = tagName; topics.appendChild(topic);
        });
        info.appendChild(topics);
      }

      const labels = document.createElement("div"); labels.className = "file-labels";
      const categoryTag = document.createElement("span"); categoryTag.className = "tag category-tag";
      categoryTag.textContent = file.category || "Other Documents";
      labels.appendChild(categoryTag);
      if (file.is_demo) {
        const demoTag = document.createElement("span"); demoTag.className = "tag demo-tag";
        demoTag.textContent = "Sample data"; labels.appendChild(demoTag);
      }
      const extractionTag = document.createElement("span");
      extractionTag.className = "tag";
      extractionTag.textContent = extractionStatusLabel(file.extraction_status);
      if (file.extraction_error) extractionTag.title = file.extraction_error;
      labels.appendChild(extractionTag);
      row.append(icon, info, labels);
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", "Open " + (file.name || file.filename || "document"));
      row.addEventListener("click", () => openDocument(file.id, index === 0 ? "home" : "documents"));
      row.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDocument(file.id, index === 0 ? "home" : "documents"); }
      });
      target.appendChild(row);
    });
  });
}

async function saveDocumentCategory(id, category) {
  const button = $("documentDetail").querySelector(".category-editor button");
  if (button) { button.disabled = true; button.textContent = "Saving..."; }
  try {
    const result = await requestJSON(`/api/files/${encodeURIComponent(id)}/category`, {
      method: "PATCH", body: JSON.stringify({category})
    });
    const file = documents.find(item => item.id === id);
    if (file) file.category = result.category;
    const value = $("detailCategoryValue");
    if (value) value.textContent = result.category;
    updateCategoryChoices();
    renderFiles();
    await loadDashboard();
    notice("detailNotice", "Category updated.", "success");
  } catch (error) {
    notice("detailNotice", "Could not update category: " + error.message, "error");
  } finally {
    if (button) { button.disabled = false; button.textContent = "Save category"; }
  }
}

async function loadFiles() {
  notice("apiNotice", "Loading documents...");
  try {
    const data = await requestJSON(API.files);
    const listedFiles = Array.isArray(data) ? data : data?.files || data?.documents;
    if (!Array.isArray(listedFiles)) {
      throw new Error("The document list response was not in the expected format.");
    }
    documents = listedFiles;
    updateCategoryChoices();
    renderFiles();
    renderCompareOptions(documents);
    renderAssistantOptions(documents);
    if (typeof renderPlagiarismOptions === "function") renderPlagiarismOptions(documents);
    if (typeof renderCompressionOptions === "function") renderCompressionOptions(documents);
    notice("apiNotice", "");
    await loadDashboard();
  } catch (error) {
    if (typeof renderPlagiarismOptions === "function") {
      renderPlagiarismOptions([], `Could not load documents: ${error.message}`);
    }
    notice("apiNotice", "Could not load documents: " + error.message, "error");
  }
}

async function uploadDocument(file) {
  const form = new FormData();
  form.append("file", file);
  return requestJSON(API.upload, {
    method: "POST",
    body: form
  });
}

function renderAssistantOptions(list) {
  const select = $("assistantDocument");
  if (!select) return;
  const previous = select.value;
  select.replaceChildren(new Option("All my documents", ""));
  list.forEach(file => select.add(new Option(file.name || `Document ${file.id}`, String(file.id))));
  if (list.some(file => String(file.id) === previous)) select.value = previous;
}

function renderCompareOptions(list) {
  const first = $("compareA"), second = $("compareB"), button = $("compareButton");
  const previousFirst = first.value, previousSecond = second.value;
  [first, second].forEach(select => {
    select.replaceChildren();
    list.forEach(file => {
      const option = document.createElement("option");
      option.value = String(file.id);
      option.textContent = file.name || `Document ${file.id}`;
      select.appendChild(option);
    });
  });
  if (list.some(file => String(file.id) === previousFirst)) first.value = previousFirst;
  if (list.some(file => String(file.id) === previousSecond)) second.value = previousSecond;
  if (list.length > 1 && first.value === second.value) second.selectedIndex = 1;
  first.disabled = second.disabled = list.length < 2;
  button.disabled = list.length < 2;
}

async function loadDashboard() {
  const categories = $("categoryStats");
  categories.replaceChildren();
  try {
    const query = typeof analyticsQuery === "function" ? analyticsQuery().toString() : "";
    const stats = await requestJSON(query ? `${API.dashboard}?${query}` : API.dashboard);
    if (typeof renderAnalytics === "function") renderAnalytics(stats);
    $("demoCount").textContent = Number(stats.demo_total || 0) === 1
      ? "1 sample document loaded"
      : `${Number(stats.demo_total || 0)} sample documents loaded`;
    const extensions = stats.extensions || {};
    const pdfCount = Number(extensions[".pdf"] || 0);
    const wordCount = Number(extensions[".doc"] || 0) + Number(extensions[".docx"] || 0);
    $("totalCount").textContent = String(stats.total || 0);
    $("pdfCount").textContent = String(pdfCount);
    $("wordCount").textContent = String(wordCount);
    $("otherCount").textContent = String(Math.max(0, Number(stats.total || 0) - pdfCount - wordCount));
    const processing = stats.processing || {};
    $("processedCount").textContent = String(Number(processing.indexed || 0) + Number(processing.extracted || 0));
    $("attentionCount").textContent = String(Number(processing.failed || 0) + Number(processing.needs_attention || 0));
    const duplicateCounts = stats.duplicates || {};
    $("dashboardExactCount").textContent = String(Number(duplicateCounts.exact_groups || 0));
    $("dashboardNearCount").textContent = String(Number(duplicateCounts.potential_near_pairs || 0));
    updateCategoryChoices(stats.category_options || []);
    const entries = Object.entries(stats.categories || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Category totals will appear after you upload or import documents.";
      categories.appendChild(empty);
    } else {
      entries.forEach(([name, count]) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "category-stat";
        item.setAttribute("aria-label", `Filter documents by ${name}`);
        item.textContent = `${name}: ${count}`;
        item.addEventListener("click", () => {
          documentTypeFilter = "";
          $("documentCategoryFilter").value = name;
          showView("documents");
          renderFiles();
        });
        categories.appendChild(item);
      });
    }
  } catch (error) {
    notice("apiNotice", "Could not load dashboard statistics: " + error.message, "error");
  }
}
document.querySelectorAll(".category").forEach(button => {
  button.addEventListener("click", () => {
    documentTypeFilter = button.dataset.category;
    $("documentCategoryFilter").value = "";
    showView("documents");
    renderFiles();
  });
});

$("documentCategoryFilter").addEventListener("change", () => { documentTypeFilter = ""; renderFiles(); });
$("documentSort").addEventListener("change", renderFiles);
$("clearDocumentFilters").addEventListener("click", () => {
  documentTypeFilter = "";
  $("documentCategoryFilter").value = "";
  $("documentSort").value = "newest";
  renderFiles();
});

$("loadDemoDocuments").addEventListener("click", async () => {
  const button = $("loadDemoDocuments");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Loading samples...";
  notice("demoNotice", "Loading and processing the sample documents...");
  try {
    const result = await requestJSON(API.demoImport, {method: "POST"});
    await loadFiles();
    let message = `Added ${result.imported} sample document${result.imported === 1 ? "" : "s"}; ${result.skipped} already loaded.`;
    if (result.warnings?.length) message += ` ${result.warnings.length} file(s) have extraction warnings.`;
    if (result.errors?.length) message += ` ${result.errors.length} file(s) could not be imported.`;
    notice("demoNotice", message, result.errors?.length || result.warnings?.length ? "error" : "success");
  } catch (error) {
    notice("demoNotice", "Could not load sample documents: " + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});

$("uploadButton").addEventListener("click", async () => {
  const files = $("fileInput").files;
  if (!files.length) {
    notice("uploadNotice", "Please choose at least one file.", "error");
    return;
  }
  const button = $("uploadButton");
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = "Uploading...";
  notice("uploadNotice", `Uploading ${files.length} file${files.length === 1 ? "" : "s"}...`);
  let uploadedCount = 0;
  const warnings = [];
  let extractionWarning = false;
  try {
    for (const file of files) {
      const result = await uploadDocument(file);
      uploadedCount += 1;
      if (result.processing_warning) {
        warnings.push(`${file.name}: ${result.processing_warning}`);
        extractionWarning = true;
      }
      if (result.duplicate_of) warnings.push(`${file.name} has the same content as ${result.duplicate_of}.`);
    }
    $("fileInput").value = "";
    await loadFiles();
    notice("uploadNotice", warnings.length
      ? `Uploaded ${uploadedCount} file${uploadedCount === 1 ? "" : "s"}. ${warnings.join(" ")}`
      : `Uploaded ${uploadedCount} file${uploadedCount === 1 ? "" : "s"}.`, extractionWarning ? "error" : "success");
  } catch (error) {
    if (uploadedCount) await loadFiles();
    notice("uploadNotice", uploadedCount
      ? `Uploaded ${uploadedCount} file${uploadedCount === 1 ? "" : "s"}; the next file failed: ${error.message}`
      : "Upload failed: " + error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});
