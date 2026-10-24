/* SmartFiles search feature handlers. */
function renderSearchResults(results, returnView) {
  const container = $("searchResultsList");
  container.replaceChildren();
  results.forEach(result => {
    const card = document.createElement("div"); card.className = "file-row search-result";
    const info = document.createElement("div"); info.className = "file-info";
    const title = document.createElement("button"); title.type = "button";
    title.className = "file-open"; title.textContent = result.name || "Untitled document";
    title.addEventListener("click", () => openDocument(result.id, returnView));
    const metadata = document.createElement("small");
    const category = result.category || "Uncategorized";
    const kind = result.kind || "Document";
    const match = result.match_type || "keyword";
    const relevance = Number.isFinite(Number(result.score)) && Number(result.score) > 0 ? ` ? ${Math.round(Number(result.score) * 100)}% relevance` : "";
    metadata.textContent = `${result.is_demo ? "Sample data | " : ""}${category} ? ${kind} ? ${match}${relevance}`;
    const snippet = document.createElement("p"); snippet.className = "search-snippet";
    snippet.textContent = result.snippet || "Matching document found.";
    info.append(title, metadata, snippet);
    const actions = document.createElement("div"); actions.className = "search-result-actions";
    const open = document.createElement("button"); open.type = "button";
    open.className = "primary"; open.textContent = "Open";
    open.addEventListener("click", () => openDocument(result.id, returnView));
    actions.appendChild(open); card.append(info, actions); container.appendChild(card);
  });
}

function runDocumentSearch() {
  const query = $("searchInput").value.trim();
  const filters = {
    category: $("searchCategory").value,
    file_type: $("searchType").value,
    tag: $("searchTag").value.trim(),
    date_from: $("searchDateFrom").value,
    date_to: $("searchDateTo").value
  };
  const hasFilter = Object.values(filters).some(Boolean);
  const panel = $("searchResultsPanel");
  clearTimeout(searchTimer);
  const requestId = ++searchRequestId;
  if (!query && !hasFilter) {
    panel.classList.add("hidden");
    panel.setAttribute("aria-busy", "false");
    $("searchResultsList").replaceChildren();
    notice("searchNotice", "");
    $("searchMethodNote").textContent = "";
    return;
  }

  panel.classList.remove("hidden");
  panel.setAttribute("aria-busy", "true");
  $("searchResultsList").replaceChildren();
  $("searchMethodNote").textContent = "";
  notice("searchNotice", "Searching your documents...");
  searchTimer = setTimeout(async () => {
    try {
      const params = new URLSearchParams({ query });
      if (filters.category) params.set("category", filters.category);
      if (filters.file_type) params.set("file_type", filters.file_type);
      if (filters.tag) params.set("tag", filters.tag);
      if (filters.date_from) params.set("date_from", String(new Date(filters.date_from + "T00:00:00").getTime() / 1000));
      if (filters.date_to) params.set("date_to", String(new Date(filters.date_to + "T23:59:59").getTime() / 1000));
      const data = await requestJSON(API.search + "?" + params.toString());
      if (requestId !== searchRequestId) return;
      const results = Array.isArray(data.results) ? data.results : [];
      const active = document.querySelector(".view.active");
      const returnView = active ? active.id.replace(/View$/, "") : "home";
      renderSearchResults(results, returnView);
      if (!query) {
        $("searchMethodNote").textContent = "Filtered by document metadata and upload date.";
      } else if (data.semantic_status === "available") {
        $("searchMethodNote").textContent = "Keyword matching plus MiniLM semantic embeddings.";
      } else if (data.semantic_status === "model_unavailable") {
        $("searchMethodNote").textContent = "Keyword search active; semantic model unavailable.";
      } else {
        $("searchMethodNote").textContent = "Keyword search active; no compatible semantic vectors indexed.";
      }
      if (results.length) {
        notice("searchNotice", `${results.length} relevant document${results.length === 1 ? "" : "s"} found.`);
      } else {
        notice("searchNotice", "No matching documents found. Try a filename, topic, category, tag, or phrase from the document.");
      }
    } catch (error) {
      if (requestId !== searchRequestId) return;
      $("searchResultsList").replaceChildren();
      notice("searchNotice", "Search failed: " + error.message + ". Check that you are signed in and the backend is running.", "error");
    } finally {
      if (requestId === searchRequestId) panel.setAttribute("aria-busy", "false");
    }
  }, 350);
}

$("searchInput").addEventListener("input", runDocumentSearch);
["searchCategory", "searchType", "searchTag", "searchDateFrom", "searchDateTo"].forEach(id => {
  $(id).addEventListener("input", runDocumentSearch);
  $(id).addEventListener("change", runDocumentSearch);
});
