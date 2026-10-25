/* SmartFiles AI chat interface. */
const assistantHistory = [];
let assistantContextFileIds = [];
let chatRequestId = 0;
let chatInFlight = false;
let activeChatController = null;

function showChatWelcome() {
  const messages = $("chatMessages");
  const template = $("chatWelcomeTemplate");
  if (!messages || !template) return;
  messages.replaceChildren(template.content.cloneNode(true));
  messages.setAttribute("aria-busy", "false");
}

function resetAssistantChat() {
  chatRequestId += 1;
  if (activeChatController) activeChatController.abort();
  activeChatController = null;
  chatInFlight = false;
  assistantHistory.length = 0;
  assistantContextFileIds = [];
  showChatWelcome();
  const send = $("chatSend");
  if (send) send.disabled = false;
  const input = $("chatInput");
  if (input) {
    input.value = "";
    resizeChatInput();
  }
  notice("chatNotice", "");
}

$("clearChat").addEventListener("click", resetAssistantChat);
showChatWelcome();

async function refreshAssistantStatus() {
  const note = $("assistantStatusNote");
  if (!note) return;
  try {
    const status = await requestJSON(API.assistantStatus);
    if (status.configured && status.provider === "Ollama") {
      note.textContent = `Local AI ready · ${status.model}`;
    } else if (status.configured && status.provider === "Gemini fallback") {
      note.textContent = "Local AI unavailable · optional fallback configured";
    } else {
      note.textContent = "AI is unavailable right now · check your local model";
    }
    note.classList.toggle("is-unavailable", !status.configured);
  } catch (_) {
    note.textContent = "Assistant status is unavailable";
    note.classList.add("is-unavailable");
  }
}

function appendInlineFormatting(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    const element = token.startsWith("**") ? document.createElement("strong") : document.createElement("code");
    element.textContent = token.startsWith("**") ? token.slice(2, -2) : token.slice(1, -1);
    parent.appendChild(element);
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
}

function renderReadableAnswer(container, answer) {
  const lines = String(answer || "").replace(/\r/g, "").split("\n");
  let paragraph = [];
  let list = null;
  let codeBlock = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const node = document.createElement("p");
    appendInlineFormatting(node, paragraph.join(" "));
    container.appendChild(node);
    paragraph = [];
  };
  const closeList = () => { list = null; };

  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flushParagraph(); closeList();
      if (codeBlock) codeBlock = null;
      else {
        codeBlock = document.createElement("pre");
        container.appendChild(codeBlock);
      }
      continue;
    }
    if (codeBlock) {
      codeBlock.textContent += (codeBlock.textContent ? "\n" : "") + line;
      continue;
    }
    if (!line.trim()) { flushParagraph(); closeList(); continue; }

    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) {
      flushParagraph(); closeList();
      const node = document.createElement("h3");
      appendInlineFormatting(node, heading[1]);
      container.appendChild(node);
      continue;
    }
    const bullet = line.match(/^\s*(?:[-*•])\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      flushParagraph();
      const kind = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== kind) {
        closeList();
        list = document.createElement(kind);
        container.appendChild(list);
      }
      const item = document.createElement("li");
      appendInlineFormatting(item, (bullet || numbered)[1]);
      list.appendChild(item);
      continue;
    }
    closeList();
    paragraph.push(line.trim());
  }
  flushParagraph();
}

function assistantReply(data) {
  const turn = document.createElement("article");
  turn.className = "chat-turn chat-turn-assistant";

  const avatar = document.createElement("span");
  avatar.className = "chat-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = "SF";

  const content = document.createElement("div");
  content.className = "chat-turn-content";
  const role = document.createElement("div");
  role.className = "chat-role";
  role.textContent = "SmartFiles AI";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const answer = document.createElement("div");
  answer.className = "chat-answer-text";
  renderReadableAnswer(answer, data.answer || "I couldn't get an answer for that request.");
  bubble.appendChild(answer);
  content.append(role, bubble);

  const citations = new Set(Array.isArray(data.citations) ? data.citations : []);
  const sources = Array.isArray(data.sources)
    ? data.sources.filter(source => citations.has(source.ref))
    : [];
  if (sources.length) {
    const list = document.createElement("div");
    list.className = "chat-source-list";
    const heading = document.createElement("strong");
    heading.textContent = sources.length === 1 ? "Source" : "Sources";
    list.appendChild(heading);
    sources.forEach(source => {
      const item = document.createElement("div");
      item.className = "chat-source";
      if (source.id) {
        const link = document.createElement("button");
        link.type = "button";
        link.textContent = `${source.ref ? `[${source.ref}] ` : ""}${source.name || "Document"}`;
        link.addEventListener("click", () => openDocument(source.id, "assistant"));
        item.appendChild(link);
      } else {
        const title = document.createElement("strong");
        title.textContent = `${source.ref ? `[${source.ref}] ` : ""}${source.name || "Source"}`;
        item.appendChild(title);
      }
      const metadata = document.createElement("small");
      metadata.textContent = [source.is_demo ? "Sample data" : "", source.file_type || source.kind,
        source.category, source.reference].filter(Boolean).join(" · ");
      item.appendChild(metadata);
      if (source.snippet) {
        const snippet = document.createElement("p");
        snippet.textContent = source.snippet;
        item.appendChild(snippet);
      }
      if (typeof source.url === "string") {
        try {
          const url = new URL(source.url);
          if (["http:", "https:"].includes(url.protocol)) {
            const external = document.createElement("a");
            external.href = url.href;
            external.target = "_blank";
            external.rel = "noopener noreferrer";
            external.textContent = "Open verified source";
            item.appendChild(external);
          }
        } catch (_) { /* Ignore invalid stored URLs. */ }
      }
      list.appendChild(item);
    });
    content.appendChild(list);
  }

  turn.append(avatar, content);
  return turn;
}

function appendUserMessage(text) {
  const turn = document.createElement("article");
  turn.className = "chat-turn chat-turn-user";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble chat-answer-text";
  bubble.textContent = text;
  turn.appendChild(bubble);
  return turn;
}

function appendPendingMessage() {
  const turn = document.createElement("article");
  turn.className = "chat-turn chat-turn-assistant";
  turn.setAttribute("role", "status");
  turn.setAttribute("aria-live", "polite");
  const avatar = document.createElement("span");
  avatar.className = "chat-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = "SF";
  const content = document.createElement("div");
  content.className = "chat-turn-content";
  const role = document.createElement("div");
  role.className = "chat-role";
  role.textContent = "SmartFiles AI";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble chat-answer-text assistant-loading";
  bubble.append("Thinking");
  const dots = document.createElement("span");
  dots.className = "typing-dots";
  dots.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i += 1) dots.appendChild(document.createElement("i"));
  bubble.appendChild(dots);
  content.append(role, bubble);
  turn.append(avatar, content);
  return turn;
}

function resizeChatInput() {
  const input = $("chatInput");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

function scrollChatToLatest() {
  const messages = $("chatMessages");
  if (messages) messages.scrollTo({top: messages.scrollHeight, behavior: "smooth"});
}

async function sendChatMessage() {
  if (chatInFlight) return;
  const input = $("chatInput");
  const question = input.value.trim();
  if (!question) { input.focus(); return; }

  const messages = $("chatMessages");
  const welcome = messages.querySelector(".chat-welcome");
  if (welcome) welcome.remove();
  chatInFlight = true;
  const requestId = ++chatRequestId;
  const controller = new AbortController();
  activeChatController = controller;
  const requestBody = {question, history: assistantHistory.slice(-8)};
  if ($("assistantDocument")?.value) requestBody.file_id = Number($("assistantDocument").value);
  if (assistantContextFileIds.length) requestBody.context_file_ids = assistantContextFileIds.slice(0, 8);

  messages.append(appendUserMessage(question));
  const pending = appendPendingMessage();
  messages.append(pending);
  messages.setAttribute("aria-busy", "true");
  const send = $("chatSend");
  send.disabled = true;
  notice("chatNotice", "");
  scrollChatToLatest();
  const timeout = setTimeout(() => controller.abort(), 180000);

  try {
    const data = await requestJSON(API.chat, {
      method: "POST",
      body: JSON.stringify(requestBody),
      signal: controller.signal
    });
    if (requestId !== chatRequestId) return;
    pending.replaceWith(assistantReply(data));
    if (Array.isArray(data.sources)) {
      assistantContextFileIds = [...new Set(data.sources.map(source => Number(source.id)).filter(id => Number.isInteger(id) && id > 0))].slice(0, 8);
    }
    if (data.answer_mode !== "ai_error") {
      assistantHistory.push({role: "user", text: question});
      assistantHistory.push({role: "assistant", text: data.answer || ""});
      if (assistantHistory.length > 16) assistantHistory.splice(0, assistantHistory.length - 16);
      input.value = "";
      resizeChatInput();
    }
    if (data.answer_mode === "ai_error") {
      notice("chatNotice", "");
    } else if (Array.isArray(data.extraction_warnings) && data.extraction_warnings.length) {
      const skipped = data.extraction_warnings.map(item => `${item.name}: ${item.message}`).join("; ");
      notice("chatNotice", `Some documents could not be searched: ${skipped}`, "warning");
    } else {
      notice("chatNotice", "");
    }
    scrollChatToLatest();
  } catch (error) {
    if (requestId !== chatRequestId) return;
    const message = error.name === "AbortError"
      ? "This request took too long. Please try again."
      : error instanceof TypeError
        ? "Could not reach SmartFiles. Check your connection and try again."
        : "SmartFiles couldn't complete this message. Please try again.";
    pending.replaceWith(assistantReply({answer: message, citations: [], sources: []}));
    notice("chatNotice", "");
    scrollChatToLatest();
  } finally {
    clearTimeout(timeout);
    if (requestId === chatRequestId) {
      chatInFlight = false;
      activeChatController = null;
      send.disabled = false;
      messages.setAttribute("aria-busy", "false");
      scrollChatToLatest();
    }
  }
}

$("chatForm").addEventListener("submit", event => {
  event.preventDefault();
  sendChatMessage();
});

$("chatInput").addEventListener("input", resizeChatInput);
$("chatInput").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("chatForm").requestSubmit();
  }
});

$("chatMessages").addEventListener("click", event => {
  const chip = event.target.closest("[data-chat-prompt]");
  if (!chip || chatInFlight) return;
  $("chatInput").value = chip.dataset.chatPrompt || "";
  resizeChatInput();
  sendChatMessage();
});

refreshAssistantStatus();

// The dashboard is shown only after a successful login. No credentials are in this page.
