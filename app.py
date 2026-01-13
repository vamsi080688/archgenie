from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
from models import db, ChatSession, ChatMessage, ArchitectureDiagram, DiagramComponent, InfrastructureCode
from datetime import datetime
from openai import AzureOpenAI
import json
import re
import os
import io
import zipfile

app = Flask(__name__)

# Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'YOUR_DB_HOST_URI'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = '1234'

# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT = "https://vamsi-mexrh0su-eastus2.cognitiveservices.azure.com/"
AZURE_OPENAI_KEY = "NUUbaVBCxIomTJI7I9U1GG6OacCIo5JvVn5CPin04E37bjg1LYjvJQQJ99BHACHYHv6XJ3w3AAAAACOGxWiQ"
AZURE_OPENAI_VERSION = "2024-12-01-preview"
MODEL_DEPLOYMENT_NAME = "gpt-4o"

# Initialize Azure OpenAI client
try:
    client = AzureOpenAI(
        api_key=AZURE_OPENAI_KEY,
        api_version=AZURE_OPENAI_VERSION,
        azure_endpoint=AZURE_OPENAI_ENDPOINT
    )
    print("✅ Azure OpenAI client initialized")
except Exception as e:
    print(f"❌ Azure OpenAI init error: {e}")
    client = None

# Initialize DB
db.init_app(app)

@app.route("/")
def index():
    sessions = ChatSession.query.order_by(ChatSession.updated_at.desc()).all()
    return render_template("index.html", sessions=sessions)

@app.route("/new_session", methods=["POST"])
def new_session():
    session_name = request.form.get('session_name', f'Architecture Session {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    s = ChatSession(session_name=session_name)
    db.session.add(s)
    db.session.commit()
    return redirect(url_for('chat_session', session_id=s.id))

@app.route("/session/<int:session_id>")
def chat_session(session_id):
    session = ChatSession.query.get_or_404(session_id)
    messages = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.message_order).all()
    diagrams = ArchitectureDiagram.query.filter_by(session_id=session_id, is_current=True).order_by(ArchitectureDiagram.version.desc()).all()
    codes = InfrastructureCode.query.filter_by(session_id=session_id).order_by(InfrastructureCode.created_at.desc()).all()
    return render_template("chat.html", session=session, messages=messages, diagrams=diagrams, codes=codes)

# ============== API ==============

@app.route("/api/send_message", methods=["POST"])
def send_message():
    if not client:
        return jsonify({'success': False, 'error': 'Azure OpenAI client not initialized'}), 500

    data = request.get_json()
    session_id = data.get('session_id')
    user_message = data.get('message', '').strip()

    if not session_id or not user_message:
        return jsonify({'success': False, 'error': 'Missing session_id or message'}), 400

    session = ChatSession.query.get_or_404(session_id)
    msg_count = ChatMessage.query.filter_by(session_id=session_id).count()

    # Save user message
    user_msg = ChatMessage(
        session_id=session_id,
        message_type='user',
        content=user_message,
        message_order=msg_count + 1
    )
    db.session.add(user_msg)
    db.session.flush()

    # AI response
    try:
        ai_response, mermaid = generate_ai_response(session_id, user_message)

        ai_msg = ChatMessage(
            session_id=session_id,
            message_type='assistant',
            content=ai_response,
            message_order=msg_count + 2
        )
        db.session.add(ai_msg)

        diagram_id = None
        if mermaid:
            diagram_id = save_architecture_diagram(session_id, mermaid, ai_response)

        session.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'success': True,
            'ai_response': ai_response,
            'mermaid_diagram': mermaid,
            'diagram_id': diagram_id
        })

    except Exception as e:
        db.session.rollback()
        print("Error generating AI response:", e)
        return jsonify({'success': False, 'error': f'Failed to generate response: {e}'}), 500

@app.route("/api/finalize_diagram", methods=["POST"])
def finalize_diagram():
    data = request.get_json()
    session_id = data.get('session_id')

    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session_id'}), 400

    try:
        session = ChatSession.query.get_or_404(session_id)
        session.status = 'finalized'
        db.session.commit()
        return jsonify({'success': True, 'message': 'Architecture diagram finalized!'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Failed to finalize diagram: {e}'}), 500

@app.route("/api/generate_code", methods=["POST"])
def generate_infrastructure_code():
    if not client:
        return jsonify({'success': False, 'error': 'Azure OpenAI client not initialized'}), 500

    data = request.get_json()
    session_id = data.get('session_id')
    diagram_id = data.get('diagram_id')
    code_type = data.get('code_type', 'terraform')  # terraform | cloudformation | bicep | pulumi

    if not session_id or not diagram_id:
        return jsonify({'success': False, 'error': 'Missing session_id or diagram_id'}), 400

    try:
        diagram = ArchitectureDiagram.query.get_or_404(diagram_id)
        components = DiagramComponent.query.filter_by(diagram_id=diagram_id).all()

        generated_files = []
        for comp in components:
            content = generate_component_code(comp, code_type, diagram.mermaid_code)
            if content:
                fname = f"{sanitize_filename(comp.component_name)}.{get_file_extension(code_type)}"
                ic = InfrastructureCode(
                    session_id=session_id,
                    diagram_id=diagram_id,
                    code_type=code_type,
                    file_name=fname,
                    file_content=content,
                    component_type=comp.component_type
                )
                db.session.add(ic)
                db.session.flush()
                generated_files.append(ic.to_dict())

        session = ChatSession.query.get(session_id)
        session.status = 'code_generated'
        db.session.commit()

        return jsonify({
            'success': True,
            'generated_files': generated_files,
            'message': f'Generated {len(generated_files)} {code_type} files successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print("Error generating code:", e)
        return jsonify({'success': False, 'error': f'Failed to generate code: {e}'}), 500

@app.route("/api/sessions/<int:session_id>/codes")
def get_session_codes(session_id):
    try:
        codes = InfrastructureCode.query.filter_by(session_id=session_id).all()
        return jsonify([c.to_dict() for c in codes])
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to fetch codes: {e}'}), 500

@app.route("/api/download_code/<int:code_id>")
def download_code(code_id):
    code = InfrastructureCode.query.get_or_404(code_id)
    return jsonify({
        'success': True,
        'filename': code.file_name,
        'content': code.file_content,
        'type': code.code_type
    })

@app.route("/api/download_zip/<int:session_id>")
def download_zip(session_id):
    """Convenience: download all generated code for a session as a zip."""
    codes = InfrastructureCode.query.filter_by(session_id=session_id).all()
    if not codes:
        return jsonify({'success': False, 'error': 'No code generated yet'}), 400

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zf:
        for c in codes:
            zf.writestr(c.file_name, c.file_content)
    mem.seek(0)
    return send_file(mem, mimetype='application/zip', as_attachment=True, download_name=f'session_{session_id}_iac.zip')

@app.route("/api/health")
def health():
    try:
        db.session.execute(db.text('SELECT 1'))
        client_status = 'connected' if client else 'not_initialized'
        return jsonify({'status': 'healthy', 'database': 'connected', 'azure_openai': client_status, 'timestamp': datetime.utcnow().isoformat()})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e), 'timestamp': datetime.utcnow().isoformat()}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Resource not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return jsonify({'success': False, 'error': 'Internal server error'}), 500

# ============== AI Helpers ==============

def generate_ai_response(session_id, user_message):
    if not client:
        raise Exception("Azure OpenAI client not initialized")

    # Last 10 messages as context
    msgs = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.message_order).all()
    conversation = [
        {
            "role": "system",
            "content": (
                "You are an expert cloud architect. Your job is to:\n"
                "1) Help users design cloud architecture diagrams based on requirements\n"
                "2) Generate proper Mermaid diagram syntax for architecture visualization\n"
                "3) Identify specific components (VMs, databases, load balancers, etc.) with their properties\n"
                "4) Suggest improvements and best practices\n\n"
                "When generating diagrams:\n"
                "- Use Mermaid syntax (graph TD/LR or flowchart) ONLY within ```mermaid fences\n"
                "- Include specific component types like EC2, RDS, ALB, S3, Lambda, VPC; or Azure/GCP equivalents\n"
                "- Add clear connections and labels\n\n"
                "Always include a single ```mermaid code block for the current best diagram."
            )
        }
    ]
    for m in msgs[-10:]:
        conversation.append({"role": "user" if m.message_type == "user" else "assistant", "content": m.content})

    conversation.append({"role": "user", "content": user_message})

    resp = client.chat.completions.create(
        model=MODEL_DEPLOYMENT_NAME,
        messages=conversation,
        max_tokens=1800,
        temperature=0.5,
    )

    ai_text = resp.choices[0].message.content.strip()
    mermaid = extract_mermaid(ai_text)
    return ai_text, mermaid

def extract_mermaid(text):
    m = re.search(r'```mermaid\s+(.+?)```', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

def save_architecture_diagram(session_id, mermaid_code, description):
    # set existing to not current
    ArchitectureDiagram.query.filter_by(session_id=session_id).update({'is_current': False})
    version = get_next_version(session_id)
    diagram = ArchitectureDiagram(
        session_id=session_id,
        diagram_name=f"Architecture v{version}",
        mermaid_code=mermaid_code,
        description=description[:1000],
        version=version,
        is_current=True
    )
    db.session.add(diagram)
    db.session.commit()

    parse_and_save_components(diagram.id, mermaid_code)
    return diagram.id

def get_next_version(session_id):
    max_version = db.session.query(db.func.max(ArchitectureDiagram.version)).filter_by(session_id=session_id).scalar()
    return (max_version or 0) + 1

def parse_and_save_components(diagram_id, mermaid_code):
    try:
        lines = [ln.strip() for ln in mermaid_code.splitlines() if ln.strip()]
        nodes = set()

        # Simple detection: look for [Label] or ((Label)) or {{Label}} in lines
        node_patterns = [
            r'\[(.*?)\]', r'\(\((.*?)\)\)', r'\{\{(.*?)\}\}', r'\((.*?)\)'
        ]
        for ln in lines:
            for pat in node_patterns:
                for match in re.finditer(pat, ln):
                    label = match.group(1).strip()
                    if label:
                        nodes.add(label)

        for name in nodes:
            ctype, provider = infer_component_type(name)
            comp = DiagramComponent(
                diagram_id=diagram_id,
                component_name=name,
                component_type=ctype,
                cloud_provider=provider
            )
            comp.set_properties({'auto_detected': True, 'source': 'mermaid'})
            db.session.add(comp)

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        print("Component parsing error:", e)

def infer_component_type(name):
    n = name.lower()

    if any(x in n for x in ['ec2', 'vm', 'instance', 'compute engine']):
        ctype = 'VM'
    elif any(x in n for x in ['rds', 'database', 'db', 'sql', 'cosmos', 'cloud sql']):
        ctype = 'Database'
    elif any(x in n for x in ['load balancer', 'lb', 'alb', 'nlb', 'elb', 'application gateway']):
        ctype = 'LoadBalancer'
    elif any(x in n for x in ['s3', 'bucket', 'storage', 'blob', 'gcs']):
        ctype = 'Storage'
    elif any(x in n for x in ['api gateway', 'apigateway', 'gateway']):
        ctype = 'APIGateway'
    elif any(x in n for x in ['lambda', 'function', 'cloud function', 'azure function', 'serverless']):
        ctype = 'Function'
    elif any(x in n for x in ['vpc', 'network', 'subnet', 'vnet']):
        ctype = 'Network'
    elif any(x in n for x in ['kubernetes', 'eks', 'aks', 'gke', 'cluster']):
        ctype = 'Kubernetes'
    else:
        ctype = 'Other'

    if any(x in n for x in ['ec2', 'rds', 's3', 'alb', 'nlb', 'elb', 'lambda', 'vpc', 'eks', 'aws']):
        provider = 'AWS'
    elif any(x in n for x in ['azure', 'vm', 'vnet', 'blob', 'cosmos', 'app service', 'application gateway', 'aks']):
        provider = 'Azure'
    elif any(x in n for x in ['gcp', 'gcs', 'compute engine', 'cloud sql', 'gke']):
        provider = 'GCP'
    else:
        provider = 'Generic'

    return ctype, provider

def generate_component_code(component, code_type, mermaid_diagram):
    if not client:
        return None

    prompt = (
        f"Generate {code_type} code for the following component based on the architecture context.\n\n"
        f"Component Name: {component.component_name}\n"
        f"Component Type: {component.component_type}\n"
        f"Cloud Provider: {component.cloud_provider}\n"
        f"Properties: {json.dumps(component.get_properties())}\n\n"
        f"Architecture (Mermaid):\n{mermaid_diagram}\n\n"
        "Requirements:\n"
        f"- Generate production-ready {code_type} code\n"
        "- Include variables/parameters and reasonable defaults\n"
        "- Follow best practices and security guidelines\n"
        "- Add tags/labels where possible\n"
        "- Include networking config if applicable\n"
        "- Return ONLY the code (no markdown, no commentary)\n"
    )

    resp = client.chat.completions.create(
        model=MODEL_DEPLOYMENT_NAME,
        messages=[
            {"role": "system", "content": f"You are an expert in {code_type} Infrastructure as Code. Output only valid {code_type} code."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1400,
        temperature=0.3
    )

    code = resp.choices[0].message.content.strip()
    # Clean possible fences
    code = re.sub(r'^```[\w-]*\s*', '', code)
    code = re.sub(r'\s*```$', '', code)
    return code

def get_file_extension(code_type):
    return {
        'terraform': 'tf',
        'cloudformation': 'yml',
        'bicep': 'bicep',
        'pulumi': 'py'
    }.get(code_type, 'txt')

def sanitize_filename(name):
    return re.sub(r'[^a-zA-Z0-9_\-\.]+', '_', name).strip('_')

if __name__ == "__main__":
    with app.app_context():
        try:
            db.create_all()
            print("✅ Database tables created")
        except Exception as e:
            print("❌ DB creation error:", e)

    print("🚀 Starting Architecture Generator on http://localhost:5012")
    app.run(debug=True, host="0.0.0.0", port=5012)
