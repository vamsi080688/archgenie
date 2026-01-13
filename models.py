from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class ChatSession(db.Model):
    __tablename__ = 'chat_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    session_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    status = db.Column(db.Enum('draft', 'finalized', 'code_generated'), default='draft')
    
    # Relationships
    messages = db.relationship('ChatMessage', backref='session', lazy=True, cascade='all, delete-orphan')
    diagrams = db.relationship('ArchitectureDiagram', backref='session', lazy=True, cascade='all, delete-orphan')
    
    def to_dict(self):
        return {
            'id': self.id,
            'session_name': self.session_name,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'status': self.status,
            'message_count': len(self.messages),
            'diagram_count': len(self.diagrams)
        }

class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'
    
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_sessions.id'), nullable=False)
    message_type = db.Column(db.Enum('user', 'assistant'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    message_order = db.Column(db.Integer, nullable=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'message_type': self.message_type,
            'content': self.content,
            'timestamp': self.timestamp.isoformat(),
            'message_order': self.message_order
        }

class ArchitectureDiagram(db.Model):
    __tablename__ = 'architecture_diagrams'
    
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_sessions.id'), nullable=False)
    diagram_name = db.Column(db.String(255), nullable=False)
    mermaid_code = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    version = db.Column(db.Integer, default=1)
    is_current = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    components = db.relationship('DiagramComponent', backref='diagram', lazy=True, cascade='all, delete-orphan')
    
    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'diagram_name': self.diagram_name,
            'mermaid_code': self.mermaid_code,
            'description': self.description,
            'version': self.version,
            'is_current': self.is_current,
            'created_at': self.created_at.isoformat(),
            'components': [comp.to_dict() for comp in self.components]
        }

class DiagramComponent(db.Model):
    __tablename__ = 'diagram_components'
    
    id = db.Column(db.Integer, primary_key=True)
    diagram_id = db.Column(db.Integer, db.ForeignKey('architecture_diagrams.id'), nullable=False)
    component_name = db.Column(db.String(255), nullable=False)
    component_type = db.Column(db.String(100), nullable=False)  # VM, RDS, LoadBalancer, etc.
    cloud_provider = db.Column(db.String(50))  # AWS, Azure, GCP
    properties = db.Column(db.Text)  # JSON string of component properties
    
    def to_dict(self):
        return {
            'id': self.id,
            'diagram_id': self.diagram_id,
            'component_name': self.component_name,
            'component_type': self.component_type,
            'cloud_provider': self.cloud_provider,
            'properties': json.loads(self.properties) if self.properties else {}
        }
    
    def set_properties(self, properties_dict):
        self.properties = json.dumps(properties_dict)
    
    def get_properties(self):
        return json.loads(self.properties) if self.properties else {}

class InfrastructureCode(db.Model):
    __tablename__ = 'infrastructure_code'
    
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_sessions.id'), nullable=False)
    diagram_id = db.Column(db.Integer, db.ForeignKey('architecture_diagrams.id'), nullable=False)
    code_type = db.Column(db.Enum('terraform', 'cloudformation', 'bicep', 'pulumi'), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    file_content = db.Column(db.Text, nullable=False)
    component_type = db.Column(db.String(100))  # Which component this code is for
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    session = db.relationship('ChatSession', backref='infrastructure_codes')
    diagram = db.relationship('ArchitectureDiagram', backref='infrastructure_codes')
    
    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'diagram_id': self.diagram_id,
            'code_type': self.code_type,
            'file_name': self.file_name,
            'file_content': self.file_content,
            'component_type': self.component_type,
            'created_at': self.created_at.isoformat()
        }