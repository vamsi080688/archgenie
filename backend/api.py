import os
import re
import json
import time
import requests
from typing import List, Dict, Any, Tuple, Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# =========================
# Env & Config
# =========================
load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

USE_LIVE_AZURE_PRICES = os.getenv("USE_LIVE_AZURE_PRICES", "true").lower() == "true"
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "730"))

DEFAULT_REGION = os.getenv("DEFAULT_REGION", "eastus")

# =========================
# Auth
# =========================
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# =========================
# FastAPI
# =========================
app = FastAPI(title="ArchGenie Backend", version="7.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie backend alive"}

# =========================
# Azure OpenAI (MCP) client
# =========================
def _aoai_configured() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT)

def aoai_chat(messages: List[Dict[str, Any]], temperature: float = 0.2) -> Dict[str, Any]:
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/"
        f"{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY}
    body = {"messages": messages, "temperature": temperature}
    if AZURE_OPENAI_FORCE_JSON:
        body["response_format"] = {"type": "json_object"}
    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=90)
    if resp.status_code >= 300:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

# =========================
# Text helpers
# =========================
def strip_fences(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    for lang in ("mermaid", "hcl", "terraform", "json"):
        m = re.match(rf"^```{lang}\s*\n([\s\S]*?)```$", s, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    m = re.match(r"^```\s*\n?([\s\S]*?)```$", s)
    if m:
        return m.group(1).strip()
    return s

def extract_json_or_fences(content: str) -> Dict[str, Any]:
    if not content:
        return {"diagram": "", "terraform": ""}
    try:
        obj = json.loads(content)
        return {
            "diagram": strip_fences(obj.get("diagram", "")),
            "terraform": strip_fences(obj.get("terraform", "")),
            "cost": obj.get("cost"),
        }
    except Exception:
        pass
    out = {"diagram": "", "terraform": ""}
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["diagram"] = m.group(1).strip()
    m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["terraform"] = m.group(2).strip()
    return out

# =========================
# Mermaid sanitizer
# =========================
def sanitize_mermaid(src: str) -> str:
    """
    - subgraph "Title (region)" (no trailing ';')
    - Keep ';' at end of EDGE lines, remove it from NODE lines
    - Normalize '-. label .->' -> '-. |label| .->'
    - Insert newline after node ']' or ')' if followed by a token (fixes ']SP')
    - Remove commas inside [] labels
    - Ensure trailing newline
    """
    if not src:
        return src
    s = src

    s = re.sub(r'^\s*subgraph\s+([^\[\n"]+)\s*\(([^)]+)\)\s*;?\s*$',
               r'subgraph "\1 (\2)"', s, flags=re.MULTILINE)
    s = re.sub(r'^(?P<hdr>\s*subgraph\b[^\n;]*?);+\s*$', r'\g<hdr>', s, flags=re.MULTILINE)
    s = re.sub(r'-\.\s+([^.|><\-\n][^.|><\-\n]*?)\s+\.\->', r'-. |\1| .->', s)

    out_lines: List[str] = []
    for line in s.splitlines():
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            out_lines.append("")
            continue
        if stripped.startswith("subgraph") or stripped == "end":
            out_lines.append(stripped)
            continue
        is_edge = ("--" in stripped) or (".->" in stripped) or ("---" in stripped)
        if is_edge:
            if not stripped.endswith(";"):
                stripped += ";"
            out_lines.append(stripped)
        else:
            stripped = stripped.rstrip(";")
            out_lines.append(stripped)
    s = "\n".join(out_lines)

    s = re.sub(r'(\]|\))\s*(?=[A-Za-z0-9_]+\s*(?:-|\.))', r'\1\n', s)
    s = re.sub(r'\[(.*?)\]', lambda m: f"[{m.group(1).replace(',', '')}]", s)

    if not s.endswith("\n"):
        s += "\n"
    return s

# =========================
# Item normalization (ask/diagram/tf -> billable items)
# =========================
def normalize_to_items(ask: str = "", diagram: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION
    items: List[dict] = []
    blob = f"{ask}\n{diagram}\n{tf}".lower()

    def add(cloud, service, sku, qty=1, size_gb=None):
        d = {"cloud": cloud, "service": service, "sku": sku, "qty": max(1, int(qty)), "region": region}
        if size_gb is not None:
            d["size_gb"] = float(size_gb)
        items.append(d)

    if re.search(r"\bapp service\b|\bweb app\b", blob):
        qty = 2 if re.search(r"\bfront.*back|backend.*front", blob) else 1
        add("azure", "app_service", "S1", qty=qty)

    if re.search(r"\b(mssql|azure sql|sql database)\b", blob):
        add("azure", "azure_sql", "S0", qty=1)
        m = re.search(r"(\d+)\s*gb", blob)
        if m:
            items[-1]["size_gb"] = float(m.group(1))

    vm_hits = len(re.findall(r"\bvmss\b|\bvm scale set\b|\bvirtual machine\b|\bvm\b", blob))
    if vm_hits:
        add("azure", "vm", "B2s", qty=vm_hits)

    if re.search(r"\bstorage account\b|\bblob storage\b|\bazurerm_storage", blob):
        add("azure", "storage", "LRS", qty=1, size_gb=100)

    if re.search(r"\bapplication gateway\b|\bapp gateway\b|\bapp gw\b", blob):
        add("azure", "app_gateway", "WAF_v2", qty=1)
    elif re.search(r"\bload balancer\b|\blb\b", blob):
        add("azure", "lb", "Standard", qty=1)

    if "redis" in blob:
        add("azure", "redis", "C1", qty=1)

    if "aks" in blob or "kubernetes service" in blob:
        add("azure", "aks", "standard", qty=1)

    if "application insights" in blob or "monitor" in blob or "log analytics" in blob:
        add("azure", "monitor", "LogAnalytics", qty=1)

    return items

# =========================
# Azure Retail Prices — cache + helpers
# =========================
_price_cache: Dict[str, Tuple[float, float]] = {}

def cache_get(key: str) -> Optional[float]:
    v = _price_cache.get(key)
    if not v:
        return None
    price, exp = v
    return price if exp > time.time() else None

def cache_put(key: str, value: float, ttl_sec: int = 3600):
    _price_cache[key] = (value, time.time() + ttl_sec)

def azure_price_query(filter_str: str) -> Optional[Dict[str, Any]]:
    url = "https://prices.azure.com/api/retail/prices"
    params = {"api-version": "2023-01-01-preview", "$filter": filter_str}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code >= 300:
        return None
    j = r.json()
    items = j.get("Items") or []
    return items[0] if items else None

def monthly_from_retail(item: Dict[str, Any]) -> float:
    price = float(item.get("retailPrice") or 0.0)
    uom = (item.get("unitOfMeasure") or "").lower()
    if "hour" in uom:
        return round(price * HOURS_PER_MONTH, 2)
    return round(price, 2)

def azure_price_for_app_service_sku(sku: str, region: str) -> Optional[float]:
    key = f"az.appservice.{region}.{sku}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'App Service' and skuName eq '{sku}' and armRegionName eq '{region}'"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_vm_size(size: str, region: str) -> Optional[float]:
    key = f"az.vm.{region}.{size}"
    c = cache_get(key)
    if c is not None: return c
    candidates = [size, size.replace("_", " "), size.replace("v", " v")]
    for sku in candidates:
        f = f"serviceName eq 'Virtual Machines' and skuName eq '{sku}' and armRegionName eq '{region}'"
        item = azure_price_query(f)
        if item:
            monthly = monthly_from_retail(item)
            cache_put(key, monthly)
            return monthly
    return None

def azure_price_for_sql(sku: str, region: str) -> Optional[float]:
    key = f"az.sql.{region}.{sku}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'SQL Database' and skuName eq '{sku}' and armRegionName eq '{region}'"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_storage_lrs_per_gb(region: str) -> Optional[float]:
    key = f"az.storage.lrs.{region}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'Storage' and armRegionName eq '{region}' and contains(skuName, 'LRS')"
    item = azure_price_query(f)
    if not item: return None
    price = monthly_from_retail(item)  # per GB-month
    cache_put(key, price)
    return price

def azure_price_for_lb(region: str) -> Optional[float]:
    key = f"az.lb.standard.{region}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'Load Balancer' and armRegionName eq '{region}' and contains(skuName, 'Standard')"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_appgw_wafv2(region: str) -> Optional[float]:
    key = f"az.appgw.wafv2.{region}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'Application Gateway' and armRegionName eq '{region}' and contains(skuName, 'WAF_v2')"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_redis(sku: str, region: str) -> Optional[float]:
    key = f"az.redis.{region}.{sku}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'Azure Cache for Redis' and skuName eq '{sku}' and armRegionName eq '{region}'"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_log_analytics(region: str) -> Optional[float]:
    key = f"az.loganalytics.{region}"
    c = cache_get(key)
    if c is not None: return c
    f = f"serviceName eq 'Log Analytics' and armRegionName eq '{region}'"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

# =========================
# Pricing Engine
# =========================
def price_items(items: List[dict]) -> dict:
    currency = "USD"
    notes: List[str] = []
    total = 0.0
    out_items = []

    for it in items:
        cloud   = it.get("cloud","").lower()
        service = it.get("service","").lower()
        sku     = it.get("sku","")
        qty     = int(it.get("qty", 1) or 1)
        region  = it.get("region") or DEFAULT_REGION
        size_gb = float(it.get("size_gb", 0) or 0)
        hours   = float(it.get("hours", HOURS_PER_MONTH) or HOURS_PER_MONTH)

        unit_monthly: Optional[float] = None

        if cloud == "azure" and USE_LIVE_AZURE_PRICES:
            try:
                if service == "app_service":
                    unit_monthly = azure_price_for_app_service_sku(sku, region)
                elif service == "vm":
                    unit_monthly = azure_price_for_vm_size(sku, region)
                elif service == "azure_sql":
                    unit_monthly = azure_price_for_sql(sku, region)
                elif service == "storage":
                    per_gb = azure_price_for_storage_lrs_per_gb(region)
                    if per_gb is not None:
                        unit_monthly = per_gb * (size_gb if size_gb > 0 else 100.0)
                elif service == "lb":
                    unit_monthly = azure_price_for_lb(region)
                elif service == "app_gateway":
                    unit_monthly = azure_price_for_appgw_wafv2(region)
                elif service == "redis":
                    unit_monthly = azure_price_for_redis(sku, region)
                elif service == "monitor":
                    unit_monthly = azure_price_for_log_analytics(region)
                elif service == "aks":
                    notes.append("AKS control plane free; worker node VM costs not included.")
                    unit_monthly = 0.0
            except Exception as e:
                notes.append(f"Lookup failed for {cloud}:{service}:{sku} in {region}: {e}")
                unit_monthly = None

        if unit_monthly is None:
            notes.append(f"No price found for {cloud}:{service}:{sku} in {region} (set $0).")
            unit_monthly = 0.0

        monthly = float(unit_monthly)
        if hours and hours != HOURS_PER_MONTH and monthly > 0:
            monthly = monthly * (hours / HOURS_PER_MONTH)

        monthly = round(monthly * qty, 2)
        total += monthly

        out_items.append({
            "cloud": cloud, "service": service, "sku": sku,
            "qty": qty, "region": region,
            "size_gb": size_gb if size_gb > 0 else None,
            "hours": hours if hours and hours != HOURS_PER_MONTH else None,
            "unit_monthly": round(unit_monthly, 2),
            "monthly": monthly
        })

    return {
        "currency": currency,
        "total_estimate": round(total, 2),
        "method": "azure-retail" if USE_LIVE_AZURE_PRICES else "offline",
        "notes": notes,
        "items": out_items
    }

# =========================
# Public Endpoints
# =========================
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    """
    Generate Azure architecture (diagram + Terraform) via Azure MCP (AOAI).
    Strict: no local fallback. Returns sanitized diagram, terraform, and pricing.
    """
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")

    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""
    region = payload.get("region") or DEFAULT_REGION

    system = (
        "You are ArchGenie's Azure MCP.\n"
        "Return ONLY a single JSON object with keys:\n"
        '{\n'
        '  "diagram": "Mermaid code starting with: graph TD (or graph LR)",\n'
        '  "terraform": "Valid Terraform HCL for Azure (resource group, app service plan, web apps, sql, etc.)"\n'
        '}\n'
        "Do not write explanations, backticks, or any other keys. JSON only."
    )
    user = (
        f"Create an Azure architecture for: {app_name}.\n"
        f"Extra requirements: {extra}\n"
        f"Region: {region}\n"
        "Output JSON only."
    )

    result = aoai_chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])

    try:
        content = result["choices"][0]["message"]["content"]
    except Exception:
        raise HTTPException(status_code=502, detail=f"AOAI returned no content. Raw: {json.dumps(result)[:500]}")

    parsed = extract_json_or_fences(content)
    diagram_raw = (parsed.get("diagram") or "").strip()
    tf_raw      = (parsed.get("terraform") or "").strip()

    if not diagram_raw:
        raise HTTPException(status_code=502, detail=f"Model did not return 'diagram'. Content: {content[:500]}")
    if not diagram_raw.lower().startswith("graph "):
        raise HTTPException(status_code=502, detail=f"'diagram' must start with 'graph'. Got: {diagram_raw[:120]}")
    if not tf_raw:
        raise HTTPException(status_code=502, detail=f"Model did not return 'terraform'. Content: {content[:500]}")

    diagram = sanitize_mermaid(diagram_raw)
    tf      = strip_fences(tf_raw)

    items = normalize_to_items(ask=extra or app_name, diagram=diagram, tf=tf, region=region)
    estimate_obj = price_items(items)

    return {"diagram": diagram, "terraform": tf, "cost": estimate_obj}

# ----------------- AWS / GCP Mocks -----------------
@app.get("/mcp/aws/diagram-tf")
def aws_mock(_=Depends(require_api_key)):
    return {
        "diagram": """graph TD
  subgraph AWS
    A[ALB] --> B[EC2: web-1];
    B --> C[RDS: archgenie-db];
    B --> D[S3: assets];
  end
""",
        "terraform": """# mock demo
resource "aws_instance" "web" {
  ami           = "ami-123456"
  instance_type = "t3.micro"
}

resource "aws_s3_bucket" "assets" {
  bucket = "archgenie-assets"
}
"""
    }

@app.get("/mcp/gcp/diagram-tf")
def gcp_mock(_=Depends(require_api_key)):
    return {
        "diagram": """graph TD
  subgraph GCP
    A[Load Balancer] --> B[Compute Engine: web-1];
    B --> C[Cloud SQL: archgenie-db];
    B --> D[Cloud Storage: assets];
  end
""",
        "terraform": """# mock demo
resource "google_compute_instance" "web" {
  name         = "web-1"
  machine_type = "e2-micro"
  zone         = "us-central1-a"
}

resource "google_storage_bucket" "assets" {
  name     = "archgenie-assets"
  location = "US"
}
"""
    }