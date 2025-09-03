/* global mermaid */
const $ = (id) => document.getElementById(id);
const apiBase = "/api"; // nginx proxies /api -> 127.0.0.1:8000

mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "dark" });

// restore apiKey from localStorage (dev-only)
(function init() {
  const saved = localStorage.getItem("archgenie.apiKey");
  if (saved) $("apiKey").value = saved;
  // sensible default app name
  $("appName").value = "secure Azure 3-tier web app";
})();

$("apiKey").addEventListener("change", () => {
  localStorage.setItem("archgenie.apiKey", $("apiKey").value.trim());
});

$("btnAzure").addEventListener("click", async () => {
  await runAzure();
});
$("btnAWS").addEventListener("click", async () => {
  await runMock("aws");
});
$("btnGCP").addEventListener("click", async () => {
  await runMock("gcp");
});
$("copyTf").addEventListener("click", () => {
  const txt = $("tf").textContent || "";
  navigator.clipboard.writeText(txt).then(() => flashStatus("Terraform copied", "ok"));
});
$("downloadTf").addEventListener("click", () => {
  const blob = new Blob([$("tf").textContent || ""], { type: "text/plain" });
  $("downloadTf").href = URL.createObjectURL(blob);
});

function flashStatus(msg, kind = "info") {
  const el = $("status");
  el.textContent = msg;
  el.style.color = kind === "ok" ? "#86efac" : kind === "err" ? "#fca5a5" : "";
  setTimeout(() => (el.textContent = ""), 4000);
}

async function runAzure() {
  const key = $("apiKey").value.trim();
  const appName = $("appName").value.trim();
  const extra = $("prompt").value.trim();
  if (!key) return flashStatus("Enter x-api-key", "err");

  flashStatus("Calling Azure MCP…");
  try {
    const res = await fetch(`${apiBase}/mcp/azure/diagram-tf`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": key },
      body: JSON.stringify({
        app_name: appName || "3-tier web app",
        // If you updated backend to accept 'prompt', pass it too:
        prompt: extra || undefined
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    // AOAI response shape: choices[0].message.content (string)
    const content = getContentFromAoai(data);
    const parsed = safeParseJson(content);

    let diagram = "", tf = "";
    if (parsed && (parsed.diagram || parsed.terraform)) {
      diagram = parsed.diagram || "";
      tf = parsed.terraform || "";
    } else {
      // fallback: try code fences
      diagram = extractFence(content, "mermaid") || extractFence(content, "diagram") || content;
      tf = extractFence(content, "terraform") || "";
    }
    renderDiagram(diagram);
    showTF(tf);
    flashStatus("Azure MCP response ready", "ok");
  } catch (e) {
    console.error(e);
    flashStatus(`Azure MCP error: ${e.message}`, "err");
  }
}

async function runMock(which) {
  const key = $("apiKey").value.trim();
  if (!key) return flashStatus("Enter x-api-key", "err");
  flashStatus(`Calling ${which.toUpperCase()} mock…`);
  try {
    const res = await fetch(`${apiBase}/mcp/${which}/diagram-tf`, {
      headers: { "x-api-key": key },
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderDiagram(data.diagram || "");
    showTF(data.terraform || "");
    flashStatus(`${which.toUpperCase()} mock ready`, "ok");
  } catch (e) {
    console.error(e);
    flashStatus(`${which} mock error: ${e.message}`, "err");
  }
}

function getContentFromAoai(resp) {
  // Azure OpenAI chat completions typical
  try {
    const c = resp.choices && resp.choices[0] && resp.choices[0].message && resp.choices[0].message.content;
    return typeof c === "string" ? c : JSON.stringify(c);
  } catch {
    return JSON.stringify(resp);
  }
}

function safeParseJson(s) {
  if (!s) return null;
  try { return JSON.parse(s); } catch { /* not pure JSON */ }
  // try to find json fenced block
  const jsonBlock = extractFence(s, "json");
  if (jsonBlock) {
    try { return JSON.parse(jsonBlock); } catch {}
  }
  return null;
}

function extractFence(text, lang) {
  if (!text) return "";
  const re = new RegExp("```" + lang + "\\s*\\n([\\s\\S]*?)```", "i");
  const m = text.match(re);
  return m ? m[1].trim() : "";
}

function renderDiagram(src) {
  $("diagramSrc").textContent = src || "";
  const container = $("diagram");
  container.innerHTML = "";
  if (!src.trim()) { container.textContent = "No diagram received"; return; }
  const id = "mmd-" + Math.random().toString(36).slice(2);
  mermaid.render(id, src)
    .then(({ svg }) => container.innerHTML = svg)
    .catch(err => {
      container.innerHTML = `<pre class="code">${escapeHtml(String(err))}\n\n${escapeHtml(src)}</pre>`;
    });
}

function showTF(tf) {
  $("tf").textContent = tf || "";
  const btn = $("downloadTf");
  btn.classList.remove("disabled");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}