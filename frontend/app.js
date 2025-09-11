/* global mermaid */

const $ = (sel) => document.querySelector(sel);
const statusEl = $("#status");
const diagHost = $("#diagramHost");
const tfOut = $("#tfOut");
const pricingHost = $("#pricing");

function setStatus(text, ok = true) {
  statusEl.textContent = text;
  statusEl.classList.toggle("err", !ok);
}

function headersJSON() {
  const key = ($("#apiKey").value || "").trim() || "super-secret-key";
  return { "x-api-key": key, "Content-Type": "application/json" };
}

// --------------------- Diagram helpers ---------------------
function isSvgString(s) {
  return typeof s === "string" && s.trim().startsWith("<svg");
}

function currentSvgString() {
  // If diagramHost contains an <svg>, return it
  const svg = diagHost.querySelector("svg");
  if (svg) return svg.outerHTML;
  // If Mermaid rendered into a DIV with innerHTML as SVG, grab it
  if (isSvgString(diagHost.innerHTML)) return diagHost.innerHTML;
  return "";
}

function renderMermaid(src) {
  const clean = cleanMermaid(src || "");
  diagHost.innerHTML = `<div class="mermaid">${clean}</div>`;
  try {
    mermaid.parse(clean);
    mermaid.run({ nodes: [diagHost] });
  } catch (e) {
    diagHost.innerHTML = `<div class="err">Mermaid parse error: ${e?.str || e?.message}</div><pre>${clean}</pre>`;
  }
}

function renderSvg(svgText) {
  diagHost.innerHTML = svgText;
}

function cleanMermaid(text) {
  if (!text) return "graph TD\nA[Empty]\n";
  let s = text.trim().replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  s = s.replace(/^```(?:mermaid)?\s*\n/i, "").replace(/\n```$/, "");
  if (!/^graph\s+(TD|LR)\b/i.test(s)) s = "graph TD\n" + s;
  // Quote subgraph titles and normalize edges
  s = s.replace(/^\s*subgraph\s+([^\n;]+)\s*;?\s*$/gm, (m, p1) => `subgraph "${p1.trim()}"`);
  s = s.replace(/-\.\s+([^.|><\-\n][^.|><\-\n]*?)\s+\.->/g, "-. |$1| .->");
  // Ensure edge lines end with ';', node lines not
  s = s
    .split("\n")
    .map((line) => {
      let t = line.trim();
      if (!t) return "";
      const isSub = t.startsWith("subgraph"), isEnd = t === "end";
      const isEdge = t.includes("--") || t.includes(".->") || t.includes("---");
      t = t.replace(/\[(.*?)\]/g, (m, a) => "[" + a.replace(/,/g, "") + "]");
      if (isSub) return t.replace(/;+\s*$/, "");
      if (isEnd) return "end";
      if (isEdge && !t.endsWith(";")) t += ";";
      if (!isEdge) t = t.replace(/;+\s*$/, "");
      return t;
    })
    .join("\n");
  // Balance subgraphs
  const opens = (s.match(/^\s*subgraph\b/mg) || []).length;
  const ends = (s.match(/^\s*end\s*$/mg) || []).length;
  if (ends < opens) s += "\n" + "end\n".repeat(opens - ends);
  if (!s.endsWith("\n")) s += "\n";
  return s;
}

// --------------------- Pricing rendering ---------------------
function renderPricing(cost) {
  if (!cost || !Array.isArray(cost.items)) {
    pricingHost.innerHTML = '<div class="muted">No pricing data.</div>';
    return;
  }
  const currency = cost.currency || "USD";
  const rows = cost.items
    .map((it) => {
      const size = it.size_gb ? `${it.size_gb} GB` : "";
      const hrs = it.hours ? `${it.hours}` : "";
      const unit = (it.unit_monthly ?? 0).toFixed(2);
      const mo = (it.monthly ?? 0).toFixed(2);
      return `<tr>
        <td>${it.cloud || ""}</td>
        <td>${it.service || ""}</td>
        <td>${it.sku || ""}</td>
        <td>${it.region || ""}</td>
        <td>${it.qty || 1}</td>
        <td>${size}</td>
        <td>${hrs}</td>
        <td>$${unit}</td>
        <td>$${mo}</td>
      </tr>`;
    })
    .join("");
  const notes = (cost.notes || []).map((n) => `<li>${n}</li>`).join("");

  pricingHost.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>Cloud</th><th>Service</th><th>SKU</th><th>Region</th>
          <th>Qty</th><th>Size</th><th>Hours</th><th>Unit/Month</th><th>Monthly</th>
        </tr>
      </thead>
      <tbody>${rows || `<tr><td colspan="9" class="muted">No items</td></tr>`}</tbody>
      <tfoot>
        <tr>
          <td colspan="8" style="text-align:right"><strong>Total (${currency})</strong></td>
          <td><strong>$${(cost.total_estimate ?? 0).toFixed(2)}</strong></td>
        </tr>
      </tfoot>
    </table>
    ${notes ? `<div class="notes"><strong>Notes:</strong><ul>${notes}</ul></div>` : ""}
  `;
}

// --------------------- Actions ---------------------
async function generateAzure() {
  setStatus("generating (Azure)...");
  pricingHost.innerHTML = "";
  tfOut.value = "";
  const body = {
    app_name: $("#appName").value.trim(),
    prompt: $("#prompt").value.trim(),
    region: ($("#region").value || "").trim() || undefined,
  };
  const res = await fetch("/api/mcp/azure/diagram-tf", {
    method: "POST",
    headers: headersJSON(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    setStatus("error", false);
    const t = await res.text();
    alert("Azure MCP error:\n" + t);
    return;
  }
  const out = await res.json();
  if (out.diagram && out.diagram.startsWith("graph ")) {
    renderMermaid(out.diagram);
  } else if (out.diagram_svg) {
    renderSvg(out.diagram_svg);
  } else {
    diagHost.innerHTML = '<div class="muted">No diagram received.</div>';
  }
  tfOut.value = out.terraform || "";
  if (out.cost) renderPricing(out.cost);
  setStatus("ready");
}

async function generateAws() {
  setStatus("generating (AWS)...");
  pricingHost.innerHTML = "";
  tfOut.value = "";

  // Preferred endpoint (diagram + tf + cost). If not present in your backend yet, we fallback to mock.
  const tryEndpoints = [
    { url: "/api/mcp/aws/diagram-tf-cost", body: { prompt: $("#prompt").value.trim(), format: "svg" } },
    { url: "/api/mcp/aws/diagram-tf", body: {} }, // mock (diagram+tf only)
  ];

  let out = null, ok = false, used = null;
  for (const ep of tryEndpoints) {
    const res = await fetch(ep.url, {
      method: "POST",
      headers: headersJSON(),
      body: JSON.stringify(ep.body),
    }).catch(() => null);
    if (res && res.ok) {
      out = await res.json();
      ok = true;
      used = ep.url;
      break;
    }
  }
  if (!ok) {
    setStatus("error", false);
    alert("AWS MCP call failed (both /diagram-tf-cost and /diagram-tf). Check proxy/server.");
    return;
  }

  if (out.diagram_svg) {
    renderSvg(out.diagram_svg);
  } else if (out.diagram && out.diagram.startsWith("graph ")) {
    renderMermaid(out.diagram); // in case your backend returns Mermaid for AWS
  } else {
    diagHost.innerHTML = '<div class="muted">No diagram received.</div>';
  }

  tfOut.value = out.terraform || "";
  if (out.cost) renderPricing(out.cost);
  setStatus(`ready (${used})`);
}

// --------------------- Downloads / clipboard ---------------------
$("#btnSvg").addEventListener("click", () => {
  const svgStr = currentSvgString();
  if (!svgStr) return alert("No SVG to download yet.");
  const blob = new Blob([svgStr], { type: "image/svg+xml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "architecture.svg";
  a.click();
  URL.revokeObjectURL(url);
});

$("#btnPng").addEventListener("click", async () => {
  const svgStr = currentSvgString();
  if (!svgStr) return alert("No SVG to convert yet.");

  // Convert SVG -> PNG via canvas
  const img = new Image();
  const svgBlob = new Blob([svgStr], { type: "image/svg+xml" });
  const url = URL.createObjectURL(svgBlob);

  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
    img.src = url;
  });

  const w = img.naturalWidth || 1600;
  const h = img.naturalHeight || 900;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0);

  URL.revokeObjectURL(url);
  canvas.toBlob((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "architecture.png";
    a.click();
    URL.revokeObjectURL(a.href);
  }, "image/png");
});

$("#btnCopyTf").addEventListener("click", async () => {
  const text = tfOut.value || "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    setStatus("Terraform copied");
  } catch {
    setStatus("Copy failed", false);
  }
});

$("#btnDlTf").addEventListener("click", () => {
  const text = tfOut.value || "";
  if (!text) return;
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "main.tf";
  a.click();
  URL.revokeObjectURL(url);
});

// --------------------- Wire buttons ---------------------
$("#btnGenerate").addEventListener("click", generateAzure);
$("#btnGenerateAws").addEventListener("click", generateAws);

// Init mermaid
mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "dark" });
setStatus("ready");