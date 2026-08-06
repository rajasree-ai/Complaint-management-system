from flask import Flask, render_template, redirect, url_for, flash, request, abort, jsonify
from flask_login import login_user, current_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
import logging
import sys
import os
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment variables from .env for local development
load_dotenv()

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db, login_manager
from models import User, Complaint, Comment, Notification, Department, PasswordResetOTP, StudentStaffAssignment
from forms import (
    RegistrationForm, LoginForm, ComplaintForm, CommentForm, UpdateComplaintForm,
    ForgotPasswordForm, ResetPasswordForm, DepartmentForm, 
    RemoveStudentAssignmentForm, StaffStudentAssignmentForm, UpdateProfileForm
)
from utils import (
    generate_complaint_id, send_email_notification, create_notification, 
    calculate_complaint_stats, send_complaint_registration_email,
    send_comment_notification, send_status_update_email, generate_otp, 
    send_otp_email, get_hod_department, utc_to_local
)
from sqlalchemy import inspect, text

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')

database_url = os.environ.get('DATABASE_URL') or 'sqlite:///grievance_hub.db'
database_url = database_url.strip().strip('"').strip("'")
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
"""
parsed_db = urlparse(database_url)
if parsed_db.scheme in ('postgresql', 'postgres'):
    print(f"DATABASE_URL host: {parsed_db.hostname}")
    print(f"DATABASE_URL path: {parsed_db.path}")
"""

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.sendgrid.net')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'apikey')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD') or os.environ.get('SENDGRID_API_KEY')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'no-reply@example.com')

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
# Initialize extensions
db.init_app(app)
login_manager.init_app(app)

# Compatibility shim: some Flask versions may remove `before_first_request`.
# Provide a decorator that runs the wrapped function only once (on the first request).
if not hasattr(app, 'before_first_request'):
    def _before_first_request(func):
        def _wrapper(*args, **kwargs):
            if not getattr(app, '_got_first_request', False):
                app._got_first_request = True
                return func(*args, **kwargs)
            return None
        app.before_request(_wrapper)
        return func
    app.before_first_request = _before_first_request

# Add Jinja2 filter for timezone conversion
@app.template_filter('localtime')
def localtime_filter(utc_dt):
    return utc_to_local(utc_dt)


# Add Jinja2 filter for date formatting
@app.template_filter('strftime')
def strftime_filter(dt, fmt='%d-%m-%Y'):
    """Format datetime using strftime"""
    if dt is None:
        return ''
    return dt.strftime(fmt)


# ========== HELPER FUNCTIONS ==========

def is_super_admin(user):
    """Check if user is super admin (vanitha only)"""
    return user.role == 'admin' and user.email == 'vanitha.sty3375@gmail.com'


def is_department_admin(user):
    """Check if user is a department admin (HOD)"""
    return user.role == 'hod'


def get_user_department(user):
    """Get the department a user belongs to"""
    return user.department


def get_department_complaints(department_name):
    """Get all complaints from a specific department"""
    return Complaint.query.join(User, Complaint.user_id == User.id).filter(User.department == department_name).all()


def get_department_users(department_name):
    """Get all users in a specific department"""
    return User.query.filter_by(department=department_name).all()


def get_department_students(department_name):
    """Get all students in a department"""
    return User.query.filter_by(department=department_name, role='student').all()


def get_department_staff(department_name):
    """Get all staff in a department"""
    return User.query.filter_by(department=department_name, role='staff').all()


def get_user_accessible_complaints(user):
    """Get complaints based on user's role"""
    if is_super_admin(user):
        return Complaint.query.all()
    elif is_department_admin(user):
        return Complaint.query.join(User, Complaint.user_id == User.id).filter(User.department == user.department).all()
    elif user.role in ['staff', 'mentor']:
        return Complaint.query.filter((Complaint.assigned_to == user.id) | (Complaint.mentor_id == user.id)).all()
    else:
        return Complaint.query.filter_by(user_id=user.id).all()


def can_manage_user(user, target_user):
    """Check if user can manage another user"""
    if is_super_admin(user):
        return True
    if is_department_admin(user):
        return target_user.department == user.department
    if user.role in ['staff', 'mentor'] and target_user.role == 'student':
        return target_user.department == user.department
    return False


def can_view_complaint(user, complaint):
    """Check if user can view a complaint"""
    if is_super_admin(user):
        return True
    if is_department_admin(user):
        author = User.query.get(complaint.user_id)
        return author.department == user.department
    elif user.role in ['staff', 'mentor']:
        return complaint.assigned_to == user.id or complaint.mentor_id == user.id
    else:
        return complaint.user_id == user.id


def can_update_complaint(user, complaint):
    """Check if user can update a complaint"""
    if is_super_admin(user):
        return True
    if is_department_admin(user):
        author = User.query.get(complaint.user_id)
        return author.department == user.department
    elif user.role in ['staff', 'mentor']:
        return complaint.assigned_to == user.id or complaint.mentor_id == user.id
    return False


def can_delete_complaint(user, complaint):
    """Check if user can delete a complaint"""
    if is_super_admin(user):
        return True
    if is_department_admin(user):
        author = User.query.get(complaint.user_id)
        return author.department == user.department and complaint.status in ['resolved', 'rejected']
    elif user.role in ['staff', 'mentor']:
        return (complaint.assigned_to == user.id or complaint.mentor_id == user.id) and complaint.status in ['resolved', 'rejected']
    else:
        return complaint.user_id == user.id and complaint.status in ['resolved', 'rejected']


def get_complaint_or_404(complaint_ref):
    """Resolve a complaint by database id first, then by display complaint_id."""
    complaint = Complaint.query.get(complaint_ref)

    if complaint is None:
        complaint = Complaint.query.filter_by(complaint_id=str(complaint_ref)).first()

    if complaint is None:
        abort(404)

    return complaint


def get_hod_department_by_name(department_name):
    """Get department by name"""
    return Department.query.filter_by(name=department_name).first()


def can_delete_user(user):
    """Check if a user can be deleted (no active complaints or assignments)"""
    if user.complaints:
        return False, f"Cannot delete user. They have {len(user.complaints)} complaint(s)."
    
    assigned_complaints = Complaint.query.filter(
        (Complaint.assigned_to == user.id) | (Complaint.mentor_id == user.id)
    ).count()
    
    if assigned_complaints > 0:
        return False, f"Cannot delete user. They are assigned to {assigned_complaints} complaint(s)."
    
    return True, "User can be deleted"


def renumber_student_ids():
    """Renumber student IDs to be sequential after deletion"""
    try:
        # Get all students ordered by current ID
        students = User.query.filter_by(role='student').order_by(User.id).all()
        
        if not students:
            return
        
        # Find the minimum student ID (students might not start from 1 due to other users)
        min_student_id = min(student.id for student in students)
        
        # Create mapping of old ID to new sequential ID starting from min_student_id
        student_id_map = {}
        new_id = min_student_id
        
        for student in students:
            old_id = student.id
            if old_id != new_id:
                student_id_map[old_id] = new_id
            new_id += 1
        
        if not student_id_map:
            return  # No renumbering needed
        
        # Disable foreign key checks for SQLite
        db.session.execute(text('PRAGMA foreign_keys=OFF'))
        
        # First, update student IDs to temporary negative values to avoid conflicts
        temp_id = -1
        temp_map = {}
        for old_id in student_id_map.keys():
            temp_map[old_id] = temp_id
            db.session.execute(
                text('UPDATE "user" SET id = :temp_id WHERE id = :old_id AND role = "student"'),
                {'temp_id': temp_id, 'old_id': old_id}
            )
            temp_id -= 1
        
        # Now update to final IDs
        for old_id, new_id in student_id_map.items():
            temp_id = temp_map[old_id]
            db.session.execute(
                text('UPDATE "user" SET id = :new_id WHERE id = :temp_id AND role = "student"'),
                {'new_id': new_id, 'temp_id': temp_id}
            )
        
        # Update all references to the old student IDs
        for old_id, new_id in student_id_map.items():
            # Update complaints user_id
            db.session.execute(
                text('UPDATE complaint SET user_id = :new_id WHERE user_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update complaints assigned_to
            db.session.execute(
                text('UPDATE complaint SET assigned_to = :new_id WHERE assigned_to = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update complaints mentor_id
            db.session.execute(
                text('UPDATE complaint SET mentor_id = :new_id WHERE mentor_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update comments user_id
            db.session.execute(
                text('UPDATE comment SET user_id = :new_id WHERE user_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update notifications user_id
            db.session.execute(
                text('UPDATE notification SET user_id = :new_id WHERE user_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update student-staff assignments
            db.session.execute(
                text('UPDATE student_staff_assignment SET student_id = :new_id WHERE student_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
            # Update department hod_id (if a student was somehow a hod)
            db.session.execute(
                text('UPDATE department SET hod_id = :new_id WHERE hod_id = :old_id'),
                {'new_id': new_id, 'old_id': old_id}
            )
        
        # Re-enable foreign key checks
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        db.session.commit()
        
        print(f"Renumbered {len(student_id_map)} student IDs")
        
    except Exception as e:
        db.session.rollback()
        print(f"Error renumbering student IDs: {str(e)}")
        raise


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def initialize_database():
    """Initialize database schema and default data safely."""
    try:
        with app.app_context():
            db.create_all()

            # Ensure legacy SQLite databases get the missing mentor_id column
            inspector = inspect(db.engine)
            if 'complaint' in inspector.get_table_names():
                complaint_columns = [column['name'] for column in inspector.get_columns('complaint')]
                if 'mentor_id' not in complaint_columns:
                    with db.engine.begin() as connection:
                        connection.execute(text('ALTER TABLE complaint ADD COLUMN mentor_id INTEGER'))
                    print('Added missing mentor_id column to complaint table')

            super_admin = User.query.filter_by(email='vanitha.sty3375@gmail.com').first()
            if not super_admin:
                super_admin = User(
                    username='vanitha',
                    email='vanitha.sty3375@gmail.com',
                    password=generate_password_hash('vanitha@75'),
                    role='admin',
                    department='Administration'
                )
                db.session.add(super_admin)
                db.session.commit()
                print("Super Admin account created!")

            if Department.query.count() == 0:
                departments = [
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
                for dept in departments:
                    db.session.add(Department(name=dept))
                db.session.commit()
                print(f"Added {len(departments)} departments")

    except Exception as e:
        app.logger.error('Database initialization failed: %s', e, exc_info=True)
        print('WARNING: Database initialization failed. Application startup continues, but database access may be unavailable. Will retry on subsequent requests.')
        return False

    print('Database initialized successfully.')
    return True


db_initialized = False

@app.before_request
def startup_db():
    global db_initialized
    if not db_initialized:
        db_initialized = initialize_database()


# ========== MAIN ROUTES ==========

@app.route('/')
def index():
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data)
        user = User(
            username=form.username.data,
            email=form.email.data,
            password=hashed_password,
            role='student',
            department=form.department.data,
            year=form.year.data,
            section=form.section.data,
            phone=form.phone.data,
            parent_name=form.parent_name.data,
            parent_phone=form.parent_phone.data,
            address=form.address.data
        )
        db.session.add(user)
        db.session.commit()
        
        subject = 'Welcome to Grievance Hub - Student Account'
        body = f'''
Dear {user.username},

Welcome to the Grievance Hub!

Your account has been successfully created.

Login Credentials:
------------------
Email: {user.email}
Password: (the password you set during registration)
Department: {user.department}

You can now register complaints and track their status.

Thank you
'''
        send_email_notification(user.email, subject, body)
        
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html', form=form)


@app.route('/register-staff', methods=['GET', 'POST'])
def register_staff():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        department = request.form.get('department')
        
        if not username or not email or not password or not confirm_password or not department:
            flash('Please fill out all required fields!', 'danger')
            return redirect(url_for('register_staff'))

        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('register_staff'))
        
        if User.query.filter_by(username=username).first():
            flash('Username already taken!', 'danger')
            return redirect(url_for('register_staff'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered!', 'danger')
            return redirect(url_for('register_staff'))
        
        dept = Department.query.filter_by(name=department).first()
        if not dept:
            flash('Invalid department!', 'danger')
            return redirect(url_for('register_staff'))
        
        hashed_password = generate_password_hash(password)
        staff = User(
            username=username,
            email=email,
            password=hashed_password,
            role='staff',
            department=department
        )
        db.session.add(staff)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('A user with that username or email already exists.', 'danger')
            return redirect(url_for('register_staff'))
        
        subject = 'Welcome to Grievance Hub - Staff Account'
        body = f'''
Dear {username},

Welcome to the Grievance Hub!

Your staff account has been successfully created.

Login Credentials:
------------------
Email: {email}
Password: {password}
Department: {department}

You can now:
- View complaints assigned to you
- Update complaint status
- Add comments to complaints
- Delete resolved/rejected complaints

Please login and change your password for security.

Thank you
'''
        send_email_notification(email, subject, body)
        
        flash('Staff account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
    
    departments = Department.query.all()
    return render_template('register_staff.html', departments=departments)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            next_page = request.args.get('next')
            flash('Login successful!', 'success')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')
    return render_template('login.html', form=form)


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    if is_super_admin(current_user):
        return redirect(url_for('super_admin_dashboard'))
    elif is_department_admin(current_user):
        return redirect(url_for('hod_dashboard'))
    elif current_user.role in ['staff', 'mentor']:
        return redirect(url_for('staff_dashboard'))
    else:
        complaints = Complaint.query.filter_by(user_id=current_user.id).all()
        stats = calculate_complaint_stats(complaints)
        notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        return render_template('dashboard.html', complaints=complaints, stats=stats, notifications=notifications)


# ========== SUPER ADMIN DASHBOARD ==========

@app.route('/super-admin/dashboard')
@login_required
def super_admin_dashboard():
    if not is_super_admin(current_user):
        abort(403)
    
    complaints = Complaint.query.all()
    users = User.query.all()
    stats = calculate_complaint_stats(complaints)
    recent_complaints = Complaint.query.order_by(Complaint.created_at.desc()).limit(10).all()
    departments = Department.query.all()
    
    return render_template('super_admin_dashboard.html', 
                         complaints=complaints, 
                         users=users,
                         stats=stats,
                         recent_complaints=recent_complaints,
                         departments=departments)


# ========== HOD DASHBOARD ==========

@app.route('/hod/dashboard')
@login_required
def hod_dashboard():
    if not is_department_admin(current_user):
        abort(403)
    
    department_name = current_user.department
    complaints = Complaint.query.join(User, Complaint.user_id == User.id).filter(User.department == department_name).all()
    users = User.query.filter_by(department=department_name).all()
    students = User.query.filter_by(department=department_name, role='student').all()
    staff = User.query.filter_by(department=department_name, role='staff').all()
    stats = calculate_complaint_stats(complaints)
    
    total_students = len(students)
    total_staff = len(staff)
    pending_complaints = stats['pending']
    resolved_complaints = stats['resolved']
    total_complaints = stats['total']
    notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    
    return render_template('hod_dashboard.html', 
                         complaints=complaints,
                         users=users,
                         stats=stats,
                         students=students,
                         staff=staff,
                         department=department_name,
                         total_students=total_students,
                         total_staff=total_staff,
                         pending_complaints=pending_complaints,
                         resolved_complaints=resolved_complaints,
                         total_complaints=total_complaints,
                         notifications=notifications)


# ========== STAFF DASHBOARD ==========

@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        abort(403)
    
    complaints = Complaint.query.filter(
        (Complaint.assigned_to == current_user.id) | (Complaint.mentor_id == current_user.id)
    ).order_by(Complaint.created_at.desc()).all()
    
    stats = calculate_complaint_stats(complaints)
    notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    
    return render_template('staff_dashboard.html', 
                         complaints=complaints,
                         stats=stats,
                         notifications=notifications,
                         staff_name=current_user.username,
                         department=current_user.department)


# ========== MENTOR (STAFF) ROUTES ==========

@app.route('/my-mentors')
@login_required
def my_mentors():
    if current_user.role != 'student':
        abort(403)
    
    mentors = User.query.filter_by(department=current_user.department, role='staff').all()
    return render_template('my_mentors.html', mentors=mentors, department=current_user.department)


@app.route('/mentor/students')
@login_required
def mentor_students():
    """Mentor can view and manage students in their department"""
    if current_user.role != 'staff':
        abort(403)
    
    # Get all students in the department
    students = User.query.filter_by(department=current_user.department, role='student').all()
    
    # Get assigned student IDs for this staff
    assigned_assignments = StudentStaffAssignment.query.filter_by(
        staff_id=current_user.id,
        department=current_user.department
    ).all()
    assigned_student_ids = [assignment.student_id for assignment in assigned_assignments]
    
    # Get assigned students
    assigned_students = [assignment.student for assignment in assigned_assignments]
    
    # Group all students by year and section
    grouped_students = {}
    for student in students:
        year_section = f"{student.year} {student.section}" if student.year and student.section else "Unassigned"
        
        if year_section not in grouped_students:
            grouped_students[year_section] = []
        
        # Get complaint stats
        complaints = Complaint.query.filter_by(user_id=student.id).all()
        stats = calculate_complaint_stats(complaints)
        
        student_data = {
            'user': student,
            'total_complaints': len(complaints),
            'pending': stats['pending'],
            'resolved': stats['resolved'],
            'is_assigned': student.id in assigned_student_ids
        }
        
        grouped_students[year_section].append(student_data)
    
    # Group assigned students by year and section
    grouped_assigned_students = {}
    for assignment in assigned_assignments:
        student = assignment.student
        year_section = f"{student.year} {student.section}" if student.year and student.section else "Unassigned"
        
        if year_section not in grouped_assigned_students:
            grouped_assigned_students[year_section] = []
        
        # Get complaint stats
        complaints = Complaint.query.filter_by(user_id=student.id).all()
        stats = calculate_complaint_stats(complaints)
        
        student_data = {
            'user': student,
            'assignment': assignment,
            'total_complaints': len(complaints),
            'pending': stats['pending'],
            'resolved': stats['resolved']
        }
        
        grouped_assigned_students[year_section].append(student_data)
    
    # Sort the groups
    grouped_students = dict(sorted(grouped_students.items()))
    grouped_assigned_students = dict(sorted(grouped_assigned_students.items()))
    
    # Create the assignment form
    form = StaffStudentAssignmentForm(current_user.department)
    
    return render_template('mentor_students.html', 
                         grouped_students=grouped_students,
                         grouped_assigned_students=grouped_assigned_students,
                         assigned_student_ids=assigned_student_ids,
                         form=form,
                         department=current_user.department,
                         mentor_name=current_user.username)


@app.route('/mentor/student/<int:student_id>/complaints')
@login_required
def mentor_student_complaints(student_id):
    """Mentor can view and manage a specific student's complaints"""
    if current_user.role != 'staff':
        abort(403)
    
    student = User.query.get_or_404(student_id)
    
    if student.department != current_user.department:
        abort(403)
    
    complaints = Complaint.query.filter_by(user_id=student.id).order_by(Complaint.created_at.desc()).all()
    stats = calculate_complaint_stats(complaints)
    
    return render_template('mentor_student_complaints.html', 
                         student=student,
                         complaints=complaints,
                         stats=stats,
                         mentor=current_user)


@app.route('/send-message/<int:student_id>', methods=['POST'])
@login_required
def send_message_to_student(student_id):
    """Mentor can send message to student"""
    if current_user.role != 'staff':
        abort(403)
    
    student = User.query.get_or_404(student_id)
    
    if student.department != current_user.department:
        abort(403)
    
    subject = request.form.get('subject')
    message = request.form.get('message')
    
    email_body = f'''
Dear {student.username},

{message}

---
This message was sent by your mentor: {current_user.username}
Department: {current_user.department}

Thank you
'''
    
    send_email_notification(student.email, subject, email_body)
    
    create_notification(
        student.id,
        None,
        f'New message from your mentor: {subject}',
        'message'
    )
    
    flash(f'Message sent to {student.username} successfully!', 'success')
    return redirect(url_for('mentor_students'))


@app.route('/mentor/student/<int:student_id>/profile')
@login_required
def mentor_student_profile(student_id):
    """Mentor can view student profile"""
    if current_user.role not in ['staff', 'mentor', 'hod', 'admin']:
        abort(403)
    
    student = User.query.get_or_404(student_id)
    
    if current_user.role != 'admin' and student.department != current_user.department:
        abort(403)
    
    complaints = Complaint.query.filter_by(user_id=student.id).all()
    stats = calculate_complaint_stats(complaints)
    
    return render_template('mentor_student_profile.html', 
                         student=student,
                         stats=stats,
                         mentor=current_user)


# ========== MENTOR DELETE STUDENT ROUTE ==========

@app.route('/mentor/student/<int:student_id>/delete', methods=['POST'])
@login_required
def mentor_delete_student(student_id):
    """Mentor can delete a student from their department"""
    if current_user.role != 'staff':
        abort(403)
    
    student = User.query.get_or_404(student_id)
    
    # Check if student is in mentor's department
    if student.department != current_user.department:
        abort(403)
    
    # Check if user is actually a student
    if student.role != 'student':
        flash('Can only delete student accounts!', 'danger')
        return redirect(url_for('mentor_students'))
    
    # Check if student has any complaints
    if student.complaints:
        flash(f'Cannot delete student "{student.username}". They have {len(student.complaints)} complaint(s). Please resolve or reassign complaints first.', 'danger')
        return redirect(url_for('mentor_students'))
    
    try:
        # Store username for flash message
        username = student.username
        
        # Delete all student-staff assignments for this student
        StudentStaffAssignment.query.filter_by(student_id=student.id).delete()
        
        # Delete all notifications related to this student
        Notification.query.filter_by(user_id=student.id).delete()
        
        # Delete all comments by this student
        Comment.query.filter_by(user_id=student.id).delete()
        
        # Delete all complaints by this student (already checked but just in case)
        Complaint.query.filter_by(user_id=student.id).delete()
        
        # Delete the student
        db.session.delete(student)
        db.session.commit()
        
        # Renumber student IDs to maintain sequential order
        renumber_student_ids()
        
        flash(f'Student "{username}" has been deleted successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting student: {str(e)}', 'danger')
    
    return redirect(url_for('mentor_students'))


# ========== COMPLAINT ROUTES ==========

@app.route('/complaint/new', methods=['GET', 'POST'])
@login_required
def new_complaint():
    if current_user.role not in ['student']:
        flash('Only students can create complaints.', 'warning')
        return redirect(url_for('dashboard'))
    
    form = ComplaintForm()
    
    department_staff = User.query.filter_by(department=current_user.department, role='staff').all()
    form.mentor_id.choices = [(0, '-- Select a Mentor (Optional) --')] + [(s.id, f"{s.username} ({s.email})") for s in department_staff]
    
    if form.validate_on_submit():
        mentor_id = form.mentor_id.data if form.mentor_id.data != 0 else None
        
        complaint = Complaint(
            complaint_id=generate_complaint_id(),
            title=form.title.data,
            description=form.description.data,
            category=form.category.data,
            priority=form.priority.data,
            user_id=current_user.id,
            mentor_id=mentor_id,
            assigned_to=mentor_id
        )
        db.session.add(complaint)
        db.session.commit()
        
        send_complaint_registration_email(complaint)
        
        if mentor_id:
            mentor = User.query.get(mentor_id)
            if mentor:
                create_notification(
                    mentor.id,
                    complaint.id,
                    f'New complaint assigned to you by {current_user.username}',
                    'new_complaint'
                )
                mentor_subject = f'New Complaint Assigned: {complaint.complaint_id}'
                mentor_body = f'''
Dear {mentor.username},

A new complaint has been assigned to you by {current_user.username}.

Complaint ID: {complaint.complaint_id}
Title: {complaint.title}
Description: {complaint.description}
Category: {complaint.category}
Priority: {complaint.priority}

Please review and take appropriate action.

Thank you
'''
                send_email_notification(mentor.email, mentor_subject, mentor_body)
        
        hod_dept = get_hod_department_by_name(current_user.department)
        if hod_dept and hod_dept.hod_id:
            create_notification(
                hod_dept.hod_id,
                complaint.id,
                f'New complaint from {current_user.username} in {current_user.department} department',
                'new_complaint'
            )
        
        flash('Your complaint has been submitted!', 'success')
        return redirect(url_for('view_complaints'))
    
    return render_template('create_complaint.html', form=form)


@app.route('/complaints')
@login_required
def view_complaints():
    search_query = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    status_filter = request.args.get('status', '')
    assigned_to_filter = request.args.get('assigned_to', '')

    # ROLE BASED ACCESS
    if current_user.role == 'super-admin':
        query = Complaint.query

    elif current_user.role == 'admin':
        query = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == current_user.department
        )

    elif current_user.role in ['staff', 'mentor']:
        # staff & mentor can see department complaints
        query = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == current_user.department
        )

    else:  # student
        query = Complaint.query.filter_by(user_id=current_user.id)

    # FILTERS (keep your filters)
    if assigned_to_filter and assigned_to_filter.isdigit():
        query = query.filter(Complaint.assigned_to == int(assigned_to_filter))

    if search_query:
        query = query.filter(
            Complaint.complaint_id.ilike(f'%{search_query}%') |
            Complaint.title.ilike(f'%{search_query}%')
        )

    if category_filter:
        query = query.filter_by(category=category_filter)

    if status_filter:
        query = query.filter_by(status=status_filter)

    complaints = query.order_by(Complaint.created_at.desc()).all()

    categories = Complaint.query.with_entities(Complaint.category).distinct().all()
    statuses = ['pending', 'in_progress', 'resolved', 'rejected']

    return render_template(
        'view_complaints.html',
        complaints=complaints,
        categories=[c[0] for c in categories],
        statuses=statuses,
        search_query=search_query,
        category_filter=category_filter,
        status_filter=status_filter,
        assigned_to_filter=assigned_to_filter
    )

@app.route('/complaint/<int:complaint_id>', methods=['GET', 'POST'])
@login_required

def complaint_details(complaint_id):
    complaint = get_complaint_or_404(complaint_id)
    
    if not can_view_complaint(current_user, complaint):
        abort(403)
    
    comment_form = CommentForm()
    update_form = UpdateComplaintForm()
    
    mentor = None
    if complaint.mentor_id:
        mentor = User.query.get(complaint.mentor_id)
    
    staff_users = []
    if is_super_admin(current_user):
        staff_users = User.query.filter_by(role='staff').all()
        update_form.assigned_to.choices = [(0, 'Unassigned')] + [(u.id, u.username) for u in staff_users]
    elif is_department_admin(current_user):
        dept_staff = User.query.filter_by(department=current_user.department, role='staff').all()
        staff_users = dept_staff
        update_form.assigned_to.choices = [(0, 'Unassigned')] + [(u.id, u.username) for u in dept_staff]
    elif current_user.role in ['staff', 'mentor']:
        update_form.assigned_to.choices = [(current_user.id, current_user.username + ' (You)')]
        update_form.assigned_to.data = current_user.id
    
    if comment_form.validate_on_submit() and 'submit_comment' in request.form:
        comment = Comment(
            content=comment_form.content.data,
            user_id=current_user.id,
            complaint_id=complaint.id
        )
        db.session.add(comment)
        db.session.commit()
        
        # Send email notification to complaint author (student) if commenter is not the author
        if complaint.user_id != current_user.id:
            send_comment_notification(complaint, comment, complaint.author)
        
        # Send email notification to assigned staff if they exist and are not the commenter
        if complaint.assigned_to and complaint.assigned_to != current_user.id:
            assigned_staff = User.query.get(complaint.assigned_to)
            if assigned_staff:
                send_comment_notification(complaint, comment, assigned_staff)
        
        # Send email notification to mentor if they exist and are not the commenter
        if complaint.mentor_id and complaint.mentor_id != current_user.id:
            mentor = User.query.get(complaint.mentor_id)
            if mentor:
                send_comment_notification(complaint, comment, mentor)
        
        # Send in-app notification to complaint author if commenter is not the author
        if complaint.user_id != current_user.id:
            create_notification(
                complaint.user_id,
                complaint.id,
                f'New comment on your complaint #{complaint.complaint_id}',
                'comment'
            )
        
        # Send in-app notification to assigned staff/mentor if they exist and are not the commenter
        if complaint.assigned_to and complaint.assigned_to != current_user.id:
            create_notification(
                complaint.assigned_to,
                complaint.id,
                f'New comment on complaint #{complaint.complaint_id} by {complaint.author.username}',
                'comment'
            )
        
        # Send in-app notification to mentor if they exist and are not the commenter
        if complaint.mentor_id and complaint.mentor_id != current_user.id:
            create_notification(
                complaint.mentor_id,
                complaint.id,
                f'New comment on complaint #{complaint.complaint_id} by {complaint.author.username}',
                'comment'
            )
        
        flash('Comment added!', 'success')
        return redirect(url_for('complaint_details', complaint_id=complaint.id))
    
    if update_form.validate_on_submit() and 'update_complaint' in request.form:
        can_update = False
        if is_super_admin(current_user):
            can_update = True
        elif is_department_admin(current_user):
            author = User.query.get(complaint.user_id)
            if author.department == current_user.department:
                can_update = True
        elif current_user.role in ['staff', 'mentor'] and (complaint.assigned_to == current_user.id or complaint.mentor_id == current_user.id):
            can_update = True
        
        if not can_update:
            abort(403)
        
        old_status = complaint.status
        complaint.status = update_form.status.data
        if current_user.role != 'staff':
            complaint.assigned_to = update_form.assigned_to.data if update_form.assigned_to.data != 0 else None
        complaint.updated_at = datetime.utcnow()
        db.session.commit()
        
        if old_status != complaint.status:
            create_notification(
                complaint.user_id,
                complaint.id,
                f'Your complaint status has been updated from {old_status} to {complaint.status}',
                'status_update'
            )
            send_status_update_email(complaint, old_status)
        
        flash('Complaint updated successfully!', 'success')
        return redirect(url_for('complaint_details', complaint_id=complaint.id))
    
    return render_template('complaint_details.html', 
                         complaint=complaint, 
                         comment_form=comment_form,
                         update_form=update_form,
                         mentor=mentor,
                         staff_users=staff_users)


@app.route('/complaint/<int:complaint_id>/resolve')
@login_required
def resolve_complaint(complaint_id):
    complaint = get_complaint_or_404(complaint_id)
    
    if not can_update_complaint(current_user, complaint):
        abort(403)
    
    old_status = complaint.status
    complaint.status = 'resolved'
    complaint.updated_at = datetime.utcnow()
    db.session.commit()
    
    create_notification(
        complaint.user_id,
        complaint.id,
        f'Your complaint has been marked as resolved!',
        'status_update'
    )
    send_status_update_email(complaint, old_status)
    
    flash(f'Complaint marked as resolved!', 'success')
    return redirect(url_for('complaint_details', complaint_id=complaint.id))


@app.route('/complaint/<int:complaint_id>/delete', methods=['POST'])
@login_required
def delete_complaint(complaint_id):
    complaint = get_complaint_or_404(complaint_id)
    
    can_delete = False
    
    if is_super_admin(current_user):
        can_delete = True
    elif is_department_admin(current_user):
        author = User.query.get(complaint.user_id)
        if author.department == current_user.department:
            can_delete = True
    elif complaint.user_id == current_user.id:
        can_delete = True
    elif current_user.role in ['staff', 'mentor'] and (complaint.assigned_to == current_user.id or complaint.mentor_id == current_user.id):
        can_delete = True
    
    if complaint.status not in ['resolved', 'rejected']:
        flash('Only resolved or rejected complaints can be deleted!', 'warning')
        return redirect(url_for('complaint_details', complaint_id=complaint.id))
    
    if not can_delete:
        flash('You do not have permission to delete this complaint.', 'danger')
        return redirect(url_for('complaint_details', complaint_id=complaint.id))
    
    try:
        complaint_title = complaint.title
        complaint_id_display = complaint.complaint_id
        
        Comment.query.filter_by(complaint_id=complaint.id).delete()
        Notification.query.filter_by(complaint_id=complaint.id).delete()
        db.session.delete(complaint)
        db.session.commit()
        
        flash(f'Complaint "{complaint_title}" (ID: {complaint_id_display}) has been deleted successfully!', 'success')
        
        if is_super_admin(current_user):
            return redirect(url_for('super_admin_dashboard'))
        elif is_department_admin(current_user):
            return redirect(url_for('hod_dashboard'))
        elif current_user.role in ['staff', 'mentor']:
            return redirect(url_for('staff_dashboard'))
        else:
            return redirect(url_for('view_complaints'))
            
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting complaint: {str(e)}', 'danger')
        return redirect(url_for('complaint_details', complaint_id=complaint.id))


@app.route('/api/complaint/<int:complaint_id>/status', methods=['POST'])
@login_required
def api_update_status(complaint_id):
    if current_user.role != 'staff':
        return jsonify({'error': 'Unauthorized'}), 403
    
    complaint = get_complaint_or_404(complaint_id)
    
    if complaint.assigned_to != current_user.id and complaint.mentor_id != current_user.id:
        return jsonify({'error': 'Not assigned to you'}), 403
    
    data = request.get_json()
    new_status = data.get('status')
    
    if new_status not in ['in_progress', 'resolved']:
        return jsonify({'error': 'Invalid status'}), 400
    
    old_status = complaint.status
    complaint.status = new_status
    complaint.updated_at = datetime.utcnow()
    db.session.commit()
    
    create_notification(
        complaint.user_id,
        complaint.id,
        f'Your complaint status has been updated from {old_status} to {new_status} by {current_user.username}',
        'status_update'
    )
    
    return jsonify({'success': True})


# ========== USER MANAGEMENT ROUTES ==========

@app.route('/department/users')
@login_required
def department_users():
    if not is_department_admin(current_user):
        abort(403)
    
    users = User.query.filter_by(department=current_user.department).all()
    students = get_department_students(current_user.department)
    staff = get_department_staff(current_user.department)
    
    return render_template('department_users.html', 
                         users=users,
                         students=students,
                         staff=staff,
                         department=current_user.department)


@app.route('/department/user/<int:user_id>/change-role/<string:role>')
@login_required
def department_change_user_role(user_id, role):
    if not is_department_admin(current_user):
        abort(403)
    
    user = User.query.get_or_404(user_id)
    
    if user.department != current_user.department:
        abort(403)
    
    if user.id == current_user.id:
        flash('You cannot change your own role!', 'danger')
        return redirect(url_for('department_users'))
    
    if role not in ['student', 'staff']:
        flash('Invalid role specified!', 'danger')
        return redirect(url_for('department_users'))
    
    old_role = user.role
    user.role = role
    db.session.commit()
    
    flash(f'User role changed from {old_role} to {role} for {user.username}', 'success')
    create_notification(user.id, None, f'Your account role has been changed from {old_role} to {role}', 'role_change')
    
    return redirect(url_for('department_users'))


@app.route('/department/user/<int:user_id>/delete', methods=['POST'])
@login_required
def department_delete_user(user_id):
    if not is_department_admin(current_user):
        abort(403)
    
    user = User.query.get_or_404(user_id)
    
    if user.department != current_user.department:
        abort(403)
    
    if user.id == current_user.id:
        flash('You cannot delete your own account!', 'danger')
        return redirect(url_for('department_users'))
    
    if user.role == 'hod':
        flash('You cannot delete another HOD!', 'danger')
        return redirect(url_for('department_users'))
    
    try:
        # Delete all student-staff assignments for this user
        StudentStaffAssignment.query.filter_by(student_id=user.id).delete()
        StudentStaffAssignment.query.filter_by(staff_id=user.id).delete()
        
        # Delete all notifications related to this user
        Notification.query.filter_by(user_id=user.id).delete()
        Comment.query.filter_by(user_id=user.id).delete()
        Complaint.query.filter_by(assigned_to=user.id).update({'assigned_to': None})
        Complaint.query.filter_by(mentor_id=user.id).update({'mentor_id': None})
        Complaint.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        
        # Renumber student IDs if a student was deleted
        if user.role == 'student':
            renumber_student_ids()
        
        flash(f'User "{user.username}" has been deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting user: {str(e)}', 'danger')
    
    return redirect(url_for('department_users'))


# ========== STAFF MANAGEMENT ROUTES (HOD) ==========

@app.route('/department/manage-staff')
@login_required
def manage_staff():
    if not is_department_admin(current_user):
        abort(403)
    
    staff_members = User.query.filter_by(department=current_user.department, role='staff').all()
    
    staff_stats = []
    for staff in staff_members:
        assigned_complaints = Complaint.query.filter(
            (Complaint.assigned_to == staff.id) | (Complaint.mentor_id == staff.id)
        ).count()
        resolved_complaints = Complaint.query.filter(
            ((Complaint.assigned_to == staff.id) | (Complaint.mentor_id == staff.id)) &
            (Complaint.status == 'resolved')
        ).count()
        
        staff_stats.append({
            'user': staff,
            'assigned': assigned_complaints,
            'resolved': resolved_complaints,
            'performance': round((resolved_complaints / assigned_complaints * 100) if assigned_complaints > 0 else 0, 2)
        })
    
    return render_template('manage_staff.html', staff_stats=staff_stats, department=current_user.department)


@app.route('/department/staff/<int:staff_id>/delete', methods=['POST'])
@login_required
def delete_staff(staff_id):
    if not is_department_admin(current_user):
        abort(403)
    
    staff = User.query.get_or_404(staff_id)
    
    if staff.department != current_user.department:
        abort(403)
    
    if staff.role != 'staff':
        flash('Can only delete staff members', 'danger')
        return redirect(url_for('manage_staff'))
    
    try:
        Notification.query.filter_by(user_id=staff.id).delete()
        Comment.query.filter_by(user_id=staff.id).delete()
        Complaint.query.filter_by(assigned_to=staff.id).update({'assigned_to': None})
        Complaint.query.filter_by(mentor_id=staff.id).update({'mentor_id': None})
        Complaint.query.filter_by(user_id=staff.id).delete()
        db.session.delete(staff)
        db.session.commit()
        
        flash(f'Staff member "{staff.username}" deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting staff: {str(e)}', 'danger')
    
    return redirect(url_for('manage_staff'))


@app.route('/department/add-staff', methods=['GET', 'POST'])
@login_required
def add_department_staff():
    if not is_department_admin(current_user):
        abort(403)
    
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('User with this email already exists!', 'danger')
            return redirect(url_for('add_department_staff'))
        
        hashed_password = generate_password_hash(password)
        staff = User(
            username=username,
            email=email,
            password=hashed_password,
            role='staff',
            department=current_user.department
        )
        db.session.add(staff)
        db.session.commit()
        
        subject = 'Welcome to Grievance Hub - Staff Account'
        body = f'''
Dear {username},

Welcome to the Grievance Hub!

Your staff account has been successfully created.

Login Credentials:
------------------
Email: {email}
Password: {password}
Department: {current_user.department}

You can now:
- View complaints assigned to you
- Update complaint status
- Add comments to complaints
- Delete resolved/rejected complaints
- Mentor students in your department

Please login and change your password for security.

Thank you
'''
        send_email_notification(email, subject, body)
        
        flash(f'Staff/Mentor "{username}" added to {current_user.department} department!', 'success')
        return redirect(url_for('department_users'))
    
    return render_template('add_department_staff.html', department=current_user.department)


@app.route('/staff/my-assigned-students')
@login_required
def my_assigned_students():
    """View students assigned to the current staff member"""
    if current_user.role != 'staff':
        abort(403)
    
    assignments = StudentStaffAssignment.query.filter_by(
        staff_id=current_user.id,
        department=current_user.department
    ).all()
    
    students_data = []
    for assignment in assignments:
        # Get complaints related to this student
        complaints = Complaint.query.filter_by(user_id=assignment.student_id).all()
        students_data.append({
            'student': assignment.student,
            'assignment': assignment,
            'complaints_count': len(complaints),
            'pending_complaints': len([c for c in complaints if c.status == 'pending'])
        })
    
    return render_template('my_assigned_students.html', students_data=students_data)


# ========== STAFF STUDENT ASSIGNMENT ROUTES ==========

@app.route('/staff/assign-students')
@login_required
def staff_assign_students():
    """Staff can assign students to themselves"""
    if current_user.role != 'staff':
        abort(403)
    
    form = StaffStudentAssignmentForm(current_user.department)
    
    # Get current assignments for this staff member
    assignments = StudentStaffAssignment.query.filter_by(
        staff_id=current_user.id,
        department=current_user.department
    ).all()
    
    assigned_student_ids = [assignment.student_id for assignment in assignments]
    
    return render_template('staff_assign_students.html', form=form, assigned_student_ids=assigned_student_ids)


@app.route('/staff/assign-students/submit', methods=['POST'])
@login_required
def staff_submit_assignment():
    """Staff submits student assignments to themselves"""
    if current_user.role != 'staff':
        abort(403)
    
    form = StaffStudentAssignmentForm(current_user.department)
    
    if form.validate_on_submit():
        student_ids = form.students.data
        notes = form.notes.data
        
        try:
            # Add assignments
            for student_id in student_ids:
                student = User.query.filter_by(id=student_id, department=current_user.department, role='student').first()
                if not student:
                    continue
                
                # Check if assignment already exists
                existing = StudentStaffAssignment.query.filter_by(
                    student_id=student_id, 
                    staff_id=current_user.id, 
                    department=current_user.department
                ).first()
                
                if not existing:
                    assignment = StudentStaffAssignment(
                        student_id=student_id,
                        staff_id=current_user.id,
                        department=current_user.department,
                        notes=notes
                    )
                    db.session.add(assignment)
            
            db.session.commit()
            flash(f'Successfully assigned {len(student_ids)} student(s) to yourself', 'success')
        
        except IntegrityError as e:
            db.session.rollback()
            flash('One or more students were already assigned to you', 'warning')
        except Exception as e:
            db.session.rollback()
            flash(f'Error assigning students: {str(e)}', 'danger')
    
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    
    return redirect(url_for('staff_assign_students'))


@app.route('/submit-student-assignment', methods=['POST'])
@login_required
def submit_student_assignment():
    """HOD submits student assignments to staff members"""
    if not is_department_admin(current_user):
        abort(403)
    
    form = StudentStaffAssignmentForm(current_user.department)
    
    if form.validate_on_submit():
        staff_id = form.staff_member.data
        student_ids = form.students.data
        notes = form.notes.data
        
        staff = User.query.filter_by(id=staff_id, department=current_user.department, role='staff').first()
        if not staff:
            flash('Selected staff member not found or not in your department', 'danger')
            return redirect(url_for('assign_students'))
        
        try:
            # Add assignments
            for student_id in student_ids:
                student = User.query.filter_by(id=student_id, department=current_user.department, role='student').first()
                if not student:
                    continue
                
                # Check if assignment already exists
                existing = StudentStaffAssignment.query.filter_by(
                    student_id=student_id, 
                    staff_id=staff_id, 
                    department=current_user.department
                ).first()
                
                if not existing:
                    assignment = StudentStaffAssignment(
                        student_id=student_id,
                        staff_id=staff_id,
                        department=current_user.department,
                        notes=notes
                    )
                    db.session.add(assignment)
            
            db.session.commit()
            flash(f'Successfully assigned {len(student_ids)} student(s) to {staff.username}', 'success')
        
        except IntegrityError as e:
            db.session.rollback()
            flash('One or more students were already assigned to this staff member', 'warning')
        except Exception as e:
            db.session.rollback()
            flash(f'Error assigning students: {str(e)}', 'danger')
    
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    
    return redirect(url_for('assign_students'))


@app.route('/remove-student-assignment/<int:assignment_id>', methods=['POST'])
@login_required
def remove_student_assignment(assignment_id):
    """Remove a student assignment"""
    assignment = StudentStaffAssignment.query.get_or_404(assignment_id)
    
    # Check permissions
    if is_super_admin(current_user):
        pass  # Super admin can remove any assignment
    elif is_department_admin(current_user):
        if assignment.department != current_user.department:
            abort(403)
    elif current_user.role in ['staff', 'mentor']:
        if assignment.staff_id != current_user.id:
            abort(403)
    else:
        abort(403)
    
    try:
        student_name = assignment.student.username
        staff_name = assignment.staff.username
        db.session.delete(assignment)
        db.session.commit()
        flash(f'Successfully removed assignment of {student_name} from {staff_name}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error removing assignment: {str(e)}', 'danger')
    
    # Redirect based on user role
    if is_department_admin(current_user):
        return redirect(url_for('assign_students'))
    elif current_user.role in ['staff', 'mentor']:
        return redirect(url_for('my_assigned_students'))
    else:
        return redirect(url_for('dashboard'))


# ========== SUPER ADMIN USER MANAGEMENT ==========

@app.route('/admin/users')
@login_required
def super_admin_users():
    if not is_super_admin(current_user):
        abort(403)
    users = User.query.all()
    return render_template('super_admin_users.html', users=users)


@app.route('/admin/user/<int:user_id>/change-role/<string:role>')
@login_required
def super_admin_change_user_role(user_id, role):
    if not is_super_admin(current_user):
        abort(403)
    
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own role!', 'danger')
        return redirect(url_for('super_admin_users'))
    
    if role not in ['student', 'staff', 'hod']:
        flash('Invalid role specified!', 'danger')
        return redirect(url_for('super_admin_users'))
    
    old_role = user.role
    user.role = role
    db.session.commit()
    
    flash(f'User role changed from {old_role} to {role} for {user.username}', 'success')
    create_notification(user.id, None, f'Your account role has been changed from {old_role} to {role}', 'role_change')
    
    return redirect(url_for('super_admin_users'))


@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@login_required
def super_admin_delete_user(user_id):
    if not is_super_admin(current_user):
        abort(403)
    
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot delete your own account!', 'danger')
        return redirect(url_for('super_admin_users'))
    
    try:
        Notification.query.filter_by(user_id=user.id).delete()
        Comment.query.filter_by(user_id=user.id).delete()
        Complaint.query.filter_by(assigned_to=user.id).update({'assigned_to': None})
        Complaint.query.filter_by(mentor_id=user.id).update({'mentor_id': None})
        Complaint.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        
        flash(f'User "{user.username}" has been deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting user: {str(e)}', 'danger')
    
    return redirect(url_for('super_admin_users'))


# ========== DEPARTMENT MANAGEMENT ROUTES ==========
@app.route('/admin/departments')
@login_required
def manage_departments():
    if not is_super_admin(current_user):
        abort(403)
    
    departments = Department.query.all()
    
    # Calculate counts for each department in the route (not in template)
    for dept in departments:
        dept.student_count = User.query.filter_by(department=dept.name, role='student').count()
        dept.staff_count = User.query.filter_by(department=dept.name, role='staff').count()
        # Use explicit join to avoid ambiguous foreign key
        dept.complaint_count = Complaint.query.join(User, Complaint.user_id == User.id).filter(User.department == dept.name).count()
    
    return render_template('manage_departments.html', departments=departments)


@app.route('/admin/department/add', methods=['GET', 'POST'])
@login_required
def add_department():
    if not is_super_admin(current_user):
        abort(403)
    
    form = DepartmentForm()
    if form.validate_on_submit():
        department = Department(
            name=form.name.data,
            hod_id=form.hod_id.data if form.hod_id.data != 0 else None
        )
        db.session.add(department)
        db.session.commit()
        flash(f'Department "{form.name.data}" added successfully!', 'success')
        return redirect(url_for('manage_departments'))
    
    return render_template('add_department.html', form=form)


@app.route('/admin/department/<int:dept_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_department(dept_id):
    if not is_super_admin(current_user):
        abort(403)
    
    department = Department.query.get_or_404(dept_id)
    form = DepartmentForm()
    
    if form.validate_on_submit():
        department.name = form.name.data
        department.hod_id = form.hod_id.data if form.hod_id.data != 0 else None
        db.session.commit()
        flash(f'Department updated successfully!', 'success')
        return redirect(url_for('manage_departments'))
    
    form.name.data = department.name
    if department.hod_id:
        form.hod_id.data = department.hod_id
    
    return render_template('edit_department.html', form=form, department=department)

@app.route('/admin/department/<int:id>/delete', methods=['POST'])
@login_required
def delete_department(id):
    department = Department.query.get_or_404(id)

    # Count users in this department
    total_users = User.query.filter_by(department=department.name).count()

    if total_users > 0:
        flash("Cannot delete department. Users are assigned to it.", "danger")
        return redirect(url_for('manage_departments'))  # ✅ FIXED

    db.session.delete(department)
    db.session.commit()

    flash("Department deleted successfully!", "success")
    return redirect(url_for('manage_departments'))  # ✅ FIXED

# ========== NOTIFICATION & PROFILE ROUTES ==========

@app.route('/notifications')
@login_required
def view_notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    for notification in notifications:
        notification.is_read = True
    db.session.commit()
    return render_template('notifications.html', notifications=notifications)


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=current_user)


@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    form = UpdateProfileForm(target_user=current_user, obj=current_user)
    if form.validate_on_submit():
        current_user.username = form.username.data
        current_user.email = form.email.data
        current_user.department = form.department.data
        current_user.year = form.year.data
        current_user.section = form.section.data
        current_user.phone = form.phone.data
        current_user.parent_name = form.parent_name.data
        current_user.parent_phone = form.parent_phone.data
        current_user.address = form.address.data
        db.session.commit()
        flash('Your profile has been updated successfully.', 'success')
        return redirect(url_for('profile'))
    return render_template('edit_profile.html', form=form, title='Edit Profile', heading='Edit Your Profile', cancel_url=url_for('profile'))


@app.route('/student/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_student_profile(student_id):
    student = User.query.get_or_404(student_id)
    if current_user.id != student.id and not can_manage_user(current_user, student):
        abort(403)

    form = UpdateProfileForm(target_user=student, obj=student)
    if form.validate_on_submit():
        student.username = form.username.data
        student.email = form.email.data
        student.department = form.department.data
        student.year = form.year.data
        student.section = form.section.data
        student.phone = form.phone.data
        student.parent_name = form.parent_name.data
        student.parent_phone = form.parent_phone.data
        student.address = form.address.data
        db.session.commit()
        flash('Student profile has been updated successfully.', 'success')
        if current_user.id == student.id:
            return redirect(url_for('profile'))
        return redirect(url_for('mentor_student_profile', student_id=student.id))

    cancel_target = url_for('profile') if current_user.id == student.id else url_for('mentor_student_profile', student_id=student.id)
    return render_template('edit_profile.html', form=form, title='Edit Student Profile', heading=f'Edit Profile: {student.username}', cancel_url=cancel_target)


@app.route('/profile/delete', methods=['POST'])
@login_required
def delete_own_account():
    user = current_user
    try:
        username = user.username
        
        Notification.query.filter_by(user_id=user.id).delete()
        Comment.query.filter_by(user_id=user.id).delete()
        Complaint.query.filter_by(assigned_to=user.id).update({'assigned_to': None})
        Complaint.query.filter_by(mentor_id=user.id).update({'mentor_id': None})
        Complaint.query.filter_by(user_id=user.id).delete()
        
        logout_user()
        db.session.delete(user)
        db.session.commit()
        
        flash(f'Your account "{username}" has been deleted successfully!', 'success')
        return redirect(url_for('index'))
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting account: {str(e)}', 'danger')
        return redirect(url_for('profile'))


# ========== PASSWORD RESET ROUTES ==========

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            otp = generate_otp()
            expires_at = datetime.utcnow() + timedelta(minutes=10)
            
            reset_request = PasswordResetOTP(
                email=user.email,
                otp=otp,
                expires_at=expires_at
            )
            db.session.add(reset_request)
            db.session.commit()
            
            send_otp_email(user.email, otp)
            flash('OTP has been sent to your email. It expires in 10 minutes.', 'info')
            return redirect(url_for('reset_password', email=user.email))
        else:
            flash('No account found with that email address.', 'danger')
    
    return render_template('forgot_password.html', form=form)


@app.route('/reset-password/<email>', methods=['GET', 'POST'])
def reset_password(email):
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Invalid request.', 'danger')
        return redirect(url_for('login'))
    
    form = ResetPasswordForm()
    if form.validate_on_submit():
        otp_record = PasswordResetOTP.query.filter_by(
            email=email, 
            otp=form.otp.data,
            is_used=False
        ).order_by(PasswordResetOTP.created_at.desc()).first()
        
        if not otp_record:
            flash('Invalid OTP.', 'danger')
            return redirect(url_for('reset_password', email=email))
        
        if datetime.utcnow() > otp_record.expires_at:
            flash('OTP has expired. Please request a new one.', 'danger')
            return redirect(url_for('forgot_password'))
        
        user.password = generate_password_hash(form.new_password.data)
        otp_record.is_used = True
        db.session.commit()
        
        flash('Your password has been reset successfully! Please login with your new password.', 'success')
        return redirect(url_for('login'))
    
    return render_template('reset_password.html', form=form, email=email)


# ========== TEMPORARY FIX ROUTES ==========

@app.route('/fix-complaint-ids')
@login_required
def fix_complaint_ids():
    if not is_super_admin(current_user):
        return "Only admin can access this", 403
    
    try:
        from sqlalchemy import text
        complaints = Complaint.query.order_by(Complaint.created_at).all()
        result = "<html><body><h2>Fixing Complaint IDs...</h2>"
        result += f"<p>Found {len(complaints)} complaints</p>"
        fixed_count = 0
        
        for idx, complaint in enumerate(complaints, start=1):
            old_id = complaint.complaint_id
            new_id = f'ESEC{idx:02d}'
            if old_id != new_id:
                db.session.execute(
                    text('UPDATE complaint SET complaint_id = :new_id WHERE id = :comp_id'),
                    {'new_id': new_id, 'comp_id': complaint.id}
                )
                result += f"<p>Changed: {old_id} → {new_id}</p>"
                fixed_count += 1
        
        db.session.commit()
        result += f"<h2 style='color:green'>✅ Fixed {fixed_count} complaints!</h2>"
        result += "<a href='/complaints'>Go to My Complaints</a></body></html>"
        return result
    except Exception as e:
        db.session.rollback()
        return f"<h2 style='color:red'>Error: {str(e)}</h2>"


@app.route('/test')
def test():
    return "✅ App is working!"


@app.route('/test-email')
@login_required
def test_email():
    if not is_super_admin(current_user):
        abort(403)
    
    try:
        send_email_notification(
            current_user.email,
            'Test Email',
            'This is a test email from your Complaint Management System. Email is working!',
            mail
        )
        flash('Test email sent successfully!', 'success')
    except Exception as e:
        flash(f'Error sending email: {str(e)}', 'danger')
    
    return redirect(url_for('super_admin_dashboard'))


# ========== CONTEXT PROCESSORS ==========

@app.context_processor
def utility_processor():
    return {
        'is_super_admin': is_super_admin,
        'is_department_admin': is_department_admin,
        'can_manage_user': can_manage_user
    }


if __name__ == '__main__':
    app.run(debug=True)
