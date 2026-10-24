/* Data-backed dashboard charts and storage optimizer views. */
const byteFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${byteFormatter.format(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value / 1024, unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${byteFormatter.format(amount)} ${units[unit]}`;
}

function renderAnalytics(data) {
  if (!data) return;
  const categorySelect = $("analyticsCategory");
  const fileTypeSelect = $("analyticsFileType");
  const previousCategory = categorySelect.value;
  const previousType = fileTypeSelect.value;
  categorySelect.replaceChildren(new Option("All categories", ""));
  (data.category_options || []).forEach(value => categorySelect.add(new Option(value, value)));
  if ([...categorySelect.options].some(option => option.value === previousCategory)) categorySelect.value = previousCategory;
  fileTypeSelect.replaceChildren(new Option("All file types", ""));
  (data.available_file_types || Object.keys(data.extensions || {})).forEach(value => {
    const extension = value.startsWith(".") ? value.slice(1) : value;
    fileTypeSelect.add(new Option(extension.toUpperCase(), extension));
  });
  if ([...fileTypeSelect.options].some(option => option.value === previousType)) fileTypeSelect.value = previousType;

  renderCategoryChart(data.categories || {});
  renderFormatChart(data.extensions || {});
  renderActivityChart(data.monthly_uploads || []);
  renderProcessingChart(data.processing || {}, Number(data.total || 0));
  renderStorageCards(data.storage || {});
  $("analyticsExactCount").textContent = String(data.duplicates?.exact_groups || 0);
  $("analyticsNearCount").textContent = String(data.duplicates?.potential_near_pairs || 0);
  const largest = Array.isArray(data.largest_documents) ? data.largest_documents[0] : null;
  $("largestSize").textContent = largest ? formatBytes(largest.size_bytes) : "—";
  $("largestName").textContent = largest ? largest.name : "No stored documents match these filters";
  $("compressibleCount").textContent = String((data.compressible_documents || []).length);
}

function renderCategoryChart(categories) {
  const root = $("categoryChart");
  root.replaceChildren();
  const entries = Object.entries(categories).filter(([, count]) => Number(count) > 0).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + Number(count), 0);
  if (!total) {
    const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No category data for this selection.";
    root.appendChild(empty); return;
  }
  const colors = ["#315fce", "#16a085", "#9b59b6", "#e67e22", "#d94f70", "#3398b5", "#859b32", "#6b7280", "#c0843d"];
  const ring = document.createElement("div"); ring.className = "donut-ring";
  let start = 0;
  const gradients = entries.map(([name, value], index) => {
    const end = start + Number(value) / total * 100;
    const segment = `${colors[index % colors.length]} ${start.toFixed(2)}% ${end.toFixed(2)}%`;
    start = end;
    return segment;
  });
  ring.style.background = `conic-gradient(${gradients.join(",")})`;
  ring.setAttribute("role", "img");
  ring.setAttribute("aria-label", `Category distribution across ${total} documents`);
  const center = document.createElement("span");
  const totalLabel = document.createElement("strong"); totalLabel.textContent = String(total);
  const unitLabel = document.createElement("small"); unitLabel.textContent = "files";
  center.append(totalLabel, unitLabel);
  ring.appendChild(center);
  const legend = document.createElement("div"); legend.className = "chart-legend";
  entries.forEach(([name, value], index) => {
    const item = document.createElement("div"); item.className = "chart-legend-item";
    const dot = document.createElement("span"); dot.className = "legend-dot"; dot.style.backgroundColor = colors[index % colors.length];
    const label = document.createElement("span"); label.textContent = name;
    const count = document.createElement("strong"); count.textContent = `${value} (${(Number(value) / total * 100).toFixed(0)}%)`;
    item.append(dot, label, count); legend.appendChild(item);
  });
  root.append(ring, legend);
}

function renderFormatChart(formats) {
  const root = $("formatChart"); root.replaceChildren();
  const entries = Object.entries(formats).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, value]) => Number(value)));
  if (!entries.length) {
    const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No file-format data for this selection.";
    root.appendChild(empty); return;
  }
  entries.forEach(([extension, value]) => {
    const row = document.createElement("div"); row.className = "bar-chart-row";
    const label = document.createElement("span"); label.textContent = extension ? extension.replace(".", "").toUpperCase() : "Unknown";
    const track = document.createElement("div"); track.className = "bar-track";
    const bar = document.createElement("span"); bar.className = "bar-fill";
    bar.style.width = `${Math.max(2, Number(value) / max * 100)}%`; bar.setAttribute("aria-label", `${value} files`);
    track.appendChild(bar);
    const count = document.createElement("strong"); count.textContent = String(value);
    row.append(label, track, count); root.appendChild(row);
  });
}

function renderActivityChart(months) {
  const root = $("activityChart"); root.replaceChildren();
  if (!months.length || months.every(item => !Number(item.count))) {
    const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No upload activity in this period.";
    root.appendChild(empty); return;
  }
  const width = 520, height = 190, margin = 24;
  const max = Math.max(1, ...months.map(item => Number(item.count)));
  const points = months.map((item, index) => ({
    x: margin + index * (width - margin * 2) / Math.max(1, months.length - 1),
    y: height - margin - Number(item.count) / max * (height - margin * 2),
    item
  }));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Monthly upload activity");
  const line = document.createElementNS(svg.namespaceURI, "polyline");
  line.setAttribute("points", points.map(point => `${point.x},${point.y}`).join(" "));
  line.setAttribute("fill", "none"); line.setAttribute("stroke", "#315fce"); line.setAttribute("stroke-width", "3");
  line.setAttribute("stroke-linecap", "round"); line.setAttribute("stroke-linejoin", "round"); svg.appendChild(line);
  points.forEach(point => {
    const dot = document.createElementNS(svg.namespaceURI, "circle");
    dot.setAttribute("cx", point.x); dot.setAttribute("cy", point.y); dot.setAttribute("r", "4"); dot.setAttribute("fill", "#315fce");
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent = `${point.item.month}: ${point.item.count} upload(s)`;
    dot.append(title); svg.appendChild(dot);
  });
  const labels = document.createElement("div"); labels.className = "month-labels";
  points.forEach((point, index) => {
    if (index % 2 && index !== points.length - 1) return;
    const label = document.createElement("span"); label.textContent = point.item.month.slice(2);
    labels.appendChild(label);
  });
  root.append(svg, labels);
}

function renderProcessingChart(status, total) {
  const root = $("processingChart"); root.replaceChildren();
  const rows = [
    ["Successfully processed", Number(status.indexed || 0) + Number(status.extracted || 0), "#168879"],
    ["Pending / needs attention", Number(status.needs_attention || 0), "#df9a2f"],
    ["Failed", Number(status.failed || 0), "#d94f70"]
  ];
  if (!total) {
    const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "Processing totals appear after documents are added.";
    root.appendChild(empty); return;
  }
  rows.forEach(([name, value, color]) => {
    const item = document.createElement("div"); item.className = "processing-item";
    const heading = document.createElement("div"); heading.className = "processing-heading";
    const label = document.createElement("span"); label.textContent = name;
    const count = document.createElement("strong"); count.textContent = `${value} / ${total}`;
    const track = document.createElement("div"); track.className = "processing-track";
    const fill = document.createElement("span"); fill.style.width = `${Math.min(100, value / total * 100)}%`; fill.style.backgroundColor = color;
    track.appendChild(fill); heading.append(label, count); item.append(heading, track); root.appendChild(item);
  });
}

function renderStorageCards(storage) {
  const root = $("storageStats"); root.replaceChildren();
  [
    ["Original storage", storage.original_bytes, "Current original files in this selection"],
    ["Compressed outputs", storage.compressed_bytes, "Stored optimized copies"],
    ["Confirmed savings", storage.confirmed_saved_bytes, "Measured positive savings only"]
  ].forEach(([label, value, detail]) => {
    const card = document.createElement("div"); card.className = "stat";
    const title = document.createElement("div"); title.className = "muted"; title.textContent = label;
    const amount = document.createElement("h3"); amount.textContent = formatBytes(value);
    const note = document.createElement("p"); note.textContent = detail;
    card.append(title, amount, note); root.appendChild(card);
  });
}

function analyticsQuery() {
  const query = new URLSearchParams();
  if ($("analyticsCategory").value) query.set("category", $("analyticsCategory").value);
  if ($("analyticsFileType").value) query.set("file_type", $("analyticsFileType").value);
  const from = $("analyticsDateFrom").value;
  const to = $("analyticsDateTo").value;
  if (from) query.set("date_from", String(new Date(`${from}T00:00:00`).getTime() / 1000));
  if (to) query.set("date_to", String(new Date(`${to}T23:59:59`).getTime() / 1000));
  return query;
}

async function refreshFilteredAnalytics() {
  const query = analyticsQuery();
  if ($("analyticsDateFrom").value && $("analyticsDateTo").value &&
      $("analyticsDateFrom").value > $("analyticsDateTo").value) {
    notice("analyticsNotice", "The start date must not be later than the end date.", "error"); return;
  }
  const button = $("applyAnalyticsFilters"); button.disabled = true;
  notice("analyticsNotice", "Loading filtered analytics...");
  try {
    renderAnalytics(await requestJSON(`${API.dashboard}?${query.toString()}`));
    notice("analyticsNotice", "");
  } catch (error) {
    notice("analyticsNotice", `Could not load analytics: ${error.message}`, "error");
  } finally { button.disabled = false; }
}

function renderOptimizerDuplicates(root, groups, isExact) {
  root.replaceChildren();
  if (!groups.length) {
    const empty = document.createElement("div"); empty.className = "empty";
    empty.textContent = isExact ? "No exact duplicate groups found." : "No potential near-duplicate pairs found.";
    root.appendChild(empty); return;
  }
  groups.forEach(group => {
    const card = document.createElement("section"); card.className = "duplicate-group";
    const heading = document.createElement("h3");
    heading.textContent = isExact ? `Exact duplicate group (${group.files.length} files)` : `Potential near-match (${(Number(group.score) * 100).toFixed(1)}% text overlap)`;
    card.appendChild(heading);
    (group.files || []).forEach(file => {
      const line = document.createElement("div"); line.className = "duplicate-file";
      const name = document.createElement("button"); name.type = "button"; name.className = "file-open"; name.textContent = file.name;
      name.addEventListener("click", () => openDocument(file.id, "optimizer"));
      const detail = document.createElement("span"); detail.className = "duplicate-file-meta";
      detail.textContent = `${file.category || ""} | ${formatBytes(file.size_bytes)}`;
      line.append(name, detail); card.appendChild(line);
    });
    if (!isExact && group.reason) {
      const note = document.createElement("p"); note.className = "muted"; note.textContent = group.reason;
      card.appendChild(note);
    }
    root.appendChild(card);
  });
}

async function loadOptimizer() {
  notice("optimizerNotice", "Checking your actual documents and compression results...");
  const query = analyticsQuery();
  try {
    const data = await requestJSON(`${API.storageOptimizer}?${query.toString()}`);
    renderStorageCards(data.storage || {});
    $("optimizerStorage").replaceChildren(...$("storageStats").cloneNode(true).childNodes);
    const largest = $("optimizerLargest"); largest.replaceChildren();
    (data.largest_documents || []).forEach(file => {
      const row = document.createElement("div"); row.className = "file-row";
      const info = document.createElement("div"); info.className = "file-info";
      const name = document.createElement("button"); name.type = "button"; name.className = "file-open"; name.textContent = file.name;
      name.addEventListener("click", () => openDocument(file.id, "optimizer"));
      const meta = document.createElement("small"); meta.textContent = `${file.file_type} | ${file.category || "Uncategorized"} | ${formatBytes(file.size_bytes)}`;
      info.append(name, meta); row.appendChild(info);
      if (file.can_compress) {
        const button = document.createElement("button"); button.type = "button"; button.className = "primary"; button.textContent = "Compress";
        button.addEventListener("click", () => openCompressor(file.id));
        row.appendChild(button);
      } else {
        const label = document.createElement("span"); label.className = "muted"; label.textContent = "No supported compressor";
        row.appendChild(label);
      }
      largest.appendChild(row);
    });
    if (!largest.childElementCount) {
      const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No stored documents match these filters.";
      largest.appendChild(empty);
    }
    renderOptimizerDuplicates($("optimizerExact"), data.exact_duplicates || [], true);
    renderOptimizerDuplicates($("optimizerNear"), data.potential_near_duplicates || [], false);
    const history = $("optimizerHistory"); history.replaceChildren();
    (data.compression_history || []).forEach(item => {
      const row = document.createElement("div"); row.className = "file-row";
      const label = document.createElement("div"); label.className = "file-info";
      const title = document.createElement("strong"); title.textContent = item.original_name;
      const detail = document.createElement("small");
      const savings = Number(item.space_saved) > 0 ? `${formatBytes(item.space_saved)} saved (${Number(item.percent_reduction).toFixed(1)}%)` : "No size reduction";
      detail.textContent = `${formatBytes(item.original_size)} → ${formatBytes(item.compressed_size)} | ${savings}`;
      label.append(title, detail); row.appendChild(label);
      const download = document.createElement("a"); download.className = "secondary"; download.textContent = "Download";
      download.href = `/api/compression/${encodeURIComponent(item.id)}/raw?dl=1`;
      row.appendChild(download); history.appendChild(row);
    });
    if (!history.childElementCount) {
      const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No measured compression results yet.";
      history.appendChild(empty);
    }
    notice("optimizerNotice", "Suggestions refreshed from your stored files and measured compression history.", "success");
  } catch (error) {
    notice("optimizerNotice", `Could not load storage suggestions: ${error.message}`, "error");
  }
}

$("applyAnalyticsFilters").addEventListener("click", refreshFilteredAnalytics);
$("focusSearch").addEventListener("click", () => $("searchInput").focus());
$("refreshOptimizer").addEventListener("click", loadOptimizer);
document.querySelectorAll('.nav-btn[data-view="optimizer"]').forEach(button => button.addEventListener("click", loadOptimizer));
