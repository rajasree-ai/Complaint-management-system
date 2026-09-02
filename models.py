from datetime import datetime
from flask_login import UserMixin
from database import db
import uuid
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='student')  # student, staff/mentor, hod, admin
    department = db.Column(db.String(100))
    year = db.Column(db.String(20))
    section = db.Column(db.String(10))
    roll_number = db.Column(db.String(20), nullable=True)
    parent_name = db.Column(db.String(100))
    parent_phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    mentor_name = db.Column(db.String(100))  # For students - who is their mentor
    phone = db.Column(db.String(20))
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


class HODDepartment(db.Model):
    """Join table allowing one HOD (User) to have access to multiple departments.
    Department.hod_id remains the single 'official' HOD shown on Manage Departments,
    but this table is the real source of truth for which departments a HOD can access."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('department.id', ondelete='CASCADE'), nullable=False)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('hod_department_links', passive_deletes=True, cascade='all, delete-orphan')
    )
    department_ref = db.relationship('Department', foreign_keys=[department_id])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'department_id', name='unique_hod_department'),
    )


class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.String(20), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default='pending')
    priority = db.Column(db.String(20), default='medium')
    action_taken = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # Make sure this exists
    # Free-text field to capture what action was taken for this complaint (e.g., interventions, fixes, notes)
    action_taken = db.Column(db.Text, nullable=True)

    # ===== Automation fields =====
    deadline = db.Column(db.DateTime, nullable=True)  # SLA deadline computed from priority at creation
    is_overdue = db.Column(db.Boolean, default=False)  # Set by the "Status Update" automation job
    escalation_level = db.Column(db.Integer, default=0)  # 0=none, 1=escalated to HOD, 2=escalated to Principal/Admin
    last_escalated_at = db.Column(db.DateTime, nullable=True)
    last_reminded_at = db.Column(db.DateTime, nullable=True)  # Last time a deadline reminder was sent
    is_duplicate_of = db.Column(db.Integer, db.ForeignKey('complaint.id'), nullable=True)  # Points to the original complaint
    auto_response_sent = db.Column(db.Boolean, default=False)  # Whether the real-time auto-responder has replied
    ai_category = db.Column(db.String(50), nullable=True)  # AI-classified category (Facility, Administration, etc.)

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
    - id: UUID primary key (intended to correspond to auth.users(id) in Supabase,
      but NOT declared as a DB-level ForeignKey here — SQLAlchemy doesn't manage
      Supabase's internal `auth` schema, so a FK to 'auth.users.id' is
      unresolvable and previously made db.create_all() fail for EVERY table,
      not just this one, since table creation is ordered by FK dependency.
      If you need this enforced at the database level, add it manually via a
      raw SQL migration in Supabase instead of through this model.)
    - updated_at: timestamp with timezone
    - username: text unique with CHECK(char_length(username) >= 3)
    - full_name, avatar_url, website
    """
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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