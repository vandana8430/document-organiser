/* Secure PDF and image compression workflow. */
function renderCompressionOptions(files) {
  const select = $("compressionDocument");
  const previous = select.value;
  select.replaceChildren(new Option("Select a PDF or supported image", ""));
  files.filter(file => /\.(pdf|jpe?g|png|webp)$/i.test(file.name || "")).forEach(file => {
    select.add(new Option(`${file.name} (${formatBytes(file.size_bytes || 0)})`, String(file.id)));
  });
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}

function openCompressor(fileId) {
  $("compressionSourceType").value = "existing";
  $("compressionExistingField").classList.remove("hidden");
  $("compressionUploadField").classList.add("hidden");
  $("compressionDocument").value = String(fileId);
  $("compressionResult").classList.add("hidden");
  showView("compressor");
  loadCompressionHistory();
}

window.openCompressor = openCompressor;

function updateCompressionSource() {
  const uploaded = $("compressionSourceType").value === "upload";
  $("compressionExistingField").classList.toggle("hidden", uploaded);
  $("compressionUploadField").classList.toggle("hidden", !uploaded);
  updateCompressionOptionVisibility();
}

function updateCompressionOptionVisibility() {
  const uploaded = $("compressionSourceType").value === "upload";
  const fileName = uploaded
    ? $("compressionFile").files[0]?.name || ""
    : $("compressionDocument").selectedOptions[0]?.textContent || "";
  const extension = fileName.split(".").pop()?.toLowerCase() || "";
  const isImage = ["jpg", "jpeg", "png", "webp"].includes(extension);
  const isPdf = extension === "pdf";
  $("compressionDimensionField").classList.toggle("hidden", !(isImage || isPdf));
  $("compressionQualityField").classList.toggle("hidden", !(isPdf || ["jpg", "jpeg", "webp"].includes(extension)));
}

function compressionOptions() {
  return {
    quality: Number($("compressionQuality").value),
    maxDimension: Number($("compressionMaxDimension").value || 0),
    mode: $("compressionMode").value
  };
}

function showCompressionResult(result) {
  const root = $("compressionResult"); root.replaceChildren(); root.classList.remove("hidden");
  const heading = document.createElement("h3");
  heading.textContent = result.status === "compressed" ? "Compression completed" : "No storage reduction";
  const stats = document.createElement("div"); stats.className = "compression-comparison";
  [
    ["Original size", result.original_size_bytes],
    ["Optimized size", result.compressed_size_bytes],
    ["Space saved", Math.max(0, Number(result.space_saved_bytes) || 0)]
  ].forEach(([label, value], index) => {
    const item = document.createElement("div"); item.className = "compression-size";
    const title = document.createElement("small"); title.textContent = label;
    const amount = document.createElement("strong");
    amount.textContent = formatBytes(value);
    item.append(title, amount); stats.appendChild(item);
  });
  const percent = document.createElement("p"); percent.className = "muted";
  percent.textContent = `Percentage reduction: ${Number(result.percent_reduction).toFixed(2)}%`;
  const message = document.createElement("p"); message.textContent = result.message;
  const actions = document.createElement("div"); actions.className = "section-actions";
  const download = document.createElement("a"); download.className = "primary"; download.href = result.download_url;
  download.textContent = result.status === "compressed" ? "Download compressed file" : "Download original file";
  download.setAttribute("download", "");
  actions.appendChild(download);
  root.append(heading, stats, percent, message);
  const fileType = String(result.file_type || "").toLowerCase();
  if (fileType === "pdf") {
    const preview = document.createElement("iframe"); preview.className = "compression-preview";
    preview.src = result.preview_url; preview.title = `Preview of compressed ${result.original_name}`;
    root.appendChild(preview);
  } else {
    const preview = document.createElement("img"); preview.className = "compression-image-preview";
    preview.src = result.preview_url; preview.alt = `Compressed preview of ${result.original_name}`;
    root.appendChild(preview);
  }
  root.appendChild(actions);
}

async function loadCompressionHistory() {
  const root = $("compressionHistory"); root.replaceChildren();
  try {
    const items = await requestJSON(API.compressionHistory);
    if (!items.length) {
      const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No compression runs yet. Results will appear here after a file is optimized.";
      root.appendChild(empty); return;
    }
    items.forEach(item => {
      const row = document.createElement("div"); row.className = "file-row";
      const info = document.createElement("div"); info.className = "file-info";
      const title = document.createElement("strong"); title.textContent = item.original_name;
      const detail = document.createElement("small");
      const saved = Number(item.space_saved) > 0
        ? `${formatBytes(item.space_saved)} saved (${Number(item.percent_reduction).toFixed(2)}%)`
        : `No reduction (${formatBytes(Math.abs(item.space_saved))}${Number(item.space_saved) < 0 ? " larger" : ""})`;
      detail.textContent = `${formatBytes(item.original_size)} → ${formatBytes(item.compressed_size)} | ${saved}`;
      info.append(title, detail);
      const link = document.createElement("a"); link.className = "secondary";
      link.href = `/api/compression/${encodeURIComponent(item.id)}/raw?dl=1`; link.textContent = "Download";
      row.append(info, link); root.appendChild(row);
    });
  } catch (error) {
    const failure = document.createElement("div"); failure.className = "empty";
    failure.textContent = `Could not load compression history: ${error.message}`; root.appendChild(failure);
  }
}

async function runCompression() {
  const button = $("compressButton");
  const progress = $("compressionProgress");
  const resultRoot = $("compressionResult");
  const sourceType = $("compressionSourceType").value;
  const selectedFile = $("compressionFile").files[0];
  if (sourceType === "existing" && !$("compressionDocument").value) {
    notice("compressionNotice", "Select a PDF or supported image from your documents.", "error"); return;
  }
  if (sourceType === "upload" && !selectedFile) {
    notice("compressionNotice", "Choose a PDF or supported image to upload.", "error"); return;
  }
  const form = new FormData();
  const {quality, maxDimension, mode} = compressionOptions();
  if (sourceType === "existing") form.append("file_id", $("compressionDocument").value);
  else form.append("file", selectedFile);
  form.append("quality", String(quality));
  form.append("max_dimension", String(maxDimension));
  form.append("mode", mode);
  button.disabled = true; progress.classList.remove("hidden"); resultRoot.classList.add("hidden");
  notice("compressionNotice", "Validating and optimizing the selected file...");
  try {
    const output = await requestJSON(API.compress, {method: "POST", body: form});
    showCompressionResult(output);
    notice("compressionNotice", output.message, output.status === "compressed" ? "success" : "");
    await Promise.all([loadCompressionHistory(), loadDashboard()]);
    if (typeof loadOptimizer === "function") loadOptimizer();
    $("compressionFile").value = "";
  } catch (error) {
    notice("compressionNotice", `Compression failed: ${error.message}`, "error");
  } finally {
    progress.classList.add("hidden"); button.disabled = false;
  }
}

$("compressionSourceType").addEventListener("change", updateCompressionSource);
$("compressionDocument").addEventListener("change", updateCompressionOptionVisibility);
$("compressionFile").addEventListener("change", updateCompressionOptionVisibility);
$("compressionQuality").addEventListener("input", () => {
  $("compressionQualityValue").textContent = $("compressionQuality").value;
});
$("compressButton").addEventListener("click", runCompression);
$("refreshCompressionHistory").addEventListener("click", loadCompressionHistory);
document.querySelectorAll('.nav-btn[data-view="compressor"]').forEach(button => button.addEventListener("click", loadCompressionHistory));
