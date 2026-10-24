/* SmartFiles duplicates feature handlers. */
function renderDuplicateGroups(container, groups, label) {
  container.replaceChildren();
  if (!Array.isArray(groups) || groups.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No " + label + " found.";
    container.appendChild(empty);
    return;
  }
  groups.forEach((group, index) => {
    const files = group.files;
    if (!Array.isArray(files) || !files.length) return;
    const card = document.createElement("div"); card.className = "duplicate-group";
    const heading = document.createElement("h3");
    heading.textContent = label === "exact duplicate groups"
      ? `Exact match group ${index + 1} (${files.length} files)`
      : label === "potential near-duplicate pairs"
        ? `Potential text match (${(Number(group.score) * 100).toFixed(1)}% shingle overlap)`
        : `Similar image pair (${(Number(group.score) * 100).toFixed(1)}% match)`;
    card.appendChild(heading);
    if (group.reason) {
      const reason = document.createElement("p"); reason.className = "muted";
      reason.textContent = group.reason + " Score: Jaccard similarity of normalized 3-word phrases.";
      card.appendChild(reason);
    }
    files.forEach(file => {
      const row = document.createElement("div"); row.className = "duplicate-file";
      const name = document.createElement("button"); name.type = "button";
      name.className = "file-open"; name.textContent = file.name || "Unnamed document";
      name.addEventListener("click", () => openDocument(file.id, "duplicates"));
      const meta = document.createElement("span"); meta.className = "duplicate-file-meta";
      const date = file.created ? prettyDate(file.created) : "Date unknown";
      const size = file.size_bytes == null ? "Size unavailable" : `${(file.size_bytes / 1024).toFixed(1)} KB`;
      meta.textContent = [file.is_demo ? "Sample data" : "", file.kind, file.category, date, size].filter(Boolean).join(" | ");
      const actions = document.createElement("div"); actions.className = "duplicate-actions";
      const open = document.createElement("button"); open.type = "button";
      open.className = "secondary"; open.textContent = "Open";
      open.addEventListener("click", () => openDocument(file.id, "duplicates"));
      const download = document.createElement("a"); download.className = "secondary";
      download.textContent = "Download";
      download.href = `/api/files/${encodeURIComponent(file.id)}/raw?dl=1`;
      download.setAttribute("download", file.name || "document");
      actions.append(open, download); row.append(name, meta, actions); card.appendChild(row);
    });
    container.appendChild(card);
  });
}

async function loadDuplicates() {
  const button = $("checkDuplicates");
  button.disabled = true;
  $("exactDuplicateCount").textContent = "…";
  $("similarDuplicateCount").textContent = "…";
  $("exactDuplicatesList").replaceChildren();
  $("similarDuplicatesList").replaceChildren();
  $("nearDuplicatesList").replaceChildren();
  $("nearDuplicateCount").textContent = "...";
  notice("duplicatesNotice", "Scanning your documents...");
  try {
    const data = await requestJSON(API.duplicates);
    const exact = Array.isArray(data.exact) ? data.exact : [];
    const similar = Array.isArray(data.similar_images) ? data.similar_images : [];
    const near = Array.isArray(data.near_duplicates) ? data.near_duplicates : [];
    $("exactDuplicateCount").textContent = exact.length;
    $("nearDuplicateCount").textContent = near.length;
    $("similarDuplicateCount").textContent = similar.length;
    renderDuplicateGroups($("exactDuplicatesList"), exact, "exact duplicate groups");
    renderDuplicateGroups($("nearDuplicatesList"), near, "potential near-duplicate pairs");
    renderDuplicateGroups($("similarDuplicatesList"), similar, "similar image pairs");
    const similarStatus = data.similar_image_status;
    notice("duplicatesNotice", similarStatus === "not_indexed"
      ? "Exact duplicate scan completed. Similar-image results are unavailable because your image files have no visual embeddings indexed yet."
      : similarStatus === "no_images"
        ? "Exact duplicate scan completed. There are no images in your workspace to compare visually."
        : "Duplicate scan completed.", "success");
  } catch (error) {
    notice("duplicatesNotice", "Could not scan duplicates: " + error.message + ". Make sure you are signed in and the backend route is available.", "error");
    $("exactDuplicatesList").replaceChildren();
    $("similarDuplicatesList").replaceChildren();
    $("nearDuplicatesList").replaceChildren();
    $("nearDuplicateCount").textContent = "—";
    $("exactDuplicateCount").textContent = "—";
    $("similarDuplicateCount").textContent = "—";
  } finally {
    button.disabled = false;
  }
}

$("checkDuplicates").addEventListener("click", loadDuplicates);
document.querySelectorAll('.nav-btn[data-view="duplicates"]').forEach(btn => {
  btn.addEventListener("click", () => { showView("duplicates"); loadDuplicates(); });
});
