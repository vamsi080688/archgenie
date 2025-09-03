import os
import json
import re
import requests
from typing import List, Dict, Any
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ----------------- Load env -----------------
load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT   = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY    = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

# ----------------- Auth -----------------
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ----------------- FastAPI app -----------------
app = FastAPI(title="ArchGenie Backend", version="3.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie backend alive"}

# ----------------- Helpers -----------------
def _assert_aoai():
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_DEPLOYMENT:
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")

def aoai_chat(messages: List[Dict[str, Any]], temperature: float = 0.2) -> Dict[str, Any]:
    _assert_aoai()
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

def strip_fences(text: str) -> str:
    """Remove ```lang ... ``` or generic ``` ``` fences if present."""
    if not text:
        return ""
    s = text.strip()
    # language-specific
    for lang in ("mermaid", "hcl", "terraform", "json"):
        m = re.match(rf"^```{lang}\s*\n([\s\S]*?)```$", s, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # generic
    m = re.match(r"^```\s*\n?([\s\S]*?)```$", s)
    if m:
        return m.group(1).strip()
    return s

def extract_json_or_fences(content: str) -> Dict[str, str]:
    """
    Accepts AOAI free-form content. Returns {'diagram': str, 'terraform': str}.
    Handles:
      - pure JSON with diagram/terraform keys
      - fenced blocks inside text (```mermaid / ```terraform or ```hcl)
      - last-resort: returns content as diagram so UI shows something
    """
    if not content:
        return {"diagram": "", "terraform": ""}

    # JSON first
    try:
        obj = json.loads(content)
        diagram = strip_fences(obj.get("diagram", ""))
        tf = strip_fences(obj.get("terraform", ""))
        if diagram or tf:
            return {"diagram": diagram, "terraform": tf}
    except Exception:
        pass

    # Fenced blocks
    diagram = ""
    tf = ""
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m:
        diagram = m.group(1).strip()
    m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m:
        tf = m.group(2).strip()

    if diagram or tf:
        return {"diagram": diagram, "terraform": tf}

    # Fallback: treat whole content as diagram (UI will show it in a code box if not valid)
    return {"diagram": content, "terraform": ""}

# ----------------- Azure MCP (via Azure OpenAI) -----------------
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _auth=Depends(require_api_key)):
    """
    Returns: { "diagram": "<mermaid>", "terraform": "<hcl>" }
    """
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""

    system = (
        "You are ArchGenie's Azure MCP.\n"
        "Return ONLY a valid JSON object with **exactly** these keys:\n"
        '{\n'
        '  "diagram": "graph TD\\nA[Frontend]-->B[Backend]-->C[Database]",\n'
        '  "terraform": "resource \\"azurerm_resource_group\\" \\"rg\\" { name = \\"demo\\" location = \\"eastus\\" }"\n'
        '}\n'
        "- Do NOT wrap code in triple backticks.\n"
        "- `diagram` MUST be raw Mermaid starting with `graph`.\n"
        "- `terraform` MUST be valid HCL for Azure resources.\n"
        "- Keep it concise and runnable (use sensible defaults/SKUs).\n"
    )
    user = (
        f"Create an Azure architecture for: {app_name}.\n"
        f"Additional requirements (optional): {extra}\n"
        "Respond with JSON only (no markdown)."
    )

    result = aoai_chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])

    # pull message content
    content = ""
    try:
        content = result["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(result)

    parsed = extract_json_or_fences(content)
    # double-sanitize
    parsed["diagram"] = strip_fences(parsed.get("diagram", ""))
    parsed["terraform"] = strip_fences(parsed.get("terraform", ""))
    return parsed

# ----------------- AWS / GCP Mocks -----------------
@app.get("/mcp/aws/diagram-tf")
def aws_mock(_auth=Depends(require_api_key)):
    return {
        "diagram": """graph TD
  subgraph AWS
    A[ALB] --> B[EC2: web-1]
    B --> C[RDS: archgenie-db]
    B --> D[S3: assets]
  end
""",
        "terraform": """resource "aws_instance" "web" {
  ami           = "ami-123456"
  instance_type = "t3.micro"
}

resource "aws_s3_bucket" "assets" {
  bucket = "archgenie-assets"
}
""",
    }

@app.get("/mcp/gcp/diagram-tf")
def gcp_mock(_auth=Depends(require_api_key)):
    return {
        "diagram": """graph TD
  subgraph GCP
    A[Load Balancer] --> B[Compute Engine: web-1]
    B --> C[Cloud SQL: archgenie-db]
    B --> D[Cloud Storage: assets]
  end
""",
  "terraform": """resource "google_compute_instance" "web" {
  name         = "web-1"
  machine_type = "e2-micro"
  zone         = "us-central1-a"
}

resource "google_storage_bucket" "assets" {
  name     = "archgenie-assets"
  location = "US"
}
""",
    }