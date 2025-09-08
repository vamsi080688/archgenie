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
app = FastAPI(title="ArchGenie Backend", version="4.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie backend alive"}

# =========================
# Azure OpenAI
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
# Helpers
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
# Normalizer (ask/diagram/tf -> items)
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

    return items

# =========================
# Azure Pricing (Retail API)
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
    if c is not None:
        return c
    f = f"serviceName eq 'App Service' and skuName eq '{sku}' and armRegionName eq '{region}'"
    item = azure_price_query(f)
    if not item: return None
    monthly = monthly_from_retail(item)
    cache_put(key, monthly)
    return monthly

def azure_price_for_sql(sku: str, region: str) -> Optional[float]:
    key = f"az.sql.{region}.{sku}"
    c = cache_get(key)
    if c is not None:
        return c
    f = f"serviceName eq 'SQL Database' and skuName eq '{sku}' and armRegionName eq '{region}'"
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
        qty     = int(it.get("qty",1) or 1)
        region  = it.get("region") or DEFAULT_REGION

        unit_monthly: Optional[float] = None
        if cloud == "azure" and USE_LIVE_AZURE_PRICES:
            try:
                if service == "app_service":
                    unit_monthly = azure_price_for_app_service_sku(sku, region)
                elif service == "azure_sql":
                    unit_monthly = azure_price_for_sql(sku, region)
            except Exception:
                unit_monthly = None
        if unit_monthly is None:
            unit_monthly = 0.0
            notes.append(f"Price not found for {cloud}:{service}:{sku} in {region}, using $0.")

        monthly = round(unit_monthly * qty, 2)
        total += monthly
        out_items.append({**it, "unit_monthly": unit_monthly, "monthly": monthly})

    return {"currency": currency, "total_estimate": round(total,2), "notes": notes, "items": out_items}

# =========================
# Fallback synthesizer
# =========================
def synthesize_3tier_from_prompt(app_name: str, extra: str, region: str) -> dict:
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", app_name.lower())
    diagram = f"""graph TD
  U[User / Browser] --> FE[App Service: {app_name} Frontend]
  FE --> BE[App Service: {app_name} Backend]
  BE --> DB[(Azure SQL Database S0)]
  subgraph AZ[Azure ({region})]
    SP[App Service Plan (Linux, S1)]
    SP -. hosts .-> FE
    SP -. hosts .-> BE
    AI[(Application Insights)]
    FE -. telemetry .-> AI
    BE -. telemetry .-> AI
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
  location            = var.location
  service_plan_id     = azurerm_service_plan.plan.id
  https_only          = true
}}

resource "azurerm_linux_web_app" "be" {{
  name                = "{safe_name}-be"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  service_plan_id     = azurerm_service_plan.plan.id
  https_only          = true
}}

resource "azurerm_mssql_server" "sql" {{
  name                         = "{safe_name}-sqlsrv"
  resource_group_name          = azurerm_resource_group.rg.name
  location                     = var.location
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
# Endpoints
# =========================
@app.post("/estimate")
def estimate(payload: dict = Body(...), _=Depends(require_api_key)):
    items = payload.get("items")
    region = payload.get("region") or DEFAULT_REGION
    if not items:
        items = normalize_to_items(payload.get("ask",""), payload.get("diagram",""), payload.get("terraform",""), region=region)
    return {"items": items, "estimate": price_items(items)}

@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""
    region = payload.get("region") or DEFAULT_REGION
    diagram, tf = "", ""

    if _aoai_configured():
        try:
            result = aoai_chat([
                {"role": "system", "content": "Return JSON with keys diagram and terraform."},
                {"role": "user", "content": f"Create Azure architecture for {app_name} with {extra} in {region}"}
            ])
            content = result["choices"][0]["message"]["content"]
            parsed = extract_json_or_fences(content)
            diagram = strip_fences(parsed.get("diagram","")) or ""
            tf = strip_fences(parsed.get("terraform","")) or ""
        except Exception:
            diagram, tf = "", ""

    if not diagram or not tf:
        synth = synthesize_3tier_from_prompt(app_name, extra, region)
        diagram, tf = synth["diagram"], synth["terraform"]

    items = normalize_to_items(app_name, diagram, tf, region)
    return {"diagram": diagram, "terraform": tf, "cost": price_items(items)}

@app.get("/mcp/aws/diagram-tf")
def aws_mock(_=Depends(require_api_key)):
    return {"diagram":"graph TD; A[ALB]-->B[EC2]; B-->C[RDS]", "terraform":"# mock AWS"}

@app.get("/mcp/gcp/diagram-tf")
def gcp_mock(_=Depends(require_api_key)):
    return {"diagram":"graph TD; A[LB]-->B[GCE]; B-->C[Cloud SQL]", "terraform":"# mock GCP"}