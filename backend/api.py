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

# Public Azure Retail Prices API (no account needed)
USE_LIVE_AZURE_PRICES = os.getenv("USE_LIVE_AZURE_PRICES", "true").lower() == "true"
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "730"))

# Default region when user doesn't provide one
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
app = FastAPI(title="ArchGenie Backend", version="5.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie backend alive"}

# =========================
# Azure OpenAI (optional)
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

def sanitize_mermaid(src: str) -> str:
    """
    Harden Mermaid for strict renderers:
      - Quote subgraph titles with parentheses: subgraph "Azure (eastus)"
      - Ensure link labels use pipes: '-. |hosts| .->'
      - Append semicolons on likely node/edge lines
      - Remove commas inside [...] labels
    """
    if not src:
        return src
    s = src

    # 1) subgraph Something (region)  ->  subgraph "Something (region)"
    s = re.sub(r'^\s*subgraph\s+([^\[\n"]+)\s*\(([^)]+)\)\s*$',
               r'subgraph "\1 (\2)"', s, flags=re.MULTILINE)

    # 2) '-. hosts .->' -> '-. |hosts| .->'
    s = re.sub(r'-\.\s+([^.|><\-\n][^.|><\-\n]*?)\s+\.\->', r'-. |\1| .->', s)

    # 3) Add semicolons to end of lines that look like node/edge statements
    def add_semicolon(line: str) -> str:
        raw = line.rstrip()
        if not raw or raw.startswith("subgraph") or raw == "end":
            return line
        if raw.endswith(";"):
            return line
        if re.search(r'(-->|\-\.\s*\|.*?\|\s*\.\->|---|\[.*\]|\(.*\))', raw):
            return raw + ";\n"
        return line
    s = "".join(add_semicolon(l) for l in s.splitlines(True))

    # 4) Remove commas inside [] labels to avoid edge-case parsers choking
    s = re.sub(r'\[(.*?)\]', lambda m: f"[{m.group(1).replace(',', '')}]", s)

    return s

# =========================
# Normalizer (ask/diagram/tf -> items)
# =========================
"""
item schema:
{
  "cloud": "azure",
  "service": "app_service" | "vm" | "azure_sql" | "storage" | "redis" | "lb" | "app_gateway" | "aks" | "monitor",
  "sku": "S1|P1v3|B2s|S0|LRS|C1|Standard|WAF_v2|...",
  "qty": 1,
  "region": "eastus",
  "size_gb": 100,   # optional for per-GB services
  "hours": 730      # optional if duty-cycle specified
}
"""
def normalize_to_items(ask: str = "", diagram: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION
    items: List[dict] = []
    blob = f"{ask}\n{diagram}\n{tf}".lower()

    def add(cloud, service, sku, qty=1, size_gb=None):
        d = {"cloud": cloud, "service": service, "sku": sku, "qty": max(1, int(qty)), "region": region}
        if size_gb is not None:
            d["size_gb"] = float(size_gb)
        items.append(d)

    # Heuristics (extend as needed)
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

    # Optionally let AOAI produce normalized items too
    if ask.strip() and _aoai_configured():
        try:
            system = (
                "Normalize the user's architecture request into a JSON array of items. "
                "Each item: {cloud:'azure', service:string, sku:string, qty:number, region:string, size_gb?:number, hours?:number}. "
                "Only output JSON, no markdown."
            )
            user = f"User request:\n{ask}\n\nDiagram:\n{diagram}\n\nTerraform:\n{tf}\nRegion default: {region}"
            resp = aoai_chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            content = resp["choices"][0]["message"]["content"]
            model_items = json.loads(content)
            if isinstance(model_items, list):
                items.extend(model_items)
        except Exception:
            pass

    # Merge duplicates
    merged: Dict[Tuple[str,str,str,str], dict] = {}
    for it in items:
        k = (it["cloud"], it["service"], it["sku"], it["region"])
        if k not in merged:
            merged[k] = it.copy()
        else:
            merged[k]["qty"] += int(it.get("qty", 1))
            if "size_gb" in it:
                merged[k]["size_gb"] = float(merged[k].get("size_gb", 0)) + float(it["size_gb"])
    return list(merged.values())

# =========================
# Azure Retail Prices — cache + lookups + dynamic fetch
# =========================
_price_cache: Dict[str, Tuple[float, float]] = {}  # key -> (price, expires_at)

def cache_get(key: str) -> Optional[float]:
    v = _price_cache.get(key)
    if not v: return None
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

def azure_retail_prices_fetch(filter_str: str, limit: int = 50) -> list:
    """
    Fetch public retail prices matching $filter. Follows nextPageLink up to 'limit' items.
    Docs: https://prices.azure.com/api/retail/prices
    """
    base = "https://prices.azure.com/api/retail/prices"
    params = {"api-version": "2023-01-01-preview", "$filter": filter_str}
    out = []
    url = base
    while True:
        r = requests.get(url, params=params if url == base else None, timeout=30)
        if r.status_code >= 300:
            raise HTTPException(status_code=r.status_code, detail=f"Retail prices error: {r.text}")
        j = r.json()
        items = j.get("Items") or []
        out.extend(items)
        if len(out) >= limit:
            return out[:limit]
        next_link = j.get("NextPageLink")
        if not next_link:
            break
        url = next_link
        params = None
    return out

def build_filter_from_payload(p: dict) -> str:
    """
    Build a robust $filter from structured fields.
    Supported keys: serviceName, armRegionName, meterName, skuName, productName, meterCategory.
    Also supports 'contains' for meterName/productName (pass as dict: {"meterName": "Transactions"}).
    """
    parts = []
    def eq(k):
        v = p.get(k)
        if v: parts.append(f"{k} eq '{v}'")
    def contains(k):
        v = (p.get("contains") or {}).get(k)
        if v: parts.append(f"contains({k}, '{v}')")
    for k in ["serviceName", "armRegionName", "meterName", "skuName", "productName", "meterCategory"]:
        eq(k)
    contains("meterName")
    contains("productName")
    if p.get("excludeZero", True):
        parts.append("retailPrice ne 0")
    return " and ".join(parts) if parts else "retailPrice ne 0"

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
    price = monthly_from_retail(item)  # usually per-GB per-month
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
                    notes.append("AKS estimate excludes worker node VMs / data-plane usage.")
                    unit_monthly = 0.0
            except Exception:
                unit_monthly = None

        if unit_monthly is None:
            notes.append(f"Price not found for {cloud}:{service}:{sku} in {region} (using $0 placeholder).")
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
# Local fallback (diagram + TF) so UI always renders
# =========================
def synthesize_3tier_from_prompt(app_name: str, extra: str, region: str) -> dict:
    """Always return a minimal, valid diagram + TF for 3-tier App Service + Azure SQL."""
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", app_name.lower())
    diagram = f"""graph TD
  U[User / Browser] --> FE[App Service: {app_name} Frontend];
  FE --> BE[App Service: {app_name} Backend];
  BE --> DB[(Azure SQL Database S0)];
  subgraph "Azure ({region})"
    SP[App Service Plan (Linux S1)];
    SP -. |hosts| .-> FE;
    SP -. |hosts| .-> BE;
    AI[(Application Insights)];
    FE -. |telemetry| .-> AI;
    BE -. |telemetry| .-> AI;
  end
"""
    tf = f'''terraform {{
  required_providers {{
    azurerm = {{ source = "hashicorp/azurerm", version = "~> 3.116" }}
  }}
}}
provider "azurerm" {{ features {{}} }}

resource "azurerm_resource_group" "rg" {{
  name     = "{safe_name}-rg"
  location = "{region}"
}}

resource "azurerm_service_plan" "plan" {{
  name                = "{safe_name}-plan"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  os_type             = "Linux"
  sku_name            = "S1"
}}

resource "azurerm_linux_web_app" "fe" {{
  name                = "{safe_name}-fe"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  service_plan_id     = azurerm_service_plan.plan.id
  https_only          = true
}}

resource "azurerm_linux_web_app" "be" {{
  name                = "{safe_name}-be"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  service_plan_id     = azurerm_service_plan.plan.id
  https_only          = true
}}

resource "azurerm_mssql_server" "sql" {{
  name                         = "{safe_name}-sqlsrv"
  resource_group_name          = azurerm_resource_group.rg.name
  location                     = azurerm_resource_group.rg.location
  version                      = "12.0"
  administrator_login          = "sqladminuser"
  administrator_login_password = "ChangeMe123!ChangeMe123!"
}}

resource "azurerm_mssql_database" "db" {{
  name        = "{safe_name}-db"
  server_id   = azurerm_mssql_server.sql.id
  sku_name    = "S0"
  max_size_gb = 32
}}
'''
    return {"diagram": diagram, "terraform": tf}

# =========================
# Public Endpoints
# =========================
@app.post("/estimate")
def estimate(payload: dict = Body(...), _=Depends(require_api_key)):
    """
    Estimate cost from:
      - items: [{cloud, service, sku, qty, region, size_gb?, hours?}]
      - or 'ask' (free text), 'diagram' (Mermaid), 'terraform' (HCL)
      - default region if not given: eastus
    """
    items = payload.get("items")
    region = payload.get("region") or DEFAULT_REGION
    if not items:
        items = normalize_to_items(
            ask=payload.get("ask", ""),
            diagram=payload.get("diagram", ""),
            tf=payload.get("terraform", ""),
            region=region,
        )
    estimate_obj = price_items(items)
    return {"items": items, "estimate": estimate_obj}

@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    """
    Returns: { diagram: "<mermaid>", terraform: "<hcl>", cost: {...} }
    Always returns a diagram/TF (fallback synthesis if AOAI is empty or down).
    """
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""
    region = payload.get("region") or DEFAULT_REGION

    diagram = ""
    tf = ""

    # 1) Try Azure OpenAI (if configured)
    if _aoai_configured():
        system = (
            "You are ArchGenie's Azure MCP.\n"
            "Return ONLY valid JSON with keys diagram and terraform. No fences.\n"
            "diagram: Mermaid code starting with 'graph'.\n"
            "terraform: Valid Azure HCL (RG, plan/web apps, Azure SQL)."
        )
        user = (
            f"Create an Azure architecture for: {app_name}.\n"
            f"Extra requirements (optional): {extra}\n"
            f"Region: {region}\n"
            "Respond JSON only."
        )
        try:
            result = aoai_chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            content = result["choices"][0]["message"]["content"]
            parsed = extract_json_or_fences(content)
            diagram = strip_fences(parsed.get("diagram", "")) or ""
            tf      = strip_fences(parsed.get("terraform", "")) or ""
        except Exception:
            diagram = ""
            tf = ""

    # 2) Fallback if AOAI empty or not configured
    if not diagram or not tf:
        synth = synthesize_3tier_from_prompt(app_name, extra, region)
        diagram = synth["diagram"]
        tf = synth["terraform"]

    # 3) Sanitize Mermaid for parser quirks
    diagram = sanitize_mermaid(diagram)

    # 4) Derive billable items and estimate cost
    items = normalize_to_items(ask=extra or app_name, diagram=diagram, tf=tf, region=region)
    estimate_obj = price_items(items)

    return {"diagram": diagram, "terraform": tf, "cost": estimate_obj}

# ---------- Dynamic Retail Prices (public) ----------
@app.post("/pricing/azure/retail")
def pricing_azure_retail(payload: dict = Body(default={}), _=Depends(require_api_key)):
    """
    Dynamic public Retail Prices lookup (no account needed).

    Body (any of the following):
    {
      // Option A: raw filter
      "filter": "serviceName eq 'App Service' and skuName eq 'S1' and armRegionName eq 'eastus'",

      // Option B: structured fields (we’ll build $filter for you)
      "serviceName": "App Service",
      "skuName": "S1",
      "armRegionName": "eastus",
      "meterCategory": "Compute",
      "productName": "App Service",
      "meterName": "Linux Plan S1",
      "contains": { "meterName": "S1" },   // optional: contains() support
      "excludeZero": true,                  // optional, default true

      // Controls
      "limit": 10,                          // default 50
      "mode": "first" | "all",              // default "first"

      // Optional convenience for a quick monthly:
      "qty": 1,
      "hours_per_month": 730
    }
    """
    limit = int(payload.get("limit", 50))
    mode  = payload.get("mode", "first")
    raw   = payload.get("filter")
    if not raw:
        raw = build_filter_from_payload(payload)

    items = azure_retail_prices_fetch(raw, limit=limit)
    if mode == "first":
        items = items[:1]

    qty = float(payload.get("qty", 1) or 1)
    hours = float(payload.get("hours_per_month", HOURS_PER_MONTH) or HOURS_PER_MONTH)

    enriched = []
    for it in items:
        uom = (it.get("unitOfMeasure") or "").lower()
        price = float(it.get("retailPrice") or 0)
        est_monthly = None
        if "hour" in uom:
            est_monthly = round(price * hours * qty, 2)
        elif "month" in uom:
            est_monthly = round(price * qty, 2)
        elif "gb" in uom:
            # treat qty as GB-month if caller passes size
            est_monthly = round(price * qty, 2)

        enriched.append({
            "serviceName": it.get("serviceName"),
            "productName": it.get("productName"),
            "skuName": it.get("skuName"),
            "meterName": it.get("meterName"),
            "armRegionName": it.get("armRegionName"),
            "currencyCode": it.get("currencyCode"),
            "unitOfMeasure": it.get("unitOfMeasure"),
            "retailPrice": price,
            "effectiveStartDate": it.get("effectiveStartDate"),
            "type": it.get("type"),
            "estimatedMonthly": est_monthly
        })

    return {
        "filter": raw,
        "count": len(enriched),
        "items": enriched
    }

# ----------------- AWS / GCP Mocks (diagrams + TF) -----------------
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
