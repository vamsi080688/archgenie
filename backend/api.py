import os, re, io, json, time, base64, zipfile
from typing import List, Dict, Any, Optional, Tuple

import requests
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# =========================
# Env & Config
# =========================
CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

# Azure OpenAI (used for Azure MCP and to synthesize AWS Terraform)
AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

# Azure defaults (same behavior you had)
DEFAULT_REGION_AZURE = os.getenv("DEFAULT_REGION", "eastus")
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "730"))

# AWS Diagram MCP (run locally via mcp-proxy)
AWS_DIAGRAM_MCP_HTTP = os.getenv("AWS_DIAGRAM_MCP_HTTP", "http://127.0.0.1:3333")

# AWS defaults
DEFAULT_REGION_AWS = os.getenv("DEFAULT_REGION_AWS", "us-east-1")

# =========================
# App & Auth
# =========================
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie MCP Backend", version="9.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie MCP backend up"}

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
    if AZURE_OPENAI_FORCE_JSON:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
    if r.status_code >= 300:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

# =========================
# Helpers: code extraction & Mermaid sanitizing
# =========================
def strip_fences(text: str) -> str:
    if not text: return ""
    s = text.strip()
    for lang in ("mermaid","hcl","terraform","json"):
        m = re.match(rf"^```{lang}\s*\n([\s\S]*?)```$", s, flags=re.I)
        if m: return m.group(1).strip()
    m = re.match(r"^```\s*\n?([\s\S]*?)```$", s)
    return (m.group(1).strip() if m else s)

def extract_json_or_fences(content: str) -> Dict[str, Any]:
    if not content: return {"diagram":"", "terraform":""}
    try:
        obj = json.loads(content)
        return {
            "diagram": strip_fences(obj.get("diagram","")),
            "terraform": strip_fences(obj.get("terraform","")),
        }
    except Exception:
        pass
    out = {"diagram":"", "terraform":""}
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, flags=re.I)
    if m: out["diagram"] = m.group(1).strip()
    m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, flags=re.I)
    if m: out["terraform"] = m.group(2).strip()
    return out

def sanitize_mermaid(src: str) -> str:
    if not src: return "graph TD\nA[Empty]\n"
    s = src.strip().replace("\r\n","\n").replace("\r","\n")
    s = s.replace(/^```(?:mermaid)?\n/i,"") if re.search(r"^```", s) else s
    if not re.match(r"^graph\s+(TD|LR)\b", s, flags=re.I):
        s = "graph TD\n" + s
    # quote subgraph titles; normalize dotted edges
    s = re.sub(r"^\s*subgraph\s+([^\n;]+)\s*;?\s*$", lambda m: f'subgraph "{m.group(1).strip()}"', s, flags=re.M)
    s = re.sub(r'-\.\s+([^.|><\-\n][^.|><\-\n]*?)\s+\.->', r'-. |\1| .->', s)
    lines = []
    for line in s.splitlines():
        t = line.strip()
        if not t: lines.append(""); continue
        isSub = t.startswith("subgraph"); isEnd = (t=="end")
        isEdge = ("--" in t) or (".->" in t) or ("---" in t)
        t = re.sub(r'\[(.*?)\]', lambda m: "["+m.group(1).replace(",","")+"]", t)
        if isSub: lines.append(t.rstrip(";")); continue
        if isEnd: lines.append("end"); continue
        if isEdge and not t.endswith(";"): t += ";"
        if not isEdge: t = t.rstrip(";")
        lines.append(t)
    s = "\n".join(lines)
    opens = len(re.findall(r'^\s*subgraph\b', s, flags=re.M))
    ends  = len(re.findall(r'^\s*end\s*$', s, flags=re.M))
    if ends < opens: s += "\n" + "end\n" * (opens-ends)
    if not s.endswith("\n"): s += "\n"
    return s

# =========================
# Azure: normalize + live pricing (unchanged logic shape)
# =========================
def normalize_azure_items(ask: str = "", diagram: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION_AZURE
    items: List[dict] = []
    blob = f"{ask}\n{diagram}\n{tf}".lower()

    def add(service, sku, qty=1, size_gb=None, extra=None):
        d = {"cloud":"azure","service":service,"sku":sku,"qty":max(1,int(qty)),"region":region}
        if size_gb is not None: d["size_gb"] = float(size_gb)
        if extra: d.update(extra)
        items.append(d)

    if re.search(r"\bapp service\b|\bweb app\b", blob):
        qty = 2 if re.search(r"\bfront.*back|backend.*front", blob) else 1
        add("app_service","S1",qty=qty)
    if re.search(r"\b(mssql|azure sql|sql database)\b", blob):
        add("azure_sql","S0",qty=1)
        m = re.search(r"(\d+)\s*gb", blob)
        if m: items[-1]["size_gb"] = float(m.group(1))
    if re.search(r"\bapplication gateway\b|\bapp gateway\b|\bapp gw\b", blob): add("app_gateway","WAF_v2")
    if re.search(r"\bload balancer\b|\blb\b", blob): add("lb","Standard")
    if "redis" in blob: add("redis","C1")
    if "application insights" in blob or "log analytics" in blob: add("monitor","LogAnalytics")
    return items

# ---- Azure retail pricing via public API (kept) ----
_price_cache: Dict[str, Tuple[Any, float]] = {}

def cache_get(key: str):
    v = _price_cache.get(key); 
    return None if not v or v[1] <= time.time() else v[0]
def cache_put(key: str, value, ttl=3600): _price_cache[key] = (value, time.time()+ttl)

def azure_prices(filter_str: str, limit:int=120) -> list:
    base="https://prices.azure.com/api/retail/prices"
    url=base; params={"api-version":"2023-01-01-preview","$filter":filter_str}; out=[]; seen=0
    for _ in range(20):
        r=requests.get(url, params=params if url==base else None, timeout=30)
        if r.status_code>=300: return out
        j=r.json(); items=j.get("Items") or []
        out.extend(items); seen+=len(items)
        if seen>=limit or not j.get("NextPageLink"): break
        url=j["NextPageLink"]; params=None
    return out

def monthly_from(item: Dict[str,Any]) -> float:
    price=float(item.get("retailPrice") or 0.0)
    uom=(item.get("unitOfMeasure") or "").lower()
    return round(price*HOURS_PER_MONTH,2) if "hour" in uom else round(price,2)

def az_price_app_service(sku:str, region:str)->Optional[float]:
    key=f"az.app.{region}.{sku}"; c=cache_get(key); 
    if c is not None: return c
    for svc in ["App Service","App Service Linux","Azure App Service","App Service Plans"]:
        flt=f"serviceName eq '{svc}' and skuName eq '{sku}' and armRegionName eq '{region}' and retailPrice ne 0"
        items=azure_prices(flt,100)
        if items:
            m=min([monthly_from(x) for x in items])
            cache_put(key,m); return m
    return None

def az_price_sql(sku:str, region:str)->Optional[float]:
    key=f"az.sql.{region}.{sku}"; c=cache_get(key)
    if c is not None: return c
    flt=f"serviceName eq 'SQL Database' and skuName eq '{sku}' and armRegionName eq '{region}' and retailPrice ne 0"
    items=azure_prices(flt,200)
    if not items: return None
    m=min([monthly_from(x) for x in items])
    cache_put(key,m); return m

def az_price_appgw(region:str)->Optional[Dict[str,float]]:
    key=f"az.appgw.{region}"; c=cache_get(key)
    if c: return c
    flt=f"serviceName eq 'Application Gateway' and armRegionName eq '{region}' and retailPrice ne 0"
    items=azure_prices(flt,200)
    base=None; cu=None
    for it in items:
        meter=(it.get("meterName") or "").lower()
        uom=(it.get("unitOfMeasure") or "").lower()
        m=monthly_from(it)
        if "capacity unit" in meter and "hour" in uom: cu = m
        elif "gateway" in meter and "hour" in uom: base = m
    if base is None and cu is None: return None
    c={"base_monthly":round(base or 0,2),"capacity_unit_monthly":round(cu or 0,2)}
    cache_put(key,c); return c

def az_price_lb(region:str)->Optional[Dict[str,float]]:
    key=f"az.lb.{region}"; c=cache_get(key)
    if c: return c
    flt=f"serviceName eq 'Load Balancer' and armRegionName eq '{region}' and retailPrice ne 0"
    items=azure_prices(flt,200)
    rule=None; data=None
    for it in items:
        meter=(it.get("meterName") or "").lower(); uom=(it.get("unitOfMeasure") or "").lower()
        m=monthly_from(it)
        if "rule" in meter and "hour" in uom: rule=m
        if ("data" in meter or "processed" in meter) and ("gb" in uom): data=m
    if rule is None and data is None: return None
    c={"rule_hour_monthly":round(rule or 0,4),"data_gb_monthly":round(data or 0,4)}
    cache_put(key,c); return c

def az_price_log_analytics(region:str)->Optional[float]:
    key=f"az.log.{region}"; c=cache_get(key)
    if c is not None: return c
    flt=f"serviceName eq 'Log Analytics' and armRegionName eq '{region}' and retailPrice ne 0"
    items=azure_prices(flt,60)
    if not items: return None
    m=min([monthly_from(x) for x in items]); cache_put(key,m); return m

def price_azure(items: List[dict]) -> dict:
    total=0.0; out=[]; notes=[]
    for it in items:
        if it.get("cloud")!="azure": continue
        svc=it["service"]; sku=it.get("sku",""); region=it.get("region") or DEFAULT_REGION_AZURE
        qty=int(it.get("qty",1)); unit=None
        if svc=="app_service": unit=az_price_app_service(sku,region)
        elif svc=="azure_sql": unit=az_price_sql(sku,region)
        elif svc=="app_gateway":
            comps=az_price_appgw(region)
            cu=int(it.get("capacity_units") or 1)
            unit=(comps["base_monthly"]+cu*comps["capacity_unit_monthly"]) if comps else None
        elif svc=="lb":
            comps=az_price_lb(region)
            rules=int(it.get("rules") or 2); data=float(it.get("data_gb") or 100)
            unit=(rules*comps["rule_hour_monthly"] + data*comps["data_gb_monthly"]) if comps else None
        elif svc=="monitor": unit=az_price_log_analytics(region)
        else: unit=0.0
        if unit is None: notes.append(f"No price found for azure:{svc}:{sku} in {region} (set $0)"); unit=0.0
        monthly=round(unit*qty,2); total+=monthly
        out.append({"cloud":"azure","service":svc,"sku":sku,"qty":qty,"region":region,
                    "unit_monthly":round(unit,2),"monthly":monthly})
    return {"currency":"USD","total_estimate":round(total,2),"method":"azure-retail","notes":notes,"items":out}

# =========================
# AWS: Diagram MCP + AOAI Terraform + Dynamic Pricing (Offer files)
# =========================
# ---- Offer file cache ----
_AWS_OFFER_CACHE: Dict[str, Tuple[Dict[str,Any], float]] = {}  # key: f"{service}:{region}" -> (json, expiry)

def aws_offer_url(service: str, region: str) -> str:
    # Examples:
    #  https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json
    return f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"

def aws_offer_get(service: str, region: str, ttl_sec: int = 86400) -> Dict[str, Any]:
    key=f"{service}:{region}"
    v=_AWS_OFFER_CACHE.get(key)
    if v and v[1] > time.time(): return v[0]
    url=aws_offer_url(service, region)
    r=requests.get(url, timeout=60)
    if r.status_code>=300:
        raise HTTPException(status_code=502, detail=f"AWS pricing fetch failed: {service}/{region} ({r.status_code})")
    j=r.json()
    _AWS_OFFER_CACHE[key]=(j, time.time()+ttl_sec)
    return j

def _first_price_usd(terms_obj: Dict[str,Any]) -> Optional[float]:
    # Navigate terms.OnDemand[*].priceDimensions[*].pricePerUnit.USD
    for _,term in (terms_obj or {}).items():
        price_dims = term.get("priceDimensions") or {}
        for _,pd in price_dims.items():
            usd = pd.get("pricePerUnit",{}).get("USD")
            if usd is not None:
                try: return float(usd)
                except: pass
    return None

# ---- EC2 On-Demand hourly (Linux, shared tenancy) ----
def aws_price_ec2_hour(instance_type: str, region: str) -> Optional[float]:
    offer=aws_offer_get("AmazonEC2", region)
    products=offer.get("products",{}); terms=offer.get("terms",{}).get("OnDemand",{})
    for sku,prod in products.items():
        a=prod.get("attributes",{})
        if (prod.get("productFamily")=="Compute Instance" and
            a.get("instanceType")==instance_type and
            a.get("operatingSystem")=="Linux" and
            a.get("tenancy")=="Shared" and
            a.get("preInstalledSw") in (None,"NA") and
            a.get("capacitystatus") in (None,"Used","UnusedCapacityReservation")):
            term=terms.get(sku) or {}
            p=_first_price_usd(term)
            if p is not None: return p
    return None

# ---- RDS On-Demand hourly (instance class only) ----
def aws_price_rds_hour(instance_class: str, region: str, engine: str = "MySQL", deployment: str = None) -> Optional[float]:
    # deployment: "Single-AZ" or "Multi-AZ" (if None, accept any)
    offer=aws_offer_get("AmazonRDS", region)
    products=offer.get("products",{}); terms=offer.get("terms",{}).get("OnDemand",{})
    for sku,prod in products.items():
        a=prod.get("attributes",{})
        if (prod.get("productFamily")=="Database Instance" and
            a.get("instanceType")==instance_class and
            (a.get("databaseEngine")==engine or engine is None) and
            (a.get("deploymentOption")==deployment or deployment is None)):
            p=_first_price_usd(terms.get(sku) or {})
            if p is not None: return p
    return None

# ---- S3 per GB-Month (Standard) ----
def aws_price_s3_gb_month(region: str, storage_class: str = "Standard") -> Optional[float]:
    offer=aws_offer_get("AmazonS3", region)
    products=offer.get("products",{}); terms=offer.get("terms",{}).get("OnDemand",{})
    for sku,prod in products.items():
        a=prod.get("attributes",{})
        pf=prod.get("productFamily") or ""
        # Look for "Storage" family, Standard class, unit GB-Mo (some files use ByteHrs; prefer GB-Mo)
        if pf=="Storage" and (a.get("storageClass")==storage_class or a.get("storageClass")=="General Purpose"):
            p=_first_price_usd(terms.get(sku) or {})
            # We accept the first GB-Mo price; if per-Byte-Hr, skip for simplicity
            # Most regional "Standard" entries expose GB-Mo.
            if p is not None:
                unit_hint = ""
                for _,term in (terms.get(sku) or {}).items():
                    for _,pd in (term.get("priceDimensions") or {}).items():
                        unit_hint = (pd.get("unit") or unit_hint)
                if "GB" in unit_hint: return p
    return None

# ---- ALB: base hour, LCU hour, data processed GB ----
def aws_price_alb_components(region: str) -> Dict[str, Optional[float]]:
    offer=aws_offer_get("ElasticLoadBalancing", region)
    products=offer.get("products",{}); terms=offer.get("terms",{}).get("OnDemand",{})
    base=None; lcu=None; data_gb=None
    for sku,prod in products.items():
        a=prod.get("attributes",{}); pf=prod.get("productFamily") or ""
        meter = a.get("usagetype","")
        unitp=_first_price_usd(terms.get(sku) or {})
        if unitp is None: continue
        # Heuristics across regions:
        if "LoadBalancerUsage" in meter:          # per LB-hour
            base = unitp
        elif "LoadBalancerLCU" in meter:          # per LCU-hour
            lcu  = unitp
        elif "DataProcessing-Bytes" in meter:     # per GB processed
            # The unit is usually "GB"
            data_gb = unitp
    return {"base_per_hour":base, "lcu_per_hour":lcu, "data_per_gb":data_gb}

# ---- Normalize AWS items from a prompt / TF (simple heuristics) ----
def normalize_aws_items(ask: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION_AWS
    blob = f"{ask}\n{tf}".lower()
    items: List[dict] = []
    def add(service, sku, qty=1, extra=None):
        d={"cloud":"aws","service":service,"sku":sku,"qty":int(qty),"region":region}
        if extra: d.update(extra)
        items.append(d)
    # EC2
    m=re.search(r"\bec2\b.*?\b([ctmr]\d\.\w+)\b", blob); itype=m.group(1) if m else "t3.micro"
    if "ec2" in blob or "autoscaling" in blob or "asg" in blob: add("ec2", itype, qty=2 if "asg" in blob else 1)
    # ALB
    if "alb" in blob or "application load balancer" in blob: add("alb","LCU", qty=1, extra={"lcu":20.0,"data_gb":100.0})
    # RDS
    if "rds" in blob or "mysql" in blob or "postgres" in blob:
        m2=re.search(r"\bdb\.[a-z0-9.]+\b", blob); rclass=m2.group(0) if m2 else "db.t3.micro"
        add("rds", rclass, qty=1, extra={"engine":"MySQL","deployment":"Multi-AZ" if "multi-az" in blob else None})
    # S3
    if "s3" in blob: add("s3","standard", qty=1, extra={"size_gb":100.0})
    return items

# ---- Price AWS items dynamically from offer files ----
def price_aws(items: List[dict]) -> dict:
    total=0.0; out=[]; notes=[]
    for it in items:
        if it.get("cloud")!="aws": continue
        svc=it["service"]; sku=it["sku"]; region=it.get("region", DEFAULT_REGION_AWS)
        qty=int(it.get("qty",1))
        unit_monthly=0.0

        try:
            if svc=="ec2":
                h=aws_price_ec2_hour(sku, region)
                if h is None: notes.append(f"No price found for aws:ec2:{sku} in {region} (set $0)")
                unit_monthly = (h or 0.0) * HOURS_PER_MONTH
            elif svc=="rds":
                h=aws_price_rds_hour(sku, region, engine=it.get("engine"), deployment=it.get("deployment"))
                if h is None: notes.append(f"No price found for aws:rds:{sku} in {region} (set $0)")
                unit_monthly = (h or 0.0) * HOURS_PER_MONTH
            elif svc=="s3":
                per_gb = aws_price_s3_gb_month(region, storage_class="Standard")
                size_gb = float(it.get("size_gb") or 100.0)
                if per_gb is None: notes.append(f"No price found for aws:s3:Standard in {region} (set $0)")
                unit_monthly = (per_gb or 0.0) * size_gb
            elif svc=="alb":
                comps=aws_price_alb_components(region)
                base = (comps.get("base_per_hour") or 0.0) * HOURS_PER_MONTH
                lcu  = (comps.get("lcu_per_hour") or 0.0)  * HOURS_PER_MONTH * float(it.get("lcu") or 20.0)
                data = (comps.get("data_per_gb") or 0.0)   * float(it.get("data_gb") or 100.0)
                if base==0 and lcu==0 and data==0:
                    notes.append(f"No price found for aws:alb in {region} (set $0)")
                unit_monthly = base + lcu + data
            else:
                notes.append(f"AWS service not priced: {svc}")
        except HTTPException as e:
            notes.append(f"Pricing fetch error for aws:{svc}:{sku} in {region}: {e.detail}")
        except Exception as e:
            notes.append(f"Pricing error for aws:{svc}:{sku}: {e}")

        monthly=round(unit_monthly*qty,2); total+=monthly
        line={"cloud":"aws","service":svc,"sku":sku,"qty":qty,"region":region,
              "unit_monthly":round(unit_monthly,2),"monthly":monthly}
        if svc=="alb":
            line["lcu"]=it.get("lcu"); line["data_gb"]=it.get("data_gb")
        if svc=="s3": line["size_gb"]=it.get("size_gb")
        out.append(line)

    return {"currency":"USD","total_estimate":round(total,2),
            "method":"aws-offer-files","notes":notes,"items":out}

# =========================
# MCP bridges
# =========================
def mcp_tools_call(tool_name: str, arguments: dict) -> dict:
    url = AWS_DIAGRAM_MCP_HTTP.rstrip("/") + "/mcp"
    payload={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool_name,"arguments":arguments}}
    try:
        r=requests.post(url, json=payload, timeout=180)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MCP bridge unreachable: {e}")
    if r.status_code>=300:
        raise HTTPException(status_code=502, detail=f"MCP HTTP {r.status_code}: {r.text}")
    j=r.json()
    if "error" in j: raise HTTPException(status_code=502, detail=f"MCP error: {j['error']}")
    return j.get("result") or {}

# =========================
# Endpoints
# =========================
@app.post("/api/mcp/azure/diagram-tf")
def mcp_azure(payload: dict = Body(...), _=Depends(require_api_key)):
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    app_name = payload.get("app_name","secure Azure 3-tier web app")
    extra    = payload.get("prompt","")
    region   = payload.get("region") or DEFAULT_REGION_AZURE

    system = (
        "You are ArchGenie's Azure MCP. Emit ONLY JSON with keys: diagram (Mermaid) and terraform (HCL). "
        "diagram must start with 'graph TD' or 'graph LR'. No markdown."
    )
    user = f"Create Azure architecture for: {app_name}\nExtra: {extra}\nRegion: {region}\nJSON only."
    r=aoai_chat([{"role":"system","content":system},{"role":"user","content":user}])
    content=r["choices"][0]["message"]["content"]
    parsed=extract_json_or_fences(content)
    diagram=sanitize_mermaid(parsed.get("diagram",""))
    tf=strip_fences(parsed.get("terraform",""))
    if not diagram.lower().startswith("graph "): raise HTTPException(status_code=502, detail="Invalid Mermaid")
    if not tf: raise HTTPException(status_code=502, detail="Missing Terraform")

    items = normalize_azure_items(extra, diagram, tf, region)
    cost  = price_azure(items)
    return {"diagram":diagram, "terraform":tf, "cost":cost}

@app.post("/api/mcp/aws/diagram-tf-cost")
def mcp_aws(payload: dict = Body(...), _=Depends(require_api_key)):
    prompt = (payload.get("prompt") or "").strip() or \
        "Three-tier: ALB -> EC2 Auto Scaling Group -> RDS (Multi-AZ); VPC public/private subnets across 2 AZs; NAT."
    region = payload.get("region") or DEFAULT_REGION_AWS
    fmt    = (payload.get("format") or "svg").lower()

    # 1) Diagram from local AWS Diagram MCP
    result = mcp_tools_call("generate_diagram", {"prompt":prompt, "format":fmt, "style":{"theme":"light"}})
    image  = result.get("image")
    svg    = None
    if fmt=="svg":
        if isinstance(image,str) and image.lstrip().startswith("<svg"):
            svg=image
        else:
            try: svg=base64.b64decode(image).decode("utf-8")
            except: raise HTTPException(status_code=500, detail="Could not decode SVG")

    # 2) Terraform via AOAI (AWS HCL only)
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured (for AWS TF synthesis)")
    system_tf = (
        "Emit ONLY Terraform HCL for AWS resources for the described architecture. "
        "Include: VPC with 2 AZ public/private subnets, NAT, ALB, Auto Scaling Group (LT), SGs, RDS (engine inferred). "
        "No comments, no markdown."
    )
    user_tf = f"Region: {region}\nPrompt:\n{prompt}\nOutput: Terraform HCL only."
    tf_resp=aoai_chat([{"role":"system","content":system_tf},{"role":"user","content":user_tf}], temperature=0.1)
    tf_content=tf_resp["choices"][0]["message"]["content"]
    tf=strip_fences(tf_content or "")

    # 3) Dynamic AWS pricing (offer files)
    items = normalize_aws_items(ask=prompt, tf=tf, region=region)
    cost  = price_aws(items)

    return {"diagram_svg": svg, "terraform": tf, "cost": cost, "region": region, "prompt_used": prompt}

# ---- ZIP bundle (same as before)
@app.post("/api/bundle")
def bundle_zip(payload: dict = Body(...), _=Depends(require_api_key)):
    diagram = (payload.get("diagram") or "").strip()
    tf      = (payload.get("terraform") or "").strip()
    svg     = (payload.get("image_svg") or "").strip()
    png_b64 = (payload.get("image_png_b64") or "").strip()
    if not (diagram or svg or png_b64 or tf):
        raise HTTPException(status_code=400, detail="Nothing to bundle")

    bio=io.BytesIO()
    with zipfile.ZipFile(bio,"w",zipfile.ZIP_DEFLATED) as z:
        if diagram: z.writestr("diagram.mmd", diagram)
        if svg:     z.writestr("diagram.svg", svg)
        if png_b64: z.writestr("diagram.png", base64.b64decode(png_b64))
        if tf:      z.writestr("main.tf", tf)
        z.writestr("README.txt","ArchGenie bundle\n")
    bio.seek(0)
    return StreamingResponse(bio, media_type="application/zip",
        headers={"Content-Disposition":"attachment; filename=archgenie-bundle.zip"})
