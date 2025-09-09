// Mermaid init (we render manually via API)
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });

const el = (id) => document.getElementById(id);
const appNameInput = el('appName');
const promptInput  = el('prompt');
const regionInput  = el('region');
const apiKeyInput  = el('apiKey');
const btnGenerate  = el('btnGenerate');
const statusEl     = el('status');
const diagramHost  = el('diagramHost');
const btnSvg       = el('btnSvg');
const btnPng       = el('btnPng');
const tfOut        = el('tfOut');
const btnCopyTf    = el('btnCopyTf');
const btnDlTf      = el('btnDlTf');
const pricingDiv   = el('pricing');

let lastSvg = '';
let lastDiagram = '';
let lastTf = '';
let lastCost = null;

function lastMileSanitize(diagram) {
  diagram = diagram.replace(/^(\s*subgraph[^\n;]*);+\s*$/gm, '$1');
  diagram = diagram.replace(/(\]|\))\s*(?=[A-Za-z0-9_]+\s*(?:-|\.))/g, '$1\n');
  return diagram;
}

async function callAzureMcp() {
  const appName = appNameInput.value.trim() || '3-tier web app';
  const prompt  = promptInput.value.trim();
  const region  = regionInput.value.trim();
  const apiKey  = apiKeyInput.value.trim();

  if (!apiKey) {
    statusEl.textContent = 'Please enter your x-api-key.';
    return;
  }

  btnGenerate.disabled = true;
  statusEl.textContent = 'Generating...';

  try {
    const body = { app_name: appName, prompt };
    if (region) body.region = region;

    const res = await fetch('/api/mcp/azure/diagram-tf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': apiKey },
      body: JSON.stringify(body)
    });

    if (!res.ok) {
      const errText = await res.text();
      diagramHost.innerHTML = `<pre class="mermaid">${escapeHtml(errText)}</pre>`;
      throw new Error(`Backend error (${res.status})`);
    }

    const data = await res.json();
    lastDiagram = (data.diagram || '').trim();
    lastTf      = (data.terraform || '').trim();
    lastCost    = data.cost || null;

    const safeDiagram = lastMileSanitize(lastDiagram);
    await renderMermaidToSvg(safeDiagram);
    renderTerraform(lastTf);
    renderPricing(lastCost);

    statusEl.textContent = 'Done.';
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || 'Request failed.';
    if (!diagramHost.innerHTML) {
      diagramHost.innerHTML = `<pre class="mermaid">${escapeHtml(lastDiagram || '(no diagram)')}</pre>`;
    }
  } finally {
    btnGenerate.disabled = false;
  }
}

async function renderMermaidToSvg(diagramText) {
  const id = 'arch-' + Math.random().toString(36).slice(2, 9);
  try {
    const { svg } = await mermaid.render(id, diagramText);
    lastSvg = svg;
    diagramHost.innerHTML = svg;
    diagramHost.querySelector('svg')?.setAttribute('width', '100%');
  } catch (err) {
    console.error('Mermaid render error', err);
    diagramHost.innerHTML = `<pre class="mermaid">${escapeHtml(diagramText)}</pre>`;
  }
}

function renderTerraform(tf) { tfOut.value = tf || ''; }

function renderPricing(costObj) {
  if (!costObj || !Array.isArray(costObj.items)) {
    pricingDiv.innerHTML = '<p class="muted">No cost data.</p>';
    return;
  }
  const rows = costObj.items.map(it => {
    const size = it.size_gb ? `${it.size_gb} GB` : '';
    const hours = it.hours ? `${it.hours} h/mo` : '';
    return `
      <tr>
        <td>${it.cloud}</td>
        <td>${it.service}</td>
        <td>${escapeHtml(it.sku || '')}</td>
        <td>${it.region}</td>
        <td style="text-align:right">${it.qty}</td>
        <td>${size}</td>
        <td>${hours}</td>
        <td style="text-align:right">$${Number(it.unit_monthly || 0).toFixed(2)}</td>
        <td style="text-align:right">$${Number(it.monthly || 0).toFixed(2)}</td>
      </tr>`;
  }).join('');
  const total = Number(costObj.total_estimate || 0).toFixed(2);
  const notes = (costObj.notes || []).map(n => `<li>${escapeHtml(n)}</li>`).join('');
  pricingDiv.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Cloud</th><th>Service</th><th>SKU</th><th>Region</th>
          <th style="text-align:right">Qty</th><th>Size</th><th>Hours</th>
          <th style="text-align:right">Unit/Month</th>
          <th style="text-align:right">Monthly</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
      <tfoot>
        <tr>
          <td colspan="8" style="text-align:right">Total (${costObj.currency || 'USD'})</td>
          <td style="text-align:right">$${total}</td>
        </tr>
      </tfoot>
    </table>
    ${notes ? `<p style="margin-top:8px"><strong>Notes:</strong></p><ul>${notes}</ul>` : ''}
  `;
}

function escapeHtml(s) { return (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

btnSvg.addEventListener('click', () => {
  if (!lastSvg) return;
  const blob = new Blob([lastSvg], { type: 'image/svg+xml;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'archgenie-diagram.svg';
  a.click(); URL.revokeObjectURL(a.href);
});

btnPng.addEventListener('click', async () => {
  if (!lastSvg) return;
  const svgEl = new DOMParser().parseFromString(lastSvg, 'image/svg+xml').documentElement;
  const svgText = new XMLSerializer().serializeToString(svgEl);
  const canvas = document.createElement('canvas');
  const bbox = diagramHost.querySelector('svg')?.getBBox?.();
  const width = Math.max(1024, (bbox?.width || 1024));
  const height = Math.max(768, (bbox?.height || 768));
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d');
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0);
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = 'archgenie-diagram.png';
    a.click();
  };
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
});

btnCopyTf.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(tfOut.value || '');
    btnCopyTf.textContent = 'Copied!';
    setTimeout(() => btnCopyTf.textContent = 'Copy', 1000);
  } catch(e) { console.error(e); }
});

btnDlTf.addEventListener('click', () => {
  const blob = new Blob([tfOut.value || ''], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'main.tf';
  a.click(); URL.revokeObjectURL(a.href);
});

el('btnGenerate').addEventListener('click', callAzureMcp);