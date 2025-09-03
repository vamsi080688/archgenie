import os, json, requests
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# ----------------- Auth -----------------
CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ----------------- Azure OpenAI Config -----------------
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

def azure_chat(messages, temperature: float = 0.2):
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_DEPLOYMENT:
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY}
    body = {"messages": messages, "temperature": temperature}
    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if resp.status_code >= 300:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

# ----------------- FastAPI app -----------------
app = FastAPI(title="ArchGenie Backend", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "ArchGenie backend alive"}

# ----------------- Azure MCP (via Azure OpenAI) -----------------
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _auth=Depends(require_api_key)):
    """
    Generate architecture diagram (Mermaid) and Terraform for Azure using Azure OpenAI.
    """
    app_name = payload.get("app_name", "3-tier web app")
    messages = [
        {"role": "system", "content": "You are ArchGenie's Azure MCP. Always return JSON with 'diagram' and 'terraform' keys."},
        {"role": "user", "content": f"Create an Azure architecture diagram in Mermaid and Terraform IaC for {app_name}."}
    ]
    result = azure_chat(messages)
    return result

# ----------------- AWS Mock -----------------
@app.get("/mcp/aws/diagram-tf")
def aws_mock(_auth=Depends(require_api_key)):
    return {
        "diagram": """
        graph TD
          A[ALB] --> B[EC2: web-1]
          B --> C[RDS: archgenie-db]
          B --> D[S3: assets]
        """,
        "terraform": """
        resource "aws_instance" "web" {
          ami           = "ami-123456"
          instance_type = "t3.micro"
        }

        resource "aws_s3_bucket" "assets" {
          bucket = "archgenie-assets"
        }
        """
    }

# ----------------- GCP Mock -----------------
@app.get("/mcp/gcp/diagram-tf")
def gcp_mock(_auth=Depends(require_api_key)):
    return {
        "diagram": """
        graph TD
          A[Load Balancer] --> B[Compute Engine: web-1]
          B --> C[Cloud SQL: archgenie-db]
          B --> D[Cloud Storage: assets]
        """,
        "terraform": """
        resource "google_compute_instance" "web" {
          name         = "web-1"
          machine_type = "e2-micro"
        }

        resource "google_storage_bucket" "assets" {
          name     = "archgenie-assets"
          location = "US"
        }
        """
    }
