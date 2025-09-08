/* global mermaid */
const $ = (id) => document.getElementById(id);
const apiBase = "/api"; // nginx proxies /api -> 127.0.0.1:8000
let lastSVG = "";
let currentScale = "fit";

mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "dark" });

// restore apiKey from localStorage (dev)
(function init(){
  const saved = localStorage.getItem("archgenie.apiKey");
  if (saved) $("apiKey").value = saved;
  $("appName").value = "secure Azure 3-tier web app";
  $("zoom").value = "fit";
})();

$("apiKey").addEventListener("change", () =>
  localStorage.setItem("archgenie.apiKey", $("apiKey").value.trim())
);

$("btnAzure").addEventListener("click", () => runAzure());
$("btnAWS").addEventListener("click", () => runMock("aws"));
$("btnGCP").addEventListener("click", () => runMock("gcp"));

$("copyTf").addEventListener("click", () =>
  navigator.clipboard.writeText($("tf").textContent || "")
);
$("downloadTf").addEventListener("click", () => {
  const blob = new Blob([$("tf").textContent || ""], { type: "text/plain" });
  $("downloadTf").href = URL.createObjectURL(blob);
});

$("zoom").addEventListener("change", () => applyZoom());
$("dlSvg").addEventListener("click", () => downloadSVG());
$("dlPng").addEventListener("click", () => downloadPNG());

function flash(msg, kind="info"){
  const el = $("status"); el.textContent = msg;
  el.style.color = kind==="ok" ? "#86efac" : kind==="err" ? "#fca5a5" : "";
  setTimeout(()=> el.textContent="", 4000);
}

function cleanFence(s){
  if(!s) return "";
  s = s.trim();
  const langs = ["mermaid","hcl","terraform","json"];
  for(const lang of langs){
    const re = new RegExp("^```"+lang+"\\s*\\n([\\s\\S]*?)```\\s*$","i");
    const m = s.match(re); if(m) return m[1].trim();
  }
  const generic = s.match(/^```[\s\S]*?\n([\s\S]*?)```$/);
  return generic ? generic[1].trim() : s;
}

async function runAzure(){
  const key = $("apiKey").value.trim();
  if(!key) return flash("Enter x-api-key", "err");
  const appName = $("appName").value.trim() || "3-tier web app";
  const extra = $("prompt").value.trim();

  flash("Calling Azure MCP…");
  try{
    const res = await fetch(`${apiBase}/mcp/azure/diagram-tf`, {
      method: "POST",
      headers: {"Content-Type":"application/json","x-api-key": key},
      body: JSON.stringify({ app_name: appName, prompt: extra || undefined })
    });
    if(!res.ok) throw new Error(await res.text());
    const data = await res.json();

    const diagram = cleanFence(data.diagram || "");
    const tf = cleanFence(data.terraform || "");
    renderDiagram(diagram);
    showTF(tf);

    // Show cost if present; if not, call /estimate with diagram/terraform
    if (data.cost) {
      renderCost(data.cost);
    } else {
      await estimateCost({ diagram, terraform: tf });
    }

    flash("Azure MCP response ready", "ok");
  }catch(e){
    console.error(e);
    flash(`Azure MCP error: ${e.message}`, "err");
  }
}

async function estimateCost({ items, diagram, terraform, region }){
  const key = $("apiKey").value.trim();
  try{
    const res = await fetch(`${apiBase}/estimate`, {
      method:"POST",
      headers: {"Content-Type":"application/json", "x-api-key": key},
      body: JSON.stringify({ items, diagram, terraform, region })
    });
    if(!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderCost(data.estimate);
  }catch(e){
    console.error(e);
    flash(`Cost estimate error: ${e.message}`, "err");
  }
}

async function runMock(which){
  const key = $("apiKey").value.trim();
  if(!key) return flash("Enter x-api-key", "err");
  flash(`Calling ${which.toUpperCase()} mock…`);
  try{
    const res = await fetch(`${apiBase}/mcp/${which}/diagram-tf`, { headers: {"x-api-key": key} });
    if(!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const diagram = cleanFence(data.diagram||"");
    const tf = cleanFence(data.terraform||"");
    renderDiagram(diagram);
    showTF(tf);
    // No live costing for AWS/GCP mocks; clear the cost table
    renderCost(null);
    flash(`${which.toUpperCase()} mock ready`, "ok");
  }catch(e){
    console.error(e);
    flash(`${which} mock error: ${e.message}`, "err");
  }
}

function renderDiagram(src){
  $("diagramSrc").textContent = src || "";
  const container = $("diagram");
  container.innerHTML = "";
  lastSVG = "";
  currentScale = $("zoom").value;

  if(!src.trim()){ container.textContent = "No diagram received"; return; }

  const id = "mmd-" + Math.random().toString(36).slice(2);
  mermaid.render(id, src).then(({svg}) => {
    const responsive = svg.replace(/width="[^"]+"/, 'width="100%"').replace(/height="[^"]+"/, '');
    container.innerHTML = responsive;
    lastSVG = container.querySelector("svg")?.outerHTML || "";
    applyZoom();
  }).catch(err => {
    container.innerHTML = `<pre class="code">${escapeHtml(String(err))}\n\n${escapeHtml(src)}</pre>`;
  });
}

function applyZoom(){
  const svg = $("diagram").querySelector("svg");
  if(!svg) return;
  const wrap = $("diagramWrap");
  const z = $("zoom").value;
  currentScale = z;
  svg.style.transformOrigin = "top left";
  svg.style.transform = "";
  if(z === "fit"){
    svg.style.width = "100%";
  } else {
    const factor = parseFloat(z) || 1;
    svg.style.width = "";
    svg.style.transform = `scale(${factor})`;
    wrap.scrollTop = 0; wrap.scrollLeft = 0;
  }
}

function showTF(tf){ $("tf").textContent = tf || ""; }

function downloadSVG(){
  if(!lastSVG){ flash("No diagram to download", "err"); return; }
  const blob = new Blob([lastSVG], {type: "image/svg+xml;charset=utf-8"});
  triggerDownload(blob, "archgenie-diagram.svg");
}

function downloadPNG(){
  if(!lastSVG){ flash("No diagram to download", "err"); return; }
  const img = new Image();
  const svgBlob = new Blob([lastSVG], {type: "image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(svgBlob);
  img.onload = () => {
    const scale = currentScale === "fit" ? 1 : parseFloat(currentScale) || 1;
    const w = img.width * scale, h = img.height * scale;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(w));
    canvas.height = Math.max(1, Math.floor(h));
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#0b1221";
    ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);
    canvas.toBlob((blob)=> triggerDownload(blob, "archgenie-diagram.png"));
  };
  img.onerror = () => { URL.revokeObjectURL(url); flash("PNG render failed", "err"); };
  img.src = url;
}

$("dlSvg").addEventListener("click", downloadSVG);
$("dlPng").addEventListener("click", downloadPNG);

function triggerDownload(blob, filename){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  requestAnimationFrame(()=> {
    URL.revokeObjectURL(a.href);
    document.body.removeChild(a);
  });
}

function renderCost(estimate){
  const table = $("costTable");
  const tbody = table.querySelector("tbody");
  const totalEl = $("costTotal");
  const sum = $("costSummary");
  const notesWrap = $("costNotesWrap");
  const notesList = $("costNotes");

  tbody.innerHTML = "";
  notesList.innerHTML = "";

  if(!estimate){
    sum.textContent = "No cost estimate available for this cloud.";
    table.classList.add("hidden");
    notesWrap.style.display = "none";
    return;
  }

  const { currency, total_estimate, items = [], notes = [] } = estimate;
  if (items.length === 0){
    sum.textContent = "No billable items detected.";
    table.classList.add("hidden");
    notesWrap.style.display = notes.length ? "block" : "none";
    notes.forEach(n => {
      const li = document.createElement("li");
      li.textContent = n;
      notesList.appendChild(li);
    });
    return;
  }

  items.forEach(it => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(it.cloud || "")}</td>
      <td>${escapeHtml(it.service || "")}</td>
      <td>${escapeHtml(it.sku || "")}</td>
      <td>${Number(it.qty || 0)}</td>
      <td>${escapeHtml(it.region || "")}</td>
      <td>${fmtMoney(it.unit_monthly, currency)}</td>
      <td>${fmtMoney(it.monthly, currency)}</td>
    `;
    tbody.appendChild(tr);
  });

  totalEl.textContent = fmtMoney(total_estimate, currency);
  sum.textContent = `Estimated monthly total: ${fmtMoney(total_estimate, currency)} (${estimate.method})`;
  table.classList.remove("hidden");

  if (notes && notes.length){
    notesWrap.style.display = "block";
    notes.forEach(n => {
      const li = document.createElement("li");
      li.textContent = n;
      notesList.appendChild(li);
    });
  } else {
    notesWrap.style.display = "none";
  }
}

function fmtMoney(v, currency="USD"){
  if (v === null || v === undefined) return "-";
  try { return new Intl.NumberFormat(undefined, {style:"currency", currency}).format(Number(v)); }
  catch { return `$${Number(v).toFixed(2)}`; }
}

function escapeHtml(s){ return (s||"").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }