import os

from app import app, db
from models import User, Department
from werkzeug.security import generate_password_hash

with app.app_context():
    # Clear existing data (optional - remove if you want to keep data)
    print("Clearing existing data...")
    db.session.query(User).delete()
    db.session.query(Department).delete()
    
    # Create departments
    print("Creating departments...")
    depts = [
        'Computer Science and Engineering',
        'Information Technology',
        'Electronics and Communication Engineering',
        'Electrical and Electronics Engineering',
        'Mechanical Engineering',
        'Civil Engineering',
        'Artificial Intelligence and Data Science',
        'Artificial Intelligence and Machine Learning',
        'Computer Science and Design',
        'Biomedical Engineering',
        'Robotics and Automation',
        'Chemical Engineering',
        'Agricultural Engineering',
        'Biotechnology',
        'Cyber Security',
        'MBA',
        'MCA'
    ]
    for dept in depts:
        db.session.add(Department(name=dept))
    
    # Create admin user
    print("Creating admin user...")
    admin = User(
        username='vanitha',
        email='vanitha.sty3375@gmail.com',
        password=generate_password_hash('vanitha@75'),
        role='admin',
        department='Administration'
    )
    db.session.add(admin)
    
    db.session.commit()
    print(f"✅ Added {len(depts)} departments")
    print("✅ Admin user created: vanitha.sty3375@gmail.com / vanitha@75")
