/* Shared API, navigation, authentication, and sign-out. */
/* Same-origin FastAPI routes; authentication uses the HTTP-only session cookie. */
const API = {
  register: "/api/register",
  login: "/api/login",
  logout: "/api/logout",
  me: "/api/me",

  // These paths match the routes implemented by the FastAPI backend.
  files: "/api/files",
  dashboard: "/api/dashboard",
  upload: "/api/upload",
  demoImport: "/api/demo/import",
  search: "/api/search",
  duplicates: "/api/duplicates",
  reprocess: "/api/reprocess",
  chat: "/api/chat",
  compare: "/api/compare",
  compress: "/api/compress",
  compressionHistory: "/api/compression/history",
  storageOptimizer: "/api/storage-optimizer",
  plagiarismScans: "/api/plagiarism/scans",
  plagiarismReports: "/api/plagiarism/reports",
  plagiarismComparisons: "/api/plagiarism/comparisons",
  assistantStatus: "/api/assistant/status"
};

const $ = id => document.getElementById(id);
let currentEmail = "";
let searchTimer;
let searchRequestId = 0;
let searchPanelWasOpen = false;
let documents = [];
let previousView = "documents";

function notice(id, message, type = "") {
  const el = $(id);
  const loading = /creating|signing in|loading|scanning|searching|retrieving|uploading|thinking|comparing|deleting/i.test(message || "");
  el.className = message ? "notice " + type + (loading ? " loading" : "") : "";
  el.textContent = message;
}

function showAuth(mode) {
  if (typeof resetAssistantChat === "function") resetAssistantChat();
  $("authScreen").classList.remove("hidden");
  $("dashboard").classList.add("hidden");
  $("loginForm").classList.toggle("hidden", mode !== "login");
  $("registerForm").classList.toggle("hidden", mode !== "register");
  notice("loginNotice", "");
  notice("registerNotice", "");
}

function enterDashboard(email) {
  currentEmail = email || "SmartFiles User";
  $("userEmail").textContent = currentEmail;
  $("userAvatar").textContent = currentEmail.charAt(0).toUpperCase();
  $("authScreen").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
  showView("home");
  renderFiles();
  loadFiles();
  notice("apiNotice", "Signed in successfully.");
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(options.body instanceof FormData ? {} : {"Content-Type": "application/json"}),
      ...(options.headers || {})
    }
  });

  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; }
  catch { data = {detail: text}; }

  if (!response.ok) {
    const detail = data.detail || data.message || data.error || "Request failed (" + response.status + ")";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

$("showRegister").addEventListener("click", () => showAuth("register"));
$("showLogin").addEventListener("click", () => showAuth("login"));

$("register").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("registerButton");
  button.disabled = true;
  notice("registerNotice", "Creating your account...");

  try {
    const email = $("registerEmail").value.trim();
    const password = $("registerPassword").value;

    await requestJSON(API.register, {
      method: "POST",
      body: JSON.stringify({email, password})
    });

    notice("registerNotice", "Registration successful. Please sign in.", "success");
    $("loginEmail").value = email;
    $("register").reset();
    setTimeout(() => showAuth("login"), 900);
  } catch (error) {
    notice("registerNotice", error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("login").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("loginButton");
  button.disabled = true;
  notice("loginNotice", "Signing in...");

  try {
    const email = $("loginEmail").value.trim();
    const password = $("loginPassword").value;

    const data = await requestJSON(API.login, {
      method: "POST",
      body: JSON.stringify({email, password})
    });

    enterDashboard(email);
    $("loginPassword").value = "";
  } catch (error) {
    notice("loginNotice", error.message, "error");
  } finally {
    button.disabled = false;
  }
});

(async function restoreSession() {
  try {
    const user = await requestJSON(API.me);
    enterDashboard(user.email);
  } catch (_) {
    showAuth("login");
  }
})();

function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  const view = $(name + "View");
  if (view) view.classList.add("active");

  document.querySelectorAll(".nav-btn[data-view]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === name);
    if (btn.dataset.view === name) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });

  const titles = {
    home: "Dashboard",
    documents: "My Documents",
    upload: "Upload Documents",
    duplicates: "Duplicate Finder",
    assistant: "AI Assistant",
    search: "Search Documents",
    comparison: "Document Comparison",
    compressor: "File Compressor",
    optimizer: "Smart Storage Optimizer",
    plagiarism: "Plagiarism Checker",
    detail: "Document Details"
  };
  $("pageTitle").textContent = titles[name] || "Dashboard";
  if (name === "assistant" && typeof refreshAssistantStatus === "function") {
    refreshAssistantStatus();
  }
}
