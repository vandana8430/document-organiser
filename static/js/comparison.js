/* SmartFiles document comparison workflow. */
function appendTermGroup(parent, title, values) {
  const section = document.createElement("section");
  const heading = document.createElement("h4");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = Array.isArray(values) && values.length ? values.join(", ") : "None";
  section.append(heading, body);
  parent.appendChild(section);
}

$("compareButton").addEventListener("click", async () => {
  const first = $("compareA").value;
  const second = $("compareB").value;
  const button = $("compareButton");
  const results = $("compareResults");
  results.replaceChildren();
  if (!first || !second || first === second) {
    notice("compareNotice", "Select two different documents to compare.", "error");
    return;
  }
  button.disabled = true;
  notice("compareNotice", "Comparing extracted document text...");
  try {
    const data = await requestJSON(API.compare, {
      method: "POST",
      body: JSON.stringify({a: Number(first), b: Number(second)})
    });
    const heading = document.createElement("h3");
    heading.textContent = `${data.a} (${data.a_kind || "file"}) and ${data.b} (${data.b_kind || "file"})`;
    const similarity = document.createElement("p");
    similarity.textContent = `Text similarity: ${Math.round(Number(data.similarity || 0) * 100)}%`;
    const summary = document.createElement("p");
    summary.textContent = data.similarity_explanation || data.analysis || "Comparison completed from extracted text.";
    results.append(heading, similarity, summary);
    if (data.identical) {
      const identical = document.createElement("p");
      identical.textContent = "These documents have identical content.";
      results.appendChild(identical);
    }
    if (data.similarity_sampled) {
      const sampleNote = document.createElement("p");
      sampleNote.className = "muted";
      sampleNote.textContent = "For very long documents, similarity uses the first 250,000 extracted characters. The displayed diff is limited to 300 lines.";
      results.appendChild(sampleNote);
    }
    appendTermGroup(results, "Shared topics", data.shared);
    appendTermGroup(results, "Matching sentences", data.common_text);
    appendTermGroup(results, `Topics only in ${data.a}`, data.only_a);
    appendTermGroup(results, `Topics only in ${data.b}`, data.only_b);
    const diffHeading = document.createElement("h4");
    diffHeading.textContent = "Text differences";
    const diff = document.createElement("pre");
    diff.className = "comparison-diff";
    diff.textContent = Array.isArray(data.diff) && data.diff.length
      ? data.diff.join("\n")
      : "No line differences found.";
    results.append(diffHeading, diff);
    if (data.truncated) {
      const hint = document.createElement("p");
      hint.className = "muted";
      hint.textContent = "The displayed diff was shortened to 300 lines.";
      results.appendChild(hint);
    }
    notice("compareNotice", "Comparison completed from extracted document text.", "success");
  } catch (error) {
    notice("compareNotice", "Could not compare documents: " + error.message, "error");
  } finally {
    button.disabled = false;
  }
});
