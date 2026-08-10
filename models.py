from datetime import datetime
from flask_login import UserMixin
from database import db
import uuid
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='student')  # student, staff/mentor, hod, admin
    department = db.Column(db.String(100))
    year = db.Column(db.String(20))
    section = db.Column(db.String(10))
    parent_name = db.Column(db.String(100))
    parent_phone = db.Column(db.String(15))
    address = db.Column(db.Text)
    mentor_name = db.Column(db.String(100))  # For students - who is their mentor
    phone = db.Column(db.String(15))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    complaints = db.relationship('Complaint', backref='author', lazy=True, foreign_keys='Complaint.user_id')
    assigned_complaints = db.relationship('Complaint', backref='assigned_staff', lazy=True, foreign_keys='Complaint.assigned_to')

class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    hod_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    hod = db.relationship('User', foreign_keys=[hod_id], backref='managed_department')



class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.String(20), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default='pending')
    priority = db.Column(db.String(20), default='medium')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # Make sure this exists
    
    comments = db.relationship('Comment', backref='complaint', lazy=True, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='complaint', lazy=True, cascade='all, delete-orphan')

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    complaint_id = db.Column(db.Integer, db.ForeignKey('complaint.id'), nullable=False)
    
    user = db.relationship('User', backref='comments')


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(50), default='status_update')
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    complaint_id = db.Column(db.Integer, db.ForeignKey('complaint.id'), nullable=True)
    
    user = db.relationship('User', backref='notifications')


class Profiles(db.Model):
    """Optional profiles table that mirrors Supabase auth.users via UUID primary key.

    Fields:
    - id: UUID primary key that references auth.users(id)
    - updated_at: timestamp with timezone
    - username: text unique with CHECK(char_length(username) >= 3)
    - full_name, avatar_url, website
    """
    id = db.Column(UUID(as_uuid=True), db.ForeignKey('auth.users.id'), primary_key=True, default=uuid.uuid4)
    updated_at = db.Column(db.DateTime(timezone=True), default=func.now(), onupdate=func.now())
    username = db.Column(db.Text, unique=True, nullable=True)
    full_name = db.Column(db.Text, nullable=True)
    avatar_url = db.Column(db.Text, nullable=True)
    website = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.CheckConstraint("char_length(username) >= 3", name='profiles_username_length_check'),
    )


class PasswordResetOTP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    otp = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    is_used = db.Column(db.Boolean, default=False)


class StudentStaffAssignment(db.Model):
    """Model to track student-staff assignments within departments"""
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text, nullable=True)
    
    # Relationships
    student = db.relationship(
        'User',
        foreign_keys=[student_id],
        backref=db.backref('staff_assignments', passive_deletes=True, cascade='all, delete-orphan')
    )
    staff = db.relationship(
        'User',
        foreign_keys=[staff_id],
        backref=db.backref('assigned_students', passive_deletes=True, cascade='all, delete-orphan')
    )
    
    # Ensure a student can only be assigned once per department, regardless of staff member.
    __table_args__ = (
        db.UniqueConstraint('student_id', 'staff_id', 'department', name='unique_student_staff_dept'),
        db.UniqueConstraint('student_id', 'department', name='unique_student_department')
    )