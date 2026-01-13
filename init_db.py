#!/usr/bin/env python3
"""
Database initialization script for Architecture Diagram Generator
"""

from flask import Flask
from models import db, ChatSession, ChatMessage, ArchitectureDiagram, DiagramComponent, InfrastructureCode
import pymysql

def create_app():
    """Create Flask application instance"""
    app = Flask(__name__)
    
    # Database configuration
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        'YOUR_DB_HOST_URI'
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = '1234'
    
    # Initialize database
    db.init_app(app)
    
    return app

def init_database():
    """Initialize the database with tables"""
    app = create_app()
    
    with app.app_context():
        try:
            # Create all tables
            db.create_all()
            print("✅ Database tables created successfully!")
            
            # Create some sample data for testing
            create_sample_data()
            
            print("✅ Database initialization completed!")
            
        except Exception as e:
            print(f"❌ Error initializing database: {str(e)}")
            raise

def create_sample_data():
    """Create sample data for testing"""
    try:
        # Check if sample data already exists
        if ChatSession.query.first():
            print("Sample data already exists, skipping...")
            return
            
        # Create a sample chat session
        sample_session = ChatSession(
            session_name="AWS Three-Tier Architecture",
            status="draft"
        )
        db.session.add(sample_session)
        db.session.commit()
        
        # Add sample messages
        messages = [
            ChatMessage(
                session_id=sample_session.id,
                message_type="user",
                content="I need to design a three-tier web application architecture on AWS with high availability",
                message_order=1
            ),
            ChatMessage(
                session_id=sample_session.id,
                message_type="assistant",
                content="I'll help you design a three-tier architecture. Let me create a diagram with web tier, application tier, and database tier with load balancers and auto-scaling.",
                message_order=2
            )
        ]
        
        for msg in messages:
            db.session.add(msg)
        
        db.session.commit()
        print("✅ Sample data created successfully!")
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error creating sample data: {str(e)}")

def drop_all_tables():
    """Drop all tables - use with caution!"""
    app = create_app()
    
    with app.app_context():
        try:
            db.drop_all()
            print("✅ All tables dropped successfully!")
        except Exception as e:
            print(f"❌ Error dropping tables: {str(e)}")

def reset_database():
    """Reset database - drop and recreate all tables"""
    print("🔄 Resetting database...")
    drop_all_tables()
    init_database()

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == "reset":
            reset_database()
        elif command == "drop":
            drop_all_tables()
        elif command == "init":
            init_database()
        else:
            print("Usage: python init_db.py [init|reset|drop]")
            print("  init  - Initialize database with tables and sample data")
            print("  reset - Drop and recreate all tables")
            print("  drop  - Drop all tables")
    else:
        # Default action
        init_database()