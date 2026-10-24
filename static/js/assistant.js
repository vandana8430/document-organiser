/* SmartFiles assistant feature handlers. */
const assistantHistory = [];

async function refreshAssistantStatus() {
  const note = $("assistantStatusNote");
  if (!note) return;
  try {
    const status = await requestJSON(API.assistantStatus);
    note.textContent = status.configured
      ? `Ollama (${status.model}) is ready. Document and report answers stay grounded in retrieved evidence.`
      : (status.message || "No language model is configured; local document search and extractive answers remain available.");
  } catch (_) {
    note.textContent = "SmartFiles searches your own documents first. Start Ollama and configure OLLAMA_MODEL to enable AI responses.";
  }
}
function assistantReply(answer, sources, citations) {
  const reply = document.createElement("div");
  reply.className = "message";
  const answerText = document.createElement("div");
  answerText.textContent = "SmartFiles: " + answer;
  answerText.style.whiteSpace = "pre-wrap";
  reply.appendChild(answerText);
  const cited = new Set(Array.isArray(citations) ? citations : []);
  const visibleSources = cited.size ? sources.filter(source => cited.has(source.ref)) : sources;
  if (visibleSources.length) {
    const list = document.createElement("div"); list.className = "chat-source-list";
    const heading = document.createElement("strong"); heading.textContent = "Sources";
    list.appendChild(heading);
    visibleSources.forEach(source => {
      const item = document.createElement("div"); item.className = "chat-source";
      const link = document.createElement("button"); link.type = "button";
      link.textContent = `${source.ref ? `[${source.ref}] ` : ""}${source.name || "Document"}`;
      link.addEventListener("click", () => openDocument(source.id, "assistant"));
      const metadata = document.createElement("small");
      metadata.className = "muted";
      metadata.textContent = [source.is_demo ? "Sample data" : "", source.file_type || source.kind, source.category, source.reference].filter(Boolean).join(" | ");
      const snippet = document.createElement("p");
      snippet.textContent = source.snippet || "Retrieved passage from this document.";
      item.append(link, metadata, snippet); list.appendChild(item);
      if (typeof source.url === "string") {
        try {
          const url = new URL(source.url);
          if (["http:", "https:"].includes(url.protocol)) {
            const external = document.createElement("a");
            external.href = url.href; external.target = "_blank"; external.rel = "noopener noreferrer";
            external.textContent = "Open verified paper source";
            item.appendChild(external);
          }
        } catch (_) { /* Ignore invalid stored source URLs. */ }
      }
    });
    reply.appendChild(list);
  }
  return reply;
}

$("chatForm").addEventListener("submit", async event => {
  event.preventDefault();
  const question = $("chatInput").value.trim();
  if (!question) {
    notice("chatNotice", "Please enter a question about your documents.", "error");
    $("chatInput").focus();
    return;
  }
  const messages = $("chatMessages");
  const userMessage = document.createElement("div");
  userMessage.className = "message message-user";
  userMessage.textContent = "You: " + question;
  messages.appendChild(userMessage);
  const pending = document.createElement("div");
  pending.className = "message";
  pending.textContent = "SmartFiles is searching your documents...";
  pending.setAttribute("aria-live", "polite");
  messages.appendChild(pending);
  const send = $("chatSend"); send.disabled = true;
  notice("chatNotice", "Retrieving relevant passages...");
  try {
    if (!API.chat) throw new Error("The assistant API route is not configured.");
    const requestBody = {question, history: assistantHistory.slice(-8)};
    if ($("assistantDocument")?.value) requestBody.file_id = Number($("assistantDocument").value);
    const data = await requestJSON(API.chat, {
      method: "POST",
      body: JSON.stringify(requestBody)
    });
    const answer = data.answer || "The available documents do not provide enough information to answer that question.";
    pending.replaceWith(assistantReply(answer,
      Array.isArray(data.sources) ? data.sources : [], Array.isArray(data.citations) ? data.citations : []));
    assistantHistory.push({role: "user", text: question});
    assistantHistory.push({role: "assistant", text: answer});
    if (assistantHistory.length > 16) assistantHistory.splice(0, assistantHistory.length - 16);
    if (data.answer_mode === "greeting") {
      notice("chatNotice", "Ask a question about information in your documents.");
    } else if (data.answer_mode === "document_found") {
      notice("chatNotice", "Found from your document filename and metadata. Select a filename below to open it.", "success");
    } else if (data.answer_mode === "document_not_found") {
      notice("chatNotice", "No matching filename or document metadata was found in your workspace.");
    } else if (data.answer_mode === "generated") {
      notice("chatNotice", data.ai_status === "ollama"
        ? "Ollama answered using the retrieved evidence."
        : "SmartFiles answered using the retrieved evidence.", "success");
    } else if (data.answer_mode === "extractive") {
      notice("chatNotice", "Showing exact passages selected from your documents; no generated claims were added.", "success");
    } else if (data.answer_mode === "comparison") {
      notice("chatNotice", "Compared extracted text from the cited documents using the local comparison method.", "success");
    } else if (data.answer_mode === "extraction_unavailable") {
      notice("chatNotice", data.answer, "error");
    } else if (data.answer_mode === "insufficient") {
      notice("chatNotice", "The retrieved documents did not contain enough supporting information.");
    } else {
      notice("chatNotice", "The available documents do not provide enough matching evidence to answer.");
    }
    if (data.ai_error) {
      notice("chatNotice", `${$("chatNotice").textContent} Local AI is unavailable: ${data.ai_error}`, "warning");
    }
    if (Array.isArray(data.extraction_warnings) && data.extraction_warnings.length && data.answer_mode !== "extraction_unavailable") {
      const skipped = data.extraction_warnings.map(item => `${item.name}: ${item.message}`).join("; ");
      notice("chatNotice", `${$("chatNotice").textContent} Some documents could not be searched: ${skipped}`, "error");
    }
  } catch (error) {
    pending.textContent = "Assistant request failed: " + error.message + ". Check your sign-in and backend connection.";
    notice("chatNotice", "Could not get an answer: " + error.message, "error");
  } finally {
    $("chatInput").value = "";
    send.disabled = false;
    messages.scrollTop = messages.scrollHeight;
  }
});

// The dashboard is shown only after a successful login.
// No credentials or tokens are hard-coded into this page.
