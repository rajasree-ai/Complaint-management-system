from flask import Flask, render_template, redirect, url_for, flash, request, abort, jsonify, make_response, session
from flask_login import login_user, current_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
import logging
import sys
import os
import csv
import io
from urllib.parse import urlparse
from dotenv import load_dotenv
from flask_mail import Mail, Message
# Load environment variables from .env for local development
load_dotenv(override=True)

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db, login_manager
from models import User, Complaint, Comment, Notification, Department, PasswordResetOTP, StudentStaffAssignment, HODDepartment
from forms import (
    RegistrationForm, LoginForm, ComplaintForm, CommentForm, UpdateComplaintForm,
    ForgotPasswordForm, ResetPasswordForm, DepartmentForm, 
    RemoveStudentAssignmentForm, StaffStudentAssignmentForm, UpdateProfileForm
)
from utils import (
    generate_complaint_id,
    send_email_notification,
    send_complaint_registration_email,
    send_comment_notification,
    send_status_update_email,
    notify_merged_duplicate_authors,
    send_otp_email,
    send_csv_imported_student_email,
    send_csv_imported_staff_email,
    generate_otp,
    generate_random_password,
    create_notification,
    calculate_complaint_stats,
    get_hod_department,
    utc_to_local
)
from sqlalchemy import inspect, text, func
from automation import init_automation, compute_deadline, run_auto_responder
from ai_analytics import classify_complaint, get_analytics_summary, CATEGORIES, suggest_form_category
# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or os.urandom(32).hex()

database_url = os.environ.get('DATABASE_URL') or 'sqlite:///grievance_hub.db'
database_url = database_url.strip().strip('"').strip("'")
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url

parsed_db = urlparse(database_url)
if parsed_db.scheme in ('postgresql', 'postgres'):
    print(f"DATABASE_URL host: {parsed_db.hostname}")
    print(f"DATABASE_URL path: {parsed_db.path}")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.sendgrid.net')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME') or os.environ.get('MAIL_DEFAULT_SENDER') or 'apikey'
app.config['MAIL_PASSWORD'] = (
    os.environ.get('MAIL_PASSWORD')
    or os.environ.get('BREVO_API_KEY')
    or os.environ.get('SENDGRID_API_KEY')
)
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'no-reply@example.com')

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
# Postgres-only connection tuning: fail fast instead of hanging forever if the
# Supabase pooler is unreachable, and skip psycopg2's extra hstore-support
# probe (this app doesn't use hstore). connect_timeout bounds the initial
# handshake; statement_timeout bounds any individual query afterwards.
if parsed_db.scheme in ('postgresql', 'postgres'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args'] = {
        "connect_timeout": 10,
        "options": "-c search_path=public -c statement_timeout=15000",
    }
    app.config['SQLALCHEMY_ENGINE_OPTIONS']['use_native_hstore'] = False
# Initialize extensions
db.init_app(app)
login_manager.init_app(app)

# Start background automation (escalation, assignment, duplicate detection, reports,
# reminders, backups, overdue status updates). See automation.py for details.
init_automation(app)

# Compatibility shim: some Flask versions may remove `before_first_request`.
# Provide a decorator that runs the wrapped function only once (on the first request).
if not hasattr(app, 'before_first_request'):
    def before_first_request(func):
        def _wrapper(*args, **kwargs):
            if not getattr(app, '_got_first_request', False):
                app._got_first_request = True
                return func(*args, **kwargs)
            return None
        app.before_request(_wrapper)
        return func

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


def get_assigned_student_ids(department_name):
    """Return the student IDs that already have an assignment in a department."""
    assigned_rows = db.session.query(StudentStaffAssignment.student_id).filter(
        StudentStaffAssignment.department == department_name
    ).distinct().all()
    return {student_id for (student_id,) in assigned_rows}


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


def get_primary_assigned_mentor(student):
    """Return the primary mentor/staff assignment for a student"""
    assignment = StudentStaffAssignment.query.filter_by(student_id=student.id).order_by(StudentStaffAssignment.assigned_at.desc()).first()
    return assignment.staff if assignment else None


def get_hod_departments(user):
    """Return the list of Department objects this HOD has access to (via
    HODDepartment), sorted by name. Falls back to their primary department
    (user.department) if the join table has no rows for them yet — this keeps
    single-department HODs working even before the one-time migration runs."""
    if user.role != 'hod':
        return []

    links = HODDepartment.query.filter_by(user_id=user.id).all()
    if links:
        dept_ids = [link.department_id for link in links]
        return Department.query.filter(Department.id.in_(dept_ids)).order_by(Department.name).all()

    # No join-table rows yet: fall back to their primary department string.
    if user.department:
        dept = Department.query.filter_by(name=user.department).first()
        if dept:
            return [dept]
    return []


def get_active_department(user):
    """Return the department NAME (string) the HOD is currently operating on.
    Reads session['active_department_id'], validated against the HOD's actual
    accessible departments. If the session value is missing/invalid/stale,
    defaults to the HOD's home department (user.department) if it's one of
    their accessible departments, otherwise falls back to their first
    accessible department (alphabetically)."""
    accessible = get_hod_departments(user)

    active_id = session.get('active_department_id')
    if active_id:
        for dept in accessible:
            if dept.id == active_id:
                return dept.name

    # Session value missing or no longer valid — default to home department if accessible.
    for dept in accessible:
        if dept.name == user.department:
            session['active_department_id'] = dept.id
            return dept.name

    # Home department isn't in their accessible list (unusual) — fall back to first accessible.
    if accessible:
        session['active_department_id'] = accessible[0].id
        return accessible[0].name

    return user.department


DEFAULT_IMPORT_PASSWORD = os.environ.get('DEFAULT_IMPORT_PASSWORD', 'Grievance@123')


def _parse_csv_stream(file_storage):
    """Parse an uploaded CSV FileStorage into a list of dict rows."""
    import csv, io
    raw = file_storage.stream.read().decode('utf-8-sig')
    stream = io.StringIO(raw, newline=None)
    reader = csv.DictReader(stream)
    rows = [row for row in reader]
    return rows, reader.fieldnames


def _import_students(rows, department, default_password):
    """Create student accounts from parsed CSV rows. Returns a summary dict."""
    required = ['username', 'email', 'roll_number', 'year', 'section']
    created, skipped, errors = [], [], []
    hashed_password = generate_password_hash(default_password)

    for i, raw_row in enumerate(rows, start=2):  # row 1 is the header
        row = {k.strip(): (v.strip() if v else v) for k, v in raw_row.items() if k}
        missing = [f for f in required if not row.get(f)]
        if missing:
            errors.append(f"Row {i}: missing {', '.join(missing)}")
            continue

        email = row['email'].lower()
        roll_number = row['roll_number']

        existing = User.query.filter(
            (User.email == email) | (User.roll_number == roll_number)
        ).first()
        if existing:
            skipped.append(f"Row {i}: {row['username']} ({email}) already exists")
            continue

        student = User(
            username=row['username'],
            email=email,
            roll_number=roll_number,
            password=hashed_password,
            role='student',
            department=department,
            year=row['year'],
            section=row['section'],
            phone=row.get('phone') or None,
            parent_name=row.get('parent_name') or None,
            parent_phone=row.get('parent_phone') or None,
            address=row.get('address') or None
        )
        db.session.add(student)
        created.append(f"{row['username']} ({email})")

    if created:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            errors.append("Database error while saving — please retry with the failed rows.")
            created = []

    return {'created': created, 'skipped': skipped, 'errors': errors}


def _import_staff(rows, department, default_password):
    """Create staff accounts from parsed CSV rows. Returns a summary dict."""
    required = ['username', 'email']
    created, skipped, errors = [], [], []
    hashed_password = generate_password_hash(default_password)

    for i, raw_row in enumerate(rows, start=2):
        row = {k.strip(): (v.strip() if v else v) for k, v in raw_row.items() if k}
        missing = [f for f in required if not row.get(f)]
        if missing:
            errors.append(f"Row {i}: missing {', '.join(missing)}")
            continue

        email = row['email'].lower()
        existing = User.query.filter_by(email=email).first()
        if existing:
            skipped.append(f"Row {i}: {row['username']} ({email}) already exists")
            continue

        staff = User(
            username=row['username'],
            email=email,
            password=hashed_password,
            role='staff',
            department=department,
            phone=row.get('phone') or None
        )
        db.session.add(staff)
        created.append(f"{row['username']} ({email})")

    if created:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            errors.append("Database error while saving — please retry with the failed rows.")
            created = []

    return {'created': created, 'skipped': skipped, 'errors': errors}


def _import_hods(rows, default_password):
    """Create HOD accounts from parsed CSV rows, or grant additional
    department access to an existing HOD if the email already exists.
    Requires a 'department' column per row since super admin isn't scoped
    to one department. Returns a summary dict with 'created', 'linked'
    (existing HOD granted a new department), 'skipped', and 'errors'."""
    required = ['username', 'email', 'department']
    created, linked, skipped, errors = [], [], [], []
    hashed_password = generate_password_hash(default_password)
    departments_by_name = {d.name: d for d in Department.query.all()}

    for i, raw_row in enumerate(rows, start=2):
        row = {k.strip(): (v.strip() if v else v) for k, v in raw_row.items() if k}
        missing = [f for f in required if not row.get(f)]
        if missing:
            errors.append(f"Row {i}: missing {', '.join(missing)}")
            continue

        email = row['email'].lower()
        department_name = row['department']

        if department_name not in departments_by_name:
            errors.append(f"Row {i}: unknown department '{department_name}'")
            continue

        dept = departments_by_name[department_name]
        existing_user = User.query.filter_by(email=email).first()

        if existing_user:
            # Same person (matched by email) getting access to another
            # department, rather than a genuine duplicate — link instead of skip.
            already_linked = HODDepartment.query.filter_by(
                user_id=existing_user.id, department_id=dept.id
            ).first()
            if already_linked:
                skipped.append(f"Row {i}: {existing_user.username} ({email}) already has access to {department_name}")
                continue

            if existing_user.role != 'hod':
                existing_user.role = 'hod'

            db.session.add(HODDepartment(user_id=existing_user.id, department_id=dept.id))

            # If this department has no official HOD yet, make this person official here too.
            if not dept.hod_id:
                dept.hod_id = existing_user.id

            linked.append(f"Row {i}: {existing_user.username} ({email}) -> {department_name} (existing HOD, department added)")
            continue

        if dept.hod_id:
            skipped.append(f"Row {i}: {department_name} already has an HOD assigned")
            continue

        hod = User(
            username=row['username'],
            email=email,
            password=hashed_password,
            role='hod',
            department=department_name,
            phone=row.get('phone') or None
        )
        db.session.add(hod)
        db.session.flush()  # get hod.id before committing

        dept.hod_id = hod.id
        db.session.add(HODDepartment(user_id=hod.id, department_id=dept.id))
        created.append(f"{row['username']} ({email}) -> {department_name}")

    if created or linked:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            errors.append("Database error while saving — please retry with the failed rows.")
            created = []
            linked = []

    return {'created': created, 'linked': linked, 'skipped': skipped, 'errors': errors}


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

def get_top_complaint_department():
    """Return (department_name, total_complaint_count) for whichever department
    has the most complaints filed against it, all-time. Returns None if there
    are no complaints yet."""
    result = (
        db.session.query(User.department, func.count(Complaint.id).label('cnt'))
        .join(Complaint, Complaint.user_id == User.id)
        .group_by(User.department)
        .order_by(func.count(Complaint.id).desc())
        .first()
    )
    return result  # (department_name, count) tuple, or None
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
    """Renumber student IDs to be sequential after deletion.
    Only supported on SQLite (uses PRAGMA to safely bypass FK checks during the shuffle).
    On Postgres, ID gaps after deletion are harmless and cosmetic only, so this is a no-op there
    rather than risking a foreign-key-constraint-violation mid-renumber.
    """
    if db.engine.dialect.name != 'sqlite':
        print('Skipping student ID renumbering (not supported on this database; safe to skip — IDs having gaps is harmless)')
        return

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
            # Run create_all but don't fail completely if it errors (e.g., missing auth schema in SQLite).
            try:
                db.create_all()
            except Exception as e:
                app.logger.exception('db.create_all failed; continuing with best-effort schema fixes')

            # Ensure legacy SQLite databases get the missing mentor_id column
            try:
                inspector = inspect(db.engine)
                table_names = inspector.get_table_names()
            except Exception:
                app.logger.exception(
                    'Could not connect to the database to inspect its schema. '
                    'The app will continue starting, but pages that need the '
                    'database will fail until this is resolved.'
                )
                return

            if 'complaint' in table_names:
                complaint_columns = [column['name'] for column in inspector.get_columns('complaint')]
                if 'mentor_id' not in complaint_columns:
                    try:
                        with db.engine.begin() as connection:
                            connection.execute(text('ALTER TABLE complaint ADD COLUMN mentor_id INTEGER'))
                        print('Added missing mentor_id column to complaint table')
                    except Exception as e:
                        app.logger.exception('Could not add mentor_id column to complaint table')

                # Ensure the complaint table has an action_taken column for storing notes about actions
                if 'action_taken' not in complaint_columns:
                    try:
                        with db.engine.begin() as connection:
                            # Use TEXT which works on both Postgres and SQLite
                            connection.execute(text('ALTER TABLE complaint ADD COLUMN action_taken TEXT'))
                        print('Added missing action_taken column to complaint table')
                    except Exception as e:
                        app.logger.exception('Could not add action_taken column to complaint table')

                # Ensure automation-related columns exist (deadline, escalation, duplicates, etc.)
                # Types below work on both SQLite and Postgres (unlike SQLite-only names like DATETIME).
                automation_columns = {
                    'deadline': 'TIMESTAMP',
                    'is_overdue': 'BOOLEAN DEFAULT FALSE',
                    'escalation_level': 'INTEGER DEFAULT 0',
                    'last_escalated_at': 'TIMESTAMP',
                    'last_reminded_at': 'TIMESTAMP',
                    'is_duplicate_of': 'INTEGER',
                    'auto_response_sent': 'BOOLEAN DEFAULT FALSE',
                    'ai_category': 'VARCHAR(50)',
                }
                for col_name, col_type in automation_columns.items():
                    if col_name not in complaint_columns:
                        try:
                            with db.engine.begin() as connection:
                                connection.execute(text(f'ALTER TABLE complaint ADD COLUMN {col_name} {col_type}'))
                            print(f'Added missing {col_name} column to complaint table')
                        except Exception as e:
                            app.logger.exception(f'Could not add {col_name} column to complaint table')

            if 'student_staff_assignment' in inspector.get_table_names():
                duplicate_groups = (
                    db.session.query(
                        StudentStaffAssignment.student_id,
                        StudentStaffAssignment.department,
                        func.min(StudentStaffAssignment.id).label('keep_id')
                    )
                    .group_by(StudentStaffAssignment.student_id, StudentStaffAssignment.department)
                    .having(func.count(StudentStaffAssignment.id) > 1)
                    .all()
                )

                if duplicate_groups:
                    for student_id, department_name, keep_id in duplicate_groups:
                        stale_assignments = StudentStaffAssignment.query.filter(
                            StudentStaffAssignment.student_id == student_id,
                            StudentStaffAssignment.department == department_name,
                            StudentStaffAssignment.id != keep_id
                        ).all()
                        for assignment in stale_assignments:
                            db.session.delete(assignment)
                    db.session.commit()
                    print('Cleaned up duplicate student assignments')

                try:
                    with db.engine.begin() as connection:
                        connection.execute(text(
                            'CREATE UNIQUE INDEX IF NOT EXISTS ix_student_staff_assignment_unique_student_department '
                            'ON student_staff_assignment (student_id, department)'
                        ))
                    print('Ensured student assignment uniqueness index exists')
                except Exception:
                    app.logger.exception('Could not create unique index for student_staff_assignment')

            # Backfill HODDepartment rows for every department that already has an
            # hod_id set, so existing single-department HODs keep working under the
            # new multi-department access model without any manual step.
            if 'hod_department' in inspector.get_table_names():
                try:
                    departments_with_hod = Department.query.filter(Department.hod_id.isnot(None)).all()
                    backfilled = 0
                    for dept in departments_with_hod:
                        existing_link = HODDepartment.query.filter_by(
                            user_id=dept.hod_id, department_id=dept.id
                        ).first()
                        if not existing_link:
                            db.session.add(HODDepartment(user_id=dept.hod_id, department_id=dept.id))
                            backfilled += 1
                    if backfilled:
                        db.session.commit()
                        print(f'Backfilled {backfilled} HODDepartment link(s) from existing hod_id values')
                except Exception:
                    db.session.rollback()
                    app.logger.exception('Could not backfill HODDepartment rows')

            # Widen phone/parent_phone columns to fit numbers like "999.../888..."
            # that some CSV imports contain (two numbers in one field).
            try:
                if 'user' in inspector.get_table_names():
                    with db.engine.begin() as connection:
                        connection.execute(text('ALTER TABLE "user" ALTER COLUMN parent_phone TYPE VARCHAR(50)'))
                        connection.execute(text('ALTER TABLE "user" ALTER COLUMN phone TYPE VARCHAR(20)'))
                    print('Widened phone/parent_phone columns')
            except Exception:
                app.logger.exception('Could not widen phone/parent_phone columns (may already be widened)')

            # Username is a display name, not a login credential (login uses email) — allow
            # duplicates since real students can share the same name across different batches.
            try:
                if 'user' in inspector.get_table_names():
                    unique_constraints = inspector.get_unique_constraints('user')
                    for uc in unique_constraints:
                        if 'username' in uc.get('columns', []):
                            constraint_name = uc.get('name')
                            if constraint_name:
                                with db.engine.begin() as connection:
                                    connection.execute(text(f'ALTER TABLE "user" DROP CONSTRAINT IF EXISTS "{constraint_name}"'))
                                print(f'Dropped unique constraint {constraint_name} on user.username')
            except Exception:
                app.logger.exception('Could not drop unique constraint on username (may not exist or already dropped)')

            # Ensure local SQLite user table has the roll_number column (add if missing).
            try:
                if 'user' in inspector.get_table_names():
                    user_columns = [c['name'] for c in inspector.get_columns('user')]
                    if 'roll_number' not in user_columns:
                        try:
                            print('Adding missing roll_number column to user table')
                            if 'complaint' in inspector.get_table_names():
                                complaint_columns_2 = [column['name'] for column in inspector.get_columns('complaint')]
                                if 'action_taken' not in complaint_columns_2:
                                    with db.engine.begin() as connection:
                                        connection.execute(text('ALTER TABLE complaint ADD COLUMN action_taken TEXT'))
                                    print('Added missing action_taken column to complaint table')
                            with db.engine.begin() as connection:
                                # Use a VARCHAR(20) compatible type; SQLite will accept it as TEXT
                                connection.execute(text('ALTER TABLE "user" ADD COLUMN roll_number VARCHAR(20)'))
                            print('Added roll_number column to user table')
                        except Exception as e:
                            # Log and continue; do not fail initialization because of schema changes
                            app.logger.exception(f'Could not add roll_number column: {e}')
            except Exception:
                app.logger.exception('Failed inspecting tables for roll_number migration')

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
        print('WARNING: Database initialization failed. Application startup continues, but database access may be unavailable.')


db_initialized = False

@app.before_request
def startup_db():
    global db_initialized
    if not db_initialized:
        initialize_database()
        db_initialized = True


# ========== MAIN ROUTES ==========

@app.route('/')
def index():
    return redirect(url_for('login'))
@app.route('/users')
@login_required
def users():
    users = User.query.all()
    return render_template('users.html', users=users)

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
            roll_number=form.roll_number.data.strip() if form.roll_number.data else None,
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
        # Ensure the hashed password is set (in case template masked password assignment)
        user.password = hashed_password
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
Grievance Hub
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
Grievance Hub
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

@app.route('/hod/switch-department/<int:dept_id>')
@login_required
def switch_hod_department(dept_id):
    """Let a multi-department HOD switch which department they're currently
    operating on. Only allows switching to a department they actually have
    access to (via HODDepartment); silently ignores invalid attempts."""
    if not is_department_admin(current_user):
        abort(403)

    accessible = get_hod_departments(current_user)
    if any(dept.id == dept_id for dept in accessible):
        session['active_department_id'] = dept_id
        dept = Department.query.get(dept_id)
        flash(f'Switched to {dept.name} department.', 'info')
    else:
        flash('You do not have access to that department.', 'danger')

    next_page = request.args.get('next')
    return redirect(next_page) if next_page else redirect(url_for('hod_dashboard'))


@app.route('/hod/dashboard')
@login_required
def hod_dashboard():
    if not is_department_admin(current_user):
        abort(403)
    
    department_name = get_active_department(current_user)
    complaints = Complaint.query.join(User, Complaint.user_id == User.id).filter(
        User.department == department_name,
        Complaint.is_overdue.is_(True)
    ).all()
    users = User.query.filter_by(department=department_name).all()
    users = User.query.filter_by(department=department_name).all()
    students = User.query.filter_by(department=department_name, role='student').all()
    staff = User.query.filter_by(department=department_name, role='staff').all()
    stats = calculate_complaint_stats(complaints)
    
    total_students = len(students)
    total_staff = len(staff)
    pending_complaints = stats['pending']
    resolved_complaints = stats['resolved']
    total_complaints = stats['total']
    resolution_rate = round((resolved_complaints / total_complaints * 100) if total_complaints else 0, 1)
    overdue_count = sum(1 for c in complaints if c.is_overdue)
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
                         resolution_rate=resolution_rate,
                         overdue_count=overdue_count,
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
    resolution_rate = round((stats['resolved'] / stats['total'] * 100) if stats['total'] else 0, 1)
    overdue_count = sum(1 for c in complaints if c.is_overdue)
    notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    
    return render_template('staff_dashboard.html', 
                         complaints=complaints,
                         stats=stats,
                         resolution_rate=resolution_rate,
                         overdue_count=overdue_count,
                         notifications=notifications,
                         staff_name=current_user.username,
                         department=current_user.department)


# ========== MENTOR (STAFF) ROUTES ==========

@app.route('/my-mentors')
@login_required
def my_mentors():
    if current_user.role != 'student':
        abort(403)
    
    assignments = StudentStaffAssignment.query.filter_by(student_id=current_user.id).all()
    mentor_map = {}
    for assignment in assignments:
        if assignment.staff and assignment.staff.id not in mentor_map:
            mentor_map[assignment.staff.id] = assignment.staff
    mentors = list(mentor_map.values())
    return render_template('my_mentors.html', mentors=mentors, assignments=assignments, department=current_user.department)


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
    
     # Sort each group's students by roll number (not just the group keys)
    import re

    def roll_number_sort_key(student_data):
        roll = student_data['user'].roll_number
        if not roll:
            return (1, 0, '')
        match = re.search(r'(\d+)$', roll)
        if match:
            return (0, int(match.group(1)), roll)
        return (0, 0, roll)

    for year_section in grouped_students:
        grouped_students[year_section].sort(key=roll_number_sort_key)
    for year_section in grouped_assigned_students:
        grouped_assigned_students[year_section].sort(key=roll_number_sort_key)

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

# ========== STUDENT EXPORT ROUTES (Staff/Mentor) ==========

# Canonical list of exportable student fields: (key, column label).
# Order here is the order columns appear in, regardless of what order the
# user checked them in the picker UI.
STUDENT_EXPORT_COLUMNS = [
    ('roll_number', 'Roll Number'),
    ('username', 'Name'),
    ('email', 'Email'),
    ('year', 'Year'),
    ('section', 'Section'),
    ('mentor', 'Mentor'),
    ('phone', 'Phone'),
    ('parent_name', 'Parent Name'),
    ('parent_phone', 'Parent Phone'),
    ('address', 'Address'),
]


def _normalize_year_display(year_value):
    """Convert Year values like '1st Year', '2nd Year', '3rd Year', '4th Year'
    into just the number ('1', '2', '3', '4') for exports. Falls back to the
    original value unchanged if it doesn't match the expected pattern."""
    import re
    if not year_value:
        return ''
    match = re.match(r'^\s*(\d+)', str(year_value))
    return match.group(1) if match else str(year_value)


def _student_row_dict(student, mentor=None):
    if mentor is None:
        mentor = get_primary_assigned_mentor(student)
    return {
        'roll_number': student.roll_number or '',
        'username': student.username,
        'email': student.email,
        'year': _normalize_year_display(student.year),
        'section': student.section or '',
        'mentor': mentor.username if mentor else 'Not assigned',
        'phone': student.phone or '',
        'parent_name': student.parent_name or '',
        'parent_phone': student.parent_phone or '',
        'address': student.address or '',
    }

def _resolve_export_columns(requested_keys, all_columns):
    """Filter all_columns (list of (key,label)) down to just the requested
    keys, preserving the canonical order. If requested_keys is empty or
    matches nothing, falls back to all_columns so an export never comes back
    with zero columns (e.g. a stale bookmark with no ?columns= param)."""
    if not requested_keys:
        return all_columns
    requested_set = set(requested_keys)
    filtered = [(key, label) for key, label in all_columns if key in requested_set]
    return filtered if filtered else all_columns


def _get_requested_columns():
    """Read ?columns=key1,key2,... from the query string."""
    raw = request.args.get('columns', '')
    return [c.strip() for c in raw.split(',') if c.strip()]


# Relative width weights per column key, used to proportionally divide the
# available page width so PDF table columns never overflow or overlap.
# Wider fields (email, address, description) get more weight; short fields
# (year, section, priority) get less. Unlisted keys default to 1.0.
PDF_COLUMN_WIDTH_WEIGHTS = {
    'roll_number': 0.9,
    'username': 1.2,
    'email': 1.7,
    'year': 0.6,
    'section': 0.6,
    'mentor': 1.1,
    'phone': 1.0,
    'parent_name': 1.2,
    'parent_phone': 1.0,
    'address': 2.0,
    'complaint_id': 0.9,
    'student_name': 1.1,
    'title': 1.4,
    'description': 2.2,
    'category': 0.9,
    'status': 0.8,
    'priority': 0.7,
    'action_taken': 1.6,
    'created_at': 1.0,
}


def _build_pdf_table(selected_columns, row_dicts, available_width, header_bg='#343a40'):
    """Build a reportlab Table for an export PDF where every cell is a
    word-wrapping Paragraph and column widths are proportionally sized to
    available_width — this is what prevents columns from overlapping or
    text spilling outside its cell, regardless of how many columns are
    selected or how long the content is (long emails/addresses included,
    since wordWrap='CJK' allows breaking mid-word if there's no space)."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib.styles import ParagraphStyle

    header_style = ParagraphStyle(
        'PdfTableHeader', fontName='Helvetica-Bold', fontSize=7.5,
        leading=9, textColor=colors.white, wordWrap='CJK'
    )
    cell_style = ParagraphStyle(
        'PdfTableCell', fontName='Helvetica', fontSize=7,
        leading=8.5, wordWrap='CJK'
    )

    header_row = [Paragraph(label, header_style) for key, label in selected_columns]
    table_data = [header_row]
    for row in row_dicts:
        table_data.append([
            Paragraph(str(row.get(key) or '-'), cell_style)
            for key, label in selected_columns
        ])

    weights = [PDF_COLUMN_WIDTH_WEIGHTS.get(key, 1.0) for key, label in selected_columns]
    total_weight = sum(weights) or 1.0
    col_widths = [available_width * (w / total_weight) for w in weights]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f2f2f2')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    return table


def _build_students_pdf(students, title, columns=None):
    from io import BytesIO
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    selected = _resolve_export_columns(columns, STUDENT_EXPORT_COLUMNS)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(title, styles['Title']))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%d-%m-%Y %H:%M')} UTC &nbsp;&nbsp; "
        f"Total: {len(students)}",
        styles['Normal']
    ))
    story.append(Spacer(1, 12))

    row_dicts = [_student_row_dict(s) for s in students]
    table = _build_pdf_table(selected, row_dicts, doc.width)
    story.append(table)

    doc.build(story)
    buffer.seek(0)
    return buffer


def _build_students_csv(students, columns=None):
    import csv
    from io import StringIO

    selected = _resolve_export_columns(columns, STUDENT_EXPORT_COLUMNS)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([label for key, label in selected])
    for s in students:
        row = _student_row_dict(s)
        writer.writerow([row[key] for key, label in selected])
    output.seek(0)
    return output


@app.route('/mentor/students/export/csv')
@login_required
def export_department_students_csv():
    if current_user.role != 'staff':
        abort(403)
    from flask import Response
    students = User.query.filter_by(department=current_user.department, role='student').order_by(User.roll_number).all()
    csv_data = _build_students_csv(students, columns=_get_requested_columns())
    filename = f"department_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        csv_data.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/mentor/students/export/pdf')
@login_required
def export_department_students_pdf():
    if current_user.role != 'staff':
        abort(403)
    from flask import send_file
    students = User.query.filter_by(department=current_user.department, role='student').order_by(User.roll_number).all()
    buffer = _build_students_pdf(students, f"All Students - {current_user.department}", columns=_get_requested_columns())
    filename = f"department_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/mentor/assigned-students/export/csv')
@login_required
def export_assigned_students_csv():
    if current_user.role != 'staff':
        abort(403)
    from flask import Response
    assignments = StudentStaffAssignment.query.filter_by(
        staff_id=current_user.id,
        department=current_user.department
    ).all()
    students = [a.student for a in assignments if a.student]
    csv_data = _build_students_csv(students, columns=_get_requested_columns())
    filename = f"my_assigned_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        csv_data.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/mentor/assigned-students/export/pdf')
@login_required
def export_assigned_students_pdf():
    if current_user.role != 'staff':
        abort(403)
    from flask import send_file
    assignments = StudentStaffAssignment.query.filter_by(
        staff_id=current_user.id,
        department=current_user.department
    ).all()
    students = [a.student for a in assignments if a.student]
    buffer = _build_students_pdf(students, f"My Assigned Students - {current_user.username}", columns=_get_requested_columns())
    filename = f"my_assigned_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')
# ========== STUDENT EXPORT ROUTES (HOD - year/section wise) ==========

@app.route('/hod/students/export/csv')
@login_required
def export_hod_students_csv():
    if not is_department_admin(current_user):
        abort(403)
    from flask import Response

    department_name = get_active_department(current_user)
    students_query = User.query.filter_by(department=department_name, role='student')

    year_filter_list = [y for y in request.args.get('years', '').split(',') if y]
    section_filter_list = [s for s in request.args.get('sections', '').split(',') if s]

    if year_filter_list:
        students_query = students_query.filter(User.year.in_(year_filter_list))
    if section_filter_list:
        students_query = students_query.filter(User.section.in_(section_filter_list))

    import re

    def roll_number_sort_key(student):
        if not student.roll_number:
            return (1, 0, '')
        match = re.search(r'(\d+)$', student.roll_number)
        if match:
            return (0, int(match.group(1)), student.roll_number)
        return (0, 0, student.roll_number)

    students = sorted(
        students_query.all(),
        key=lambda s: (s.year or '', s.section or '', roll_number_sort_key(s))
    )

    csv_data = _build_students_csv(students, columns=_get_requested_columns())
    filename = f"{department_name}_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        csv_data.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/hod/students/export/pdf')
@login_required
def export_hod_students_pdf():
    if not is_department_admin(current_user):
        abort(403)
    from io import BytesIO
    from flask import send_file
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from itertools import groupby

    department_name = get_active_department(current_user)
    students_query = User.query.filter_by(department=department_name, role='student')

    year_filter_list = [y for y in request.args.get('years', '').split(',') if y]
    section_filter_list = [s for s in request.args.get('sections', '').split(',') if s]

    if year_filter_list:
        students_query = students_query.filter(User.year.in_(year_filter_list))
    if section_filter_list:
        students_query = students_query.filter(User.section.in_(section_filter_list))

    import re

    def roll_number_sort_key(student):
        if not student.roll_number:
            return (1, 0, '')
        match = re.search(r'(\d+)$', student.roll_number)
        if match:
            return (0, int(match.group(1)), student.roll_number)
        return (0, 0, student.roll_number)

    students = sorted(
        students_query.all(),
        key=lambda s: (s.year or '', s.section or '', roll_number_sort_key(s))
    )

    selected = _resolve_export_columns(_get_requested_columns(), STUDENT_EXPORT_COLUMNS)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"{department_name} - Students by Year/Section", styles['Title']))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%d-%m-%Y %H:%M')} UTC &nbsp;&nbsp; "
        f"Total: {len(students)}",
        styles['Normal']
    ))
    story.append(Spacer(1, 16))

    def group_key(s):
        return (s.year or 'Unassigned', s.section or '-')

    for (year, section), group in groupby(students, key=group_key):
        group_list = list(group)
        story.append(Paragraph(f"Year {year}, Section {section} &nbsp; ({len(group_list)} students)", styles['Heading2']))
        story.append(Spacer(1, 7))

        row_dicts = [_student_row_dict(s) for s in group_list]
        table = _build_pdf_table(selected, row_dicts, doc.width)
        story.append(table)
        story.append(Spacer(1, 16))

    doc.build(story)
    buffer.seek(0)

    filename = f"{department_name}_students_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')
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
        StudentStaffAssignment.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        db.session.flush()
        
        # Delete all notifications related to this student
        Notification.query.filter_by(user_id=student.id).delete(synchronize_session=False)
        
        # Delete all comments by this student
        Comment.query.filter_by(user_id=student.id).delete(synchronize_session=False)
        
        # Delete all complaints by this student (already checked but just in case)
        Complaint.query.filter_by(user_id=student.id).delete(synchronize_session=False)
        db.session.flush()
        
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


# ========== MENTOR BULK STUDENT UPLOAD ROUTE ==========

@app.route('/mentor/students/upload', methods=['GET', 'POST'])
@login_required
def upload_students_csv():
    """Mentor bulk-uploads students into their own department via CSV."""
    if current_user.role != 'staff':
        abort(403)

    if request.method == 'GET':
        return render_template('upload_students_csv.html', department=current_user.department)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.', 'danger')
        return redirect(url_for('upload_students_csv'))

    if not file.filename.lower().endswith('.csv'):
        flash('Only .csv files are supported.', 'danger')
        return redirect(url_for('upload_students_csv'))

    try:
        rows, fieldnames = _parse_csv_stream(file)
    except Exception as e:
        flash(f'Could not read the CSV file: {e}', 'danger')
        return redirect(url_for('upload_students_csv'))

    if not rows:
        flash('The CSV file appears to be empty.', 'danger')
        return redirect(url_for('upload_students_csv'))

    result = _import_students(rows, current_user.department, DEFAULT_IMPORT_PASSWORD)

    if result['created']:
        flash(f"Imported {len(result['created'])} student(s) successfully.", 'success')
    if result['skipped']:
        flash(f"{len(result['skipped'])} row(s) skipped (already exist).", 'warning')
    if result['errors']:
        flash(f"{len(result['errors'])} row(s) had errors.", 'danger')

    return render_template('upload_students_csv.html',
                            department=current_user.department,
                            result=result)


# ========== COMPLAINT ROUTES ==========
@app.route('/api/suggest-category', methods=['POST'])
@login_required
def api_suggest_category():
    if current_user.role != 'student':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()

    if len(title) < 3 and len(description) < 10:
        return jsonify({'category': None})  # not enough text yet to bother guessing

    from ai_analytics import suggest_form_category
    category = suggest_form_category(title, description)
    return jsonify({'category': category})
@app.route('/complaint/new', methods=['GET', 'POST'])
@login_required
def new_complaint():
    if current_user.role not in ['student']:
        flash('Only students can create complaints.', 'warning')
        return redirect(url_for('dashboard'))
    
    form = ComplaintForm()
    assigned_mentor = get_primary_assigned_mentor(current_user)
    if assigned_mentor:
        form.mentor_id.choices = [(assigned_mentor.id, f"{assigned_mentor.username} ({assigned_mentor.email})")]
        form.mentor_id.data = assigned_mentor.id
    else:
        form.mentor_id.choices = [(0, '-- No Mentor Assigned --')]
        form.mentor_id.data = 0
    
    if form.validate_on_submit():
        mentor_id = assigned_mentor.id if assigned_mentor else None
        
        complaint = Complaint(
            complaint_id=generate_complaint_id(),
            title=form.title.data,
            description=form.description.data,
            category=form.category.data,
            priority=form.priority.data,
            user_id=current_user.id,
            mentor_id=mentor_id,
            assigned_to=mentor_id,
            deadline=compute_deadline(form.priority.data)
        )
        db.session.add(complaint)
        db.session.commit()

        # Real-time auto-response: replies immediately if the complaint matches a known pattern
        run_auto_responder(complaint)

        # Real-time AI categorization (Facility / Administration / Academic / etc.)
        try:
            classify_complaint(complaint)
        except Exception:
            app.logger.exception('AI classification failed for %s', complaint.complaint_id)

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
Grievance Hub
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

        # Real-time alert to super admin: which department currently has the most complaints overall
        top_dept_result = get_top_complaint_department()
        if top_dept_result:
            top_department, top_count = top_dept_result
            super_admins = [u for u in User.query.filter_by(role='admin').all() if is_super_admin(u)]
            for admin in super_admins:
                create_notification(
                    admin.id,
                    None,
                    f'"{top_department}" currently has the most complaints overall ({top_count} total).',
                    'department_alert'
                )
                send_email_notification(
                    admin.email,
                    'Department Complaint Alert - Grievance Hub',
                    f'''Dear {admin.username},

As of the latest complaint submission, "{top_department}" currently has the highest
number of total complaints across all departments ({top_count} complaints).

This is an automated real-time alert sent whenever a new complaint is filed.

Thank you,
Grievance Hub'''
                )

        flash('Your complaint has been submitted!', 'success')
        return redirect(url_for('view_complaints'))
    
    return render_template('create_complaint.html', form=form, assigned_mentor=assigned_mentor)


@app.route('/complaints')
@login_required
def view_complaints():
    search_query = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    status_filter = request.args.get('status', '')
    assigned_to_filter = request.args.get('assigned_to', '')
    department_filter = request.args.get('department', '')
    # ROLE BASED ACCESS
    if is_super_admin(current_user):
        query = Complaint.query.join(User, Complaint.user_id == User.id)

    elif is_department_admin(current_user):
        query = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == get_active_department(current_user),
            Complaint.is_overdue.is_(True)
        )

    elif current_user.role in ['staff', 'mentor']:
        query = Complaint.query.filter(
            (Complaint.assigned_to == current_user.id) | (Complaint.mentor_id == current_user.id)
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
        query = query.filter(Complaint.category == category_filter)

    ai_category_filter = request.args.get('ai_category', '')
    if ai_category_filter:
        query = query.filter(Complaint.ai_category == ai_category_filter)

    if status_filter:
        query = query.filter(Complaint.status == status_filter)

    if department_filter and is_super_admin(current_user):
        query = query.filter(User.department == department_filter)

    from sqlalchemy import case

    priority_order = case(
        (Complaint.priority == 'high', 1),
        (Complaint.priority == 'medium', 2),
        (Complaint.priority == 'low', 3),
        else_=4
    )
    complaints = query.order_by(priority_order, Complaint.created_at.desc()).all()

    categories = [
        ('academic', 'Academic'),
        ('administrative', 'Administrative'),
        ('facility', 'Facility'),
        ('harassment', 'Harassment'),
        ('technical', 'Technical'),
        ('other', 'Other')
    ]
    statuses = ['pending', 'in_progress', 'resolved', 'rejected']
    departments = Department.query.order_by(Department.name).all() if is_super_admin(current_user) else []

    return render_template(
        'view_complaints.html',
        complaints=complaints,
        categories=categories,
        statuses=statuses,
        search_query=search_query,
        category_filter=category_filter,
        status_filter=status_filter,
        assigned_to_filter=assigned_to_filter,
        departments=departments,
        department_filter=department_filter
    )
# Canonical list of exportable complaint fields: (key, column label).
COMPLAINT_EXPORT_COLUMNS = [
    ('complaint_id', 'Complaint ID'),
    ('student_name', 'Student Name'),
    ('title', 'Title'),
    ('description', 'Issue / Description'),
    ('category', 'Category'),
    ('status', 'Status'),
    ('priority', 'Priority'),
    ('action_taken', 'Action Taken'),
    ('phone', 'Student Phone'),
    ('parent_name', 'Parent Name'),
    ('parent_phone', 'Parent Phone'),
    ('created_at', 'Created'),
]

# Columns included when the user hasn't picked any via the column picker
# (e.g. a bare URL hit with no ?columns= param) — matches the original
# default export shape so nothing breaks for existing links/bookmarks.
DEFAULT_COMPLAINT_EXPORT_COLUMNS = [
    'complaint_id', 'title', 'category', 'status', 'priority', 'action_taken', 'created_at'
]


def _complaint_row_dict(c):
    author = c.author
    return {
        'complaint_id': c.complaint_id,
        'student_name': author.username if author else '',
        'title': c.title,
        'description': c.description or '',
        'category': c.category,
        'status': c.status.replace('_', ' ').title(),
        'priority': c.priority.title(),
        'action_taken': c.action_taken or '',
        'phone': (author.phone if author else '') or '',
        'parent_name': (author.parent_name if author else '') or '',
        'parent_phone': (author.parent_phone if author else '') or '',
        'created_at': c.created_at.strftime('%d-%m-%Y %H:%M'),
    }


def _resolve_complaint_columns(requested_keys):
    """Like _resolve_export_columns, but falls back to the original default
    column set (not every field) when nothing was explicitly requested."""
    base = requested_keys if requested_keys else DEFAULT_COMPLAINT_EXPORT_COLUMNS
    requested_set = set(base)
    filtered = [(key, label) for key, label in COMPLAINT_EXPORT_COLUMNS if key in requested_set]
    return filtered if filtered else COMPLAINT_EXPORT_COLUMNS


def _scope_complaints_query(search_query, category_filter, status_filter):
    """Role-based base query shared by both complaint export routes."""
    if is_super_admin(current_user):
        query = Complaint.query
    elif is_department_admin(current_user):
        query = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == get_active_department(current_user),
            Complaint.is_overdue.is_(True)
        )
    elif current_user.role in ['staff', 'mentor']:
        query = Complaint.query.filter(
            (Complaint.assigned_to == current_user.id) | (Complaint.mentor_id == current_user.id)
        )
    else:
        query = Complaint.query.filter_by(user_id=current_user.id)

    if search_query:
        query = query.filter(
            Complaint.complaint_id.ilike(f'%{search_query}%') |
            Complaint.title.ilike(f'%{search_query}%')
        )
    if category_filter:
        query = query.filter_by(category=category_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    return query


@app.route('/complaints/export/pdf')
@login_required
def export_complaints_pdf():
    from io import BytesIO
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from flask import send_file

    search_query = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    status_filter = request.args.get('status', '')

    query = _scope_complaints_query(search_query, category_filter, status_filter)

    selected_ids = request.args.getlist('ids')
    if selected_ids:
        id_list = [int(i) for i in selected_ids if i.isdigit()]
        if id_list:
            query = query.filter(Complaint.id.in_(id_list))

    complaints = query.order_by(Complaint.created_at.desc()).all()
    selected_columns = _resolve_complaint_columns(_get_requested_columns())

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Complaints Report", styles['Title']))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%d-%m-%Y %H:%M')} UTC &nbsp;&nbsp; "
        f"Total: {len(complaints)}",
        styles['Normal']
    ))
    story.append(Spacer(1, 12))

    def capped_row(c):
        # Word-wrapping cells handle normal-length text fine; only cap
        # pathologically long free-text fields so a single row can't blow
        # up to an unreadable page-and-a-half of height.
        row = _complaint_row_dict(c)
        for key in ('title', 'description', 'action_taken'):
            if row.get(key) and len(row[key]) > 300:
                row[key] = row[key][:300] + '...'
        return row

    row_dicts = [capped_row(c) for c in complaints]
    table = _build_pdf_table(selected_columns, row_dicts, doc.width)
    story.append(table)

    doc.build(story)
    buffer.seek(0)

    filename = f"complaints_export_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')
@app.route('/complaints/export/csv')
@login_required
def export_complaints():
    from flask import Response
    import csv
    from io import StringIO
 
    search_query = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    status_filter = request.args.get('status', '')

    query = _scope_complaints_query(search_query, category_filter, status_filter)

    selected_ids = request.args.getlist('ids')
    if selected_ids:
        id_list = [int(i) for i in selected_ids if i.isdigit()]
        if id_list:
            query = query.filter(Complaint.id.in_(id_list))
 
    complaints = query.order_by(Complaint.created_at.desc()).all()
    selected_columns = _resolve_complaint_columns(_get_requested_columns())
 
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([label for key, label in selected_columns])
 
    for c in complaints:
        row = _complaint_row_dict(c)
        writer.writerow([row[key] for key, label in selected_columns])
 
    output.seek(0)
    filename = f"complaints_export_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
 
@app.route('/complaint/<int:complaint_id>', methods=['GET', 'POST'])
@login_required

def complaint_details(complaint_id):
    complaint = get_complaint_or_404(complaint_id)
    
    if not can_view_complaint(current_user, complaint):
        abort(403)
    
    comment_form = CommentForm()
    update_form = UpdateComplaintForm()
    
    # Pre-fill action_taken in the update form if present
    try:
        update_form.action_taken.data = complaint.action_taken
    except Exception:
        # If DB doesn't have the column yet, ignore and continue
        update_form.action_taken.data = None
    
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
        complaint.action_taken = request.form.get('action_taken', '').strip() or None
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
            notify_merged_duplicate_authors(complaint, old_status)
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
    notify_merged_duplicate_authors(complaint, old_status)
    
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
    if current_user.role not in ['staff', 'mentor']:
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
    notify_merged_duplicate_authors(complaint, old_status) 
    return jsonify({'success': True})

@app.route('/duplicates')
@login_required
def review_duplicates():
    """Human review queue for complaints the automation flagged as possible
    duplicates. Nothing is ever auto-merged — a person confirms or dismisses
    each one here."""
    if current_user.role == 'student':
        abort(403)

    accessible = get_user_accessible_complaints(current_user)
    accessible_ids = {c.id for c in accessible}

    pending_review = Complaint.query.filter(
        Complaint.is_possible_duplicate.is_(True),
        Complaint.duplicate_reviewed.is_(False),
    ).all()
    pending_review = [c for c in pending_review if c.id in accessible_ids]

    return render_template('review_duplicates.html', pairs=pending_review)


@app.route('/duplicates/<int:complaint_id>/confirm', methods=['POST'])
@login_required
def confirm_duplicate(complaint_id):
    if current_user.role == 'student':
        abort(403)
    complaint = Complaint.query.get_or_404(complaint_id)
    complaint.duplicate_reviewed = True
    complaint.status = 'rejected'
    note = f"Marked as duplicate of {complaint.duplicate_of.complaint_id if complaint.duplicate_of else 'another complaint'} by {current_user.username}."
    complaint.action_taken = (complaint.action_taken + '\n' + note) if complaint.action_taken else note
    db.session.commit()
    flash(f'{complaint.complaint_id} marked as a duplicate and closed.', 'success')
    return redirect(url_for('review_duplicates'))


@app.route('/duplicates/<int:complaint_id>/dismiss', methods=['POST'])
@login_required
def dismiss_duplicate(complaint_id):
    if current_user.role == 'student':
        abort(403)
    complaint = Complaint.query.get_or_404(complaint_id)
    complaint.duplicate_reviewed = True
    db.session.commit()
    flash(f'{complaint.complaint_id} dismissed as not a duplicate.', 'info')
    return redirect(url_for('review_duplicates'))
# ========== USER MANAGEMENT ROUTES ==========

@app.route('/department/users')
@login_required
def department_users():
    if not is_department_admin(current_user):
        abort(403)

    department_name = get_active_department(current_user)

    users = User.query.filter_by(department=department_name).all()
    staff = get_department_staff(department_name)

    # Distinct sections and years in this department, for the filter dropdowns
    all_dept_students = get_department_students(department_name)
    sections = sorted({s.section for s in all_dept_students if s.section})
    years = sorted({s.year for s in all_dept_students if s.year})

    # No "All Sections" option -- default to the first section if none chosen
    section_filter = request.args.get('section', '') or (sections[0] if sections else '')
    # Year filter DOES have an "All Years" option -- empty string means no year filter applied
    year_filter = request.args.get('year', '')

    import re

    def roll_number_sort_key(student):
        # Extract trailing digits from roll_number (e.g. "ES24AD117" -> 117)
        # so sorting is numeric, not alphabetical.
        if not student.roll_number:
            return (1, 0, '')  # push blanks to the end
        match = re.search(r'(\d+)$', student.roll_number)
        if match:
            return (0, int(match.group(1)), student.roll_number)
        return (0, 0, student.roll_number)

    students_query = User.query.filter_by(department=department_name, role='student')
    if section_filter:
        students_query = students_query.filter_by(section=section_filter)
    if year_filter:
        students_query = students_query.filter_by(year=year_filter)
    students = sorted(students_query.all(), key=roll_number_sort_key)
    # Look up actual mentor from StudentStaffAssignment (mentor_name field is unused)
    student_mentors = {}
    for student in students:
        mentor = get_primary_assigned_mentor(student)
        if mentor:
            student_mentors[student.id] = mentor.username
    
    return render_template('department_users.html', 
                         users=users,
                         students=students,
                         staff=staff,
                         department=department_name,
                         student_mentors=student_mentors,
                         sections=sections,
                         section_filter=section_filter,
                         years=years,
                         year_filter=year_filter)
@app.route('/department/user/<int:user_id>/change-role/<string:role>')
@login_required
def department_change_user_role(user_id, role):
    if not is_department_admin(current_user):
        abort(403)
    
    user = User.query.get_or_404(user_id)

    accessible_dept_names = {d.name for d in get_hod_departments(current_user)}
    if user.department not in accessible_dept_names:
        abort(403)
    
    if user.id == current_user.id:
        flash('You cannot change your own role!', 'danger')
        return redirect(url_for('department_users'))
    
    if role not in ['student', 'staff', 'hod']:
        flash('Invalid role specified!', 'danger')
        return redirect(url_for('department_users'))

    dept = Department.query.filter_by(name=user.department).first()

    if role == 'hod':
        # Only one HOD per department: demote whoever currently holds it (if anyone).
        if dept and dept.hod_id and dept.hod_id != user.id:
            existing_hod = User.query.get(dept.hod_id)
            if existing_hod:
                existing_hod.role = 'staff'
                flash(f'"{existing_hod.username}" was demoted from HOD to Staff.', 'info')
        if dept:
            dept.hod_id = user.id
    else:
        # If this user was this department's HOD and is being moved to another
        # role, clear the department's hod_id so it doesn't point at a non-HOD user.
        if dept and dept.hod_id == user.id:
            dept.hod_id = None
    
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

    accessible_dept_names = {d.name for d in get_hod_departments(current_user)}
    if user.department not in accessible_dept_names:
        abort(403)
    
    if user.id == current_user.id:
        flash('You cannot delete your own account!', 'danger')
        return redirect(url_for('department_users'))
    
    if user.role == 'hod':
        flash('You cannot delete another HOD!', 'danger')
        return redirect(url_for('department_users'))
    
    try:
        # Delete all student-staff assignments for this user
        StudentStaffAssignment.query.filter_by(student_id=user.id).delete(synchronize_session=False)
        StudentStaffAssignment.query.filter_by(staff_id=user.id).delete(synchronize_session=False)
        db.session.flush()
        
        # Delete all notifications related to this user
        Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Comment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Complaint.query.filter_by(assigned_to=user.id).update({'assigned_to': None}, synchronize_session=False)
        Complaint.query.filter_by(mentor_id=user.id).update({'mentor_id': None}, synchronize_session=False)
        Complaint.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.flush()
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

def _compute_staff_stats(staff_members):
    """Build the assigned/resolved/performance stats dict list for a list of staff Users."""
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
    return staff_stats


def _build_staff_csv(staff_stats):
    import csv
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Phone', 'Assigned Complaints', 'Resolved', 'Performance (%)'])
    for s in staff_stats:
        writer.writerow([
            s['user'].username,
            s['user'].email,
            s['user'].phone or '',
            s['assigned'],
            s['resolved'],
            s['performance']
        ])
    output.seek(0)
    return output


def _build_staff_pdf(staff_stats, title):
    from io import BytesIO
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(title, styles['Title']))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%d-%m-%Y %H:%M')} UTC &nbsp;&nbsp; "
        f"Total: {len(staff_stats)}",
        styles['Normal']
    ))
    story.append(Spacer(1, 12))

    staff_columns = [
        ('username', 'Name'), ('email', 'Email'), ('phone', 'Phone'),
        ('assigned', 'Assigned'), ('resolved', 'Resolved'), ('performance', 'Performance'),
    ]
    row_dicts = []
    for s in staff_stats:
        row_dicts.append({
            'username': s['user'].username,
            'email': s['user'].email,
            'phone': s['user'].phone or '-',
            'assigned': str(s['assigned']),
            'resolved': str(s['resolved']),
            'performance': f"{s['performance']}%",
        })

    table = _build_pdf_table(staff_columns, row_dicts, doc.width)
    story.append(table)

    doc.build(story)
    buffer.seek(0)
    return buffer


@app.route('/department/manage-staff')
@login_required
def manage_staff():
    if not is_department_admin(current_user):
        abort(403)
    
    department_name = get_active_department(current_user)
    staff_members = User.query.filter_by(department=department_name, role='staff').all()
    staff_stats = _compute_staff_stats(staff_members)
    
    return render_template('manage_staff.html', staff_stats=staff_stats, department=department_name)


@app.route('/department/staff/export/csv')
@login_required
def export_department_staff_csv():
    """Export staff in the HOD's active department as CSV. Supports ?ids=1&ids=2... for
    exporting only the selected rows; falls back to exporting all staff otherwise."""
    if not is_department_admin(current_user):
        abort(403)
    from flask import Response

    department_name = get_active_department(current_user)
    staff_members = User.query.filter_by(department=department_name, role='staff').all()

    selected_ids = request.args.getlist('ids')
    if selected_ids:
        id_list = {int(i) for i in selected_ids if i.isdigit()}
        if id_list:
            staff_members = [s for s in staff_members if s.id in id_list]

    staff_stats = _compute_staff_stats(staff_members)
    csv_data = _build_staff_csv(staff_stats)
    filename = f"staff_{department_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        csv_data.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/department/staff/export/pdf')
@login_required
def export_department_staff_pdf():
    """Export staff in the HOD's active department as PDF. Supports ?ids=1&ids=2... for
    exporting only the selected rows; falls back to exporting all staff otherwise."""
    if not is_department_admin(current_user):
        abort(403)
    from flask import send_file

    department_name = get_active_department(current_user)
    staff_members = User.query.filter_by(department=department_name, role='staff').all()

    selected_ids = request.args.getlist('ids')
    if selected_ids:
        id_list = {int(i) for i in selected_ids if i.isdigit()}
        if id_list:
            staff_members = [s for s in staff_members if s.id in id_list]

    staff_stats = _compute_staff_stats(staff_members)
    buffer = _build_staff_pdf(staff_stats, f"Staff - {department_name}")
    filename = f"staff_{department_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/department/staff/<int:staff_id>/delete', methods=['POST'])
@login_required
def delete_staff(staff_id):
    if not is_department_admin(current_user):
        abort(403)
    
    staff = User.query.get_or_404(staff_id)

    accessible_dept_names = {d.name for d in get_hod_departments(current_user)}
    if staff.department not in accessible_dept_names:
        abort(403)
    
    if staff.role != 'staff':
        flash('Can only delete staff members', 'danger')
        return redirect(url_for('manage_staff'))
    
    try:
        StudentStaffAssignment.query.filter_by(student_id=staff.id).delete(synchronize_session=False)
        StudentStaffAssignment.query.filter_by(staff_id=staff.id).delete(synchronize_session=False)
        db.session.flush()
        Notification.query.filter_by(user_id=staff.id).delete(synchronize_session=False)
        Comment.query.filter_by(user_id=staff.id).delete(synchronize_session=False)
        Complaint.query.filter_by(assigned_to=staff.id).update({'assigned_to': None}, synchronize_session=False)
        Complaint.query.filter_by(mentor_id=staff.id).update({'mentor_id': None}, synchronize_session=False)
        Complaint.query.filter_by(user_id=staff.id).delete(synchronize_session=False)
        db.session.flush()
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

    department_name = get_active_department(current_user)
    
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
            department=department_name
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
Department: {department_name}

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
        
        flash(f'Staff/Mentor "{username}" added to {department_name} department!', 'success')
        return redirect(url_for('department_users'))
    
    return render_template('add_department_staff.html', department=department_name)


# ========== HOD BULK STAFF UPLOAD ROUTE ==========

@app.route('/department/staff/upload', methods=['GET', 'POST'])
@login_required
def upload_staff_csv():
    """HOD bulk-uploads staff into their active department via CSV."""
    if not is_department_admin(current_user):
        abort(403)

    department_name = get_active_department(current_user)

    if request.method == 'GET':
        return render_template('upload_staff_csv.html', department=department_name)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.', 'danger')
        return redirect(url_for('upload_staff_csv'))

    if not file.filename.lower().endswith('.csv'):
        flash('Only .csv files are supported.', 'danger')
        return redirect(url_for('upload_staff_csv'))

    try:
        rows, fieldnames = _parse_csv_stream(file)
    except Exception as e:
        flash(f'Could not read the CSV file: {e}', 'danger')
        return redirect(url_for('upload_staff_csv'))

    if not rows:
        flash('The CSV file appears to be empty.', 'danger')
        return redirect(url_for('upload_staff_csv'))

    result = _import_staff(rows, department_name, DEFAULT_IMPORT_PASSWORD)

    if result['created']:
        flash(f"Imported {len(result['created'])} staff member(s) successfully.", 'success')
    if result['skipped']:
        flash(f"{len(result['skipped'])} row(s) skipped (already exist).", 'warning')
    if result['errors']:
        flash(f"{len(result['errors'])} row(s) had errors.", 'danger')

    return render_template('upload_staff_csv.html',
                            department=department_name,
                            result=result)


# ========== CSV BULK IMPORT ==========

BULK_IMPORT_REQUIRED_COLUMNS = {'username', 'email', 'role'}
BULK_IMPORT_STUDENT_COLUMNS = ['roll_number', 'year', 'section', 'phone', 'parent_name', 'parent_phone', 'address']


@app.route('/bulk-import/template/<string:role>')
@login_required
def bulk_import_template(role):
    """Download a sample CSV template for bulk import."""
    if not (is_super_admin(current_user) or is_department_admin(current_user)):
        abort(403)

    if role not in ('student', 'staff'):
        abort(404)

    output = io.StringIO()
    writer = csv.writer(output)

    base_columns = ['username', 'email', 'role']
    if is_super_admin(current_user):
        base_columns.append('department')

    if role == 'student':
        columns = base_columns + BULK_IMPORT_STUDENT_COLUMNS
        sample = ['jane_doe', 'jane.doe@example.com', 'student']
        if is_super_admin(current_user):
            sample.append('Computer Science')
        sample += ['CS2024001', '2nd Year', 'A', '9876543210', 'John Doe', '9876543211', '123 Main St']
    else:
        columns = base_columns
        sample = ['staff_smith', 'staff.smith@example.com', 'staff']
        if is_super_admin(current_user):
            sample.append('Computer Science')

    writer.writerow(columns)
    writer.writerow(sample)

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename={role}_import_template.csv'
    return response


@app.route('/bulk-import', methods=['GET', 'POST'])
@login_required
def bulk_import():
    """Bulk import students and/or staff from a CSV file."""
    if not (is_super_admin(current_user) or is_department_admin(current_user)):
        abort(403)

    if request.method == 'GET':
        departments = Department.query.order_by(Department.name).all() if is_super_admin(current_user) else []
        return render_template('bulk_import.html', departments=departments)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.', 'danger')
        return redirect(url_for('bulk_import'))

    if not file.filename.lower().endswith('.csv'):
        flash('Only .csv files are supported.', 'danger')
        return redirect(url_for('bulk_import'))

    try:
        stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
        reader = csv.DictReader(stream)
    except Exception as e:
        flash(f'Could not read the CSV file: {e}', 'danger')
        return redirect(url_for('bulk_import'))

    if reader.fieldnames is None:
        flash('The CSV file appears to be empty.', 'danger')
        return redirect(url_for('bulk_import'))

    headers = {h.strip().lower() for h in reader.fieldnames if h}
    missing_columns = BULK_IMPORT_REQUIRED_COLUMNS - headers
    if missing_columns:
        flash(f'CSV is missing required column(s): {", ".join(sorted(missing_columns))}', 'danger')
        return redirect(url_for('bulk_import'))

    created_users = []
    errors = []
    seen_emails = set()
    seen_usernames = set()
    seen_roll_numbers = set()

    departments_by_name = {d.name: d for d in Department.query.all()}

    for row_num, raw_row in enumerate(reader, start=2):  # header is row 1
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in raw_row.items()}

        username = row.get('username')
        email = row.get('email')
        role = row.get('role', '').lower()

        row_label = username or email or f'row {row_num}'

        if not username or not email or not role:
            errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Missing username, email, or role'})
            continue

        if role not in ('student', 'staff'):
            errors.append({'row': row_num, 'identifier': row_label, 'reason': f"Role must be 'student' or 'staff', got '{role}'"})
            continue

        # Determine department
        if is_super_admin(current_user):
            department_name = row.get('department', '').strip()
            if not department_name:
                errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Department is required'})
                continue
            if department_name not in departments_by_name:
                errors.append({'row': row_num, 'identifier': row_label, 'reason': f"Unknown department '{department_name}'"})
                continue
        else:
            department_name = current_user.department

        # Duplicate checks (within file)
        if email.lower() in seen_emails:
            errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Duplicate email within this file'})
            continue
        # Duplicate checks (against database) — username is a display name only
        # (login uses email), so duplicate usernames across students are allowed.
        if User.query.filter_by(email=email).first():
            errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Email already registered'})
            continue

        roll_number = row.get('roll_number') or None

        # Guard against DB column-length overflows crashing the whole import
        field_limits = {
            'username': 80, 'email': 120, 'phone': 20, 'parent_phone': 50,
            'roll_number': 20, 'department': 100, 'year': 20, 'section': 10,
        }
        overflow = None
        for field_name, max_len in field_limits.items():
            value = row.get(field_name) or (roll_number if field_name == 'roll_number' else None)
            if value and len(value) > max_len:
                overflow = f"{field_name} is too long ({len(value)} chars, max {max_len})"
                break
        if overflow:
            errors.append({'row': row_num, 'identifier': row_label, 'reason': overflow})
            continue
        if role == 'student' and roll_number:
            if roll_number.lower() in seen_roll_numbers:
                errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Duplicate roll number within this file'})
                continue
            if User.query.filter_by(roll_number=roll_number).first():
                errors.append({'row': row_num, 'identifier': row_label, 'reason': 'Roll number already exists'})
                continue

        hashed_password = generate_password_hash(generate_random_password())

        user = User(
            username=username,
            email=email,
            password=hashed_password,
            role=role,
            department=department_name,
        )

        if role == 'student':
            user.roll_number = roll_number
            user.year = row.get('year') or None
            user.section = row.get('section') or None
            user.phone = row.get('phone') or None
            user.parent_name = row.get('parent_name') or None
            user.parent_phone = row.get('parent_phone') or None
            user.address = row.get('address') or None

        seen_emails.add(email.lower())
        if roll_number:
            seen_roll_numbers.add(roll_number.lower())

        db.session.add(user)
        created_users.append(user)

    if created_users:
        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            flash(f'Import failed due to a database conflict: {e}', 'danger')
            return redirect(url_for('bulk_import'))

        # Best-effort welcome emails; failures here don't roll back the import.
        emailed = 0
        for user in created_users:
            try:
                sent = (
                    send_csv_imported_student_email(user)
                    if user.role == 'student'
                    else send_csv_imported_staff_email(user)
                )
                if sent:
                    emailed += 1
            except Exception as e:
                print(f"❌ Error emailing imported user {user.username}: {e}")

        flash(
            f'Imported {len(created_users)} user(s) successfully. '
            f'Welcome emails sent to {emailed}/{len(created_users)}.',
            'success'
        )
    else:
        flash('No users were imported.', 'warning')

    return render_template('bulk_import_results.html', created_users=created_users, errors=errors)


@app.route('/assign-students')
@login_required
def assign_students():
    """HOD can assign students to department staff members."""
    if not is_department_admin(current_user):
        abort(403)

    department_name = get_active_department(current_user)
    form = StudentStaffAssignmentForm(department_name)

    staff_members = User.query.filter_by(department=department_name, role='staff').all()
    staff_assignments = {}
    for staff in staff_members:
        assignments = StudentStaffAssignment.query.filter_by(
            staff_id=staff.id,
            department=department_name
        ).all()
        if assignments:
            staff_assignments[staff.id] = {
                'staff': staff,
                'students': [
                    {'student': assignment.student, 'assignment': assignment}
                    for assignment in assignments
                    if assignment.student
                ]
            }

    return render_template('assign_students.html', form=form, staff_assignments=staff_assignments)


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
            assigned_student_ids = get_assigned_student_ids(current_user.department)
            created_assignments = 0
            skipped_students = []

            # Add assignments
            for student_id in student_ids:
                student = User.query.filter_by(id=student_id, department=current_user.department, role='student').first()
                if not student:
                    continue

                if student.id in assigned_student_ids:
                    skipped_students.append(student.username)
                    continue

                existing = StudentStaffAssignment.query.filter_by(
                    student_id=student.id,
                    department=current_user.department
                ).first()

                if existing:
                    skipped_students.append(student.username)
                    continue

                assignment = StudentStaffAssignment(
                    student_id=student.id,
                    staff_id=current_user.id,
                    department=current_user.department,
                    notes=notes
                )
                db.session.add(assignment)
                assigned_student_ids.add(student.id)
                created_assignments += 1

            db.session.commit()
            if created_assignments > 0:
                if skipped_students:
                    flash(f'Successfully assigned {created_assignments} student(s) to yourself. {len(skipped_students)} already-assigned student(s) were skipped.', 'warning')
                else:
                    flash(f'Successfully assigned {created_assignments} student(s) to yourself', 'success')
            elif skipped_students:
                flash('The selected students were already assigned and were not added.', 'warning')
            else:
                flash('No valid students were selected for assignment.', 'warning')

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

    department_name = get_active_department(current_user)
    form = StudentStaffAssignmentForm(department_name)
    
    if form.validate_on_submit():
        staff_id = form.staff_member.data
        student_ids = form.students.data
        notes = form.notes.data
        
        staff = User.query.filter_by(id=staff_id, department=department_name, role='staff').first()
        if not staff:
            flash('Selected staff member not found or not in your department', 'danger')
            return redirect(url_for('assign_students'))
        
        try:
            assigned_student_ids = get_assigned_student_ids(department_name)
            created_assignments = 0
            skipped_students = []

            # Add assignments
            for student_id in student_ids:
                student = User.query.filter_by(id=student_id, department=department_name, role='student').first()
                if not student:
                    continue

                if student.id in assigned_student_ids:
                    skipped_students.append(student.username)
                    continue

                existing = StudentStaffAssignment.query.filter_by(
                    student_id=student.id,
                    department=department_name
                ).first()

                if existing:
                    skipped_students.append(student.username)
                    continue

                assignment = StudentStaffAssignment(
                    student_id=student.id,
                    staff_id=staff_id,
                    department=department_name,
                    notes=notes
                )
                db.session.add(assignment)
                assigned_student_ids.add(student.id)
                created_assignments += 1

            db.session.commit()
            if created_assignments > 0:
                if skipped_students:
                    flash(f'Successfully assigned {created_assignments} student(s) to {staff.username}. {len(skipped_students)} already-assigned student(s) were skipped.', 'warning')
                else:
                    flash(f'Successfully assigned {created_assignments} student(s) to {staff.username}', 'success')
            elif skipped_students:
                flash('The selected students were already assigned and were not added.', 'warning')
            else:
                flash('No valid students were selected for assignment.', 'warning')

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


@app.route('/remove-student-assignments/bulk', methods=['POST'])
@login_required
def remove_student_assignments_bulk():
    """Remove multiple student assignments at once (checkbox-selected),
    instead of one-by-one. Same permission rules as the single-remove route,
    applied per assignment — any assignment the caller isn't allowed to touch
    is silently skipped rather than aborting the whole batch."""
    assignment_ids = request.form.getlist('assignment_ids')
    id_list = [int(i) for i in assignment_ids if i.isdigit()]

    if not id_list:
        flash('No students were selected to remove.', 'warning')
    else:
        assignments = StudentStaffAssignment.query.filter(StudentStaffAssignment.id.in_(id_list)).all()

        removed_count = 0
        skipped_count = 0

        for assignment in assignments:
            allowed = False
            if is_super_admin(current_user):
                allowed = True
            elif is_department_admin(current_user):
                allowed = assignment.department in {d.name for d in get_hod_departments(current_user)}
            elif current_user.role in ['staff', 'mentor']:
                allowed = assignment.staff_id == current_user.id

            if not allowed:
                skipped_count += 1
                continue

            try:
                db.session.delete(assignment)
                removed_count += 1
            except Exception:
                skipped_count += 1

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'Error removing assignments: {str(e)}', 'danger')
            removed_count = 0

        if removed_count:
            flash(f'Successfully removed {removed_count} student assignment(s).', 'success')
        if skipped_count:
            flash(f'{skipped_count} assignment(s) were skipped (not permitted or not found).', 'warning')

    # Redirect based on user role
    if is_department_admin(current_user):
        return redirect(url_for('assign_students'))
    elif current_user.role in ['staff', 'mentor']:
        return redirect(url_for('mentor_students'))
    else:
        return redirect(url_for('dashboard'))


# ========== SUPER ADMIN USER MANAGEMENT ==========

@app.route('/admin/users')
@login_required
def super_admin_users():
    if not is_super_admin(current_user):
        abort(403)

    import re

    def roll_number_sort_key(student):
        if not student.roll_number:
            return (1, 0, '')
        match = re.search(r'(\d+)$', student.roll_number)
        if match:
            return (0, int(match.group(1)), student.roll_number)
        return (0, 0, student.roll_number)

    departments = Department.query.order_by(Department.name).all()
    department_filter = request.args.get('department', '') or (departments[0].name if departments else '')
    year_filter = request.args.get('year', '')
    section_filter = request.args.get('section', '')

    all_users_query = User.query
    if department_filter:
        all_users_query = all_users_query.filter_by(department=department_filter)
    all_users = all_users_query.order_by(User.id).all()

    # Year/section options are scoped to the currently selected department,
    # so the dropdowns only ever show values that actually exist there.
    years = sorted({u.year for u in all_users if u.role == 'student' and u.year})
    sections = sorted({u.section for u in all_users if u.role == 'student' and u.section})

    students = sorted(
        [u for u in all_users if u.role == 'student'],
        key=roll_number_sort_key
    )
    if year_filter:
        students = [s for s in students if s.year == year_filter]
    if section_filter:
        students = [s for s in students if s.section == section_filter]

    staff = [u for u in all_users if u.role in ('staff', 'mentor')]
    others = [u for u in all_users if u.role not in ('student', 'staff', 'mentor')]

    # These users are being shown because their home department matches the
    # current filter — the department to display for them is just their own.
    for u in others:
        u.display_department = u.department
        u.is_additional_department = False

    # A HOD's home department (User.department) can differ from a department
    # they've been granted additional access to via HODDepartment (multi-department
    # HOD support). Surface those HODs in the Admin/HOD tab too, so this page
    # matches what Manage Departments shows as the assigned HOD for this department.
    # For these, show the FILTERED department (why they're in this list), not
    # their home department, and flag them so the template can label it clearly.
    if department_filter:
        filter_dept = Department.query.filter_by(name=department_filter).first()
        if filter_dept:
            linked_hod_ids = {
                link.user_id for link in HODDepartment.query.filter_by(department_id=filter_dept.id).all()
            }
            already_included_ids = {u.id for u in others}
            extra_hod_ids = linked_hod_ids - already_included_ids
            if extra_hod_ids:
                extra_hods = User.query.filter(User.id.in_(extra_hod_ids), User.role == 'hod').all()
                for hod in extra_hods:
                    hod.display_department = department_filter
                    hod.is_additional_department = True
                others.extend(extra_hods)

    complaint_counts = dict(
        db.session.query(Complaint.user_id, func.count(Complaint.id))
        .group_by(Complaint.user_id)
        .all()
    )

    departments = Department.query.order_by(Department.name).all()

    return render_template(
        'super_admin_users.html',
        users=all_users,
        students=students,
        staff=staff,
        others=others,
        complaint_counts=complaint_counts,
        departments=departments,
        department_filter=department_filter,
        years=years,
        year_filter=year_filter,
        sections=sections,
        section_filter=section_filter
    )

@app.route('/admin/manage-users')
@login_required
def manage_all_users():
    """Simple flat list of every user in the system (super admin only).
    Complements super_admin_users (which is grouped/filterable by department)
    with a single unfiltered table view."""
    if not is_super_admin(current_user):
        abort(403)

    users = User.query.order_by(User.id).all()
    return render_template('manage_users.html', users=users)


@app.route('/admin/user/<int:user_id>/hod-departments', methods=['GET', 'POST'])
@login_required
def manage_hod_departments(user_id):
    """Super admin controls which departments a HOD has access to (beyond
    the single 'official' hod_id shown on Manage Departments). This is what
    lets one person manage multiple departments with a single login."""
    if not is_super_admin(current_user):
        abort(403)

    hod_user = User.query.get_or_404(user_id)
    if hod_user.role != 'hod':
        flash('Only HOD accounts can be given department access. Change this user\'s role to HOD first.', 'danger')
        return redirect(url_for('manage_all_users'))

    all_departments = Department.query.order_by(Department.name).all()

    if request.method == 'POST':
        selected_ids = {int(i) for i in request.form.getlist('department_ids') if i.isdigit()}
        current_links = HODDepartment.query.filter_by(user_id=hod_user.id).all()
        current_ids = {link.department_id for link in current_links}

        # Remove access that was unchecked
        for link in current_links:
            if link.department_id not in selected_ids:
                db.session.delete(link)

        # Add access that was newly checked
        for dept_id in selected_ids - current_ids:
            db.session.add(HODDepartment(user_id=hod_user.id, department_id=dept_id))

        db.session.commit()
        flash(f'Department access updated for "{hod_user.username}".', 'success')
        return redirect(url_for('manage_hod_departments', user_id=hod_user.id))

    current_dept_ids = {
        link.department_id for link in HODDepartment.query.filter_by(user_id=hod_user.id).all()
    }
    return render_template(
        'manage_hod_departments.html',
        hod_user=hod_user,
        all_departments=all_departments,
        current_dept_ids=current_dept_ids
    )


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

    dept = Department.query.filter_by(name=user.department).first()

    if role == 'hod':
        # Only one HOD per department: demote whoever currently holds it (if anyone).
        if dept and dept.hod_id and dept.hod_id != user.id:
            existing_hod = User.query.get(dept.hod_id)
            if existing_hod:
                existing_hod.role = 'staff'
                flash(f'"{existing_hod.username}" was demoted from HOD to Staff.', 'info')
        if dept:
            dept.hod_id = user.id
    else:
        # If this user was their department's HOD and is being moved to another
        # role, clear the department's hod_id so it doesn't point at a non-HOD user.
        if dept and dept.hod_id == user.id:
            dept.hod_id = None
    
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
        chosen_hod_id = form.hod_id.data if form.hod_id.data != 0 else None

        department = Department(
            name=form.name.data,
            hod_id=chosen_hod_id
        )
        db.session.add(department)

        if chosen_hod_id:
            chosen_hod = User.query.get(chosen_hod_id)
            if chosen_hod:
                chosen_hod.role = 'hod'

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
    form = DepartmentForm(current_hod_id=department.hod_id)
    
    if form.validate_on_submit():
        new_hod_id = form.hod_id.data if form.hod_id.data != 0 else None
        old_hod_id = department.hod_id

        department.name = form.name.data
        department.hod_id = new_hod_id

        if old_hod_id and old_hod_id != new_hod_id:
            old_hod = User.query.get(old_hod_id)
            if old_hod:
                old_hod.role = 'staff'

        if new_hod_id:
            new_hod = User.query.get(new_hod_id)
            if new_hod:
                new_hod.role = 'hod'

        db.session.commit()
        flash(f'Department updated successfully!', 'success')
        return redirect(url_for('manage_departments'))
    
    form.name.data = department.name
    if department.hod_id:
        form.hod_id.data = department.hod_id
    
    return render_template('edit_department.html', form=form, department=department)

@app.route('/admin/hods/upload', methods=['GET', 'POST'])
@login_required
def upload_hods_csv():
    """Super admin bulk-uploads HODs via CSV. If an email already belongs to
    an existing HOD, that department is linked to their account instead of
    creating a duplicate — this is how one person ends up managing multiple
    departments with a single login."""
    if not is_super_admin(current_user):
        abort(403)

    if request.method == 'GET':
        departments = Department.query.order_by(Department.name).all()
        return render_template('upload_hods_csv.html', departments=departments)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.', 'danger')
        return redirect(url_for('upload_hods_csv'))

    if not file.filename.lower().endswith('.csv'):
        flash('Only .csv files are supported.', 'danger')
        return redirect(url_for('upload_hods_csv'))

    try:
        rows, fieldnames = _parse_csv_stream(file)
    except Exception as e:
        flash(f'Could not read the CSV file: {e}', 'danger')
        return redirect(url_for('upload_hods_csv'))

    if not rows:
        flash('The CSV file appears to be empty.', 'danger')
        return redirect(url_for('upload_hods_csv'))

    result = _import_hods(rows, DEFAULT_IMPORT_PASSWORD)

    if result['created']:
        flash(f"Created {len(result['created'])} new HOD account(s).", 'success')
    if result['linked']:
        flash(f"Granted {len(result['linked'])} additional department(s) to existing HOD(s).", 'success')
    if result['skipped']:
        flash(f"{len(result['skipped'])} row(s) skipped.", 'warning')
    if result['errors']:
        flash(f"{len(result['errors'])} row(s) had errors.", 'danger')

    departments = Department.query.order_by(Department.name).all()
    return render_template('upload_hods_csv.html', departments=departments, result=result)


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

@app.route('/analytics')
@login_required
def analytics_board():
    if not (is_super_admin(current_user) or is_department_admin(current_user)):
        abort(403)

    categories = ['academic', 'administrative', 'facility', 'harassment', 'technical', 'other']
    category_labels = {
        'academic': 'Academic',
        'administrative': 'Administrative',
        'facility': 'Facility',
        'harassment': 'Harassment',
        'technical': 'Technical',
        'other': 'Other'
    }
    statuses = ['pending', 'in_progress', 'resolved', 'rejected']

    if is_super_admin(current_user):
        base_query = Complaint.query
        scope_label = 'All Departments'
    else:
        active_department = get_active_department(current_user)
        base_query = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == active_department
        )
        scope_label = active_department

    category_totals = []
    status_breakdown = {status: [] for status in statuses}

    for cat in categories:
        cat_complaints = base_query.filter(Complaint.category == cat).all()
        category_totals.append(len(cat_complaints))
        for status in statuses:
            status_breakdown[status].append(
                len([c for c in cat_complaints if c.status == status])
            )

    total_complaints = sum(category_totals)

    # Needed by the template (it was previously undefined -> broke |tojson)
    departments = Department.query.order_by(Department.name).all() if is_super_admin(current_user) else []

    return render_template(
        'analytics_board.html',
        category_labels=[category_labels[c] for c in categories],
        category_totals=category_totals,
        status_breakdown=status_breakdown,
        statuses=statuses,
        total_complaints=total_complaints,
        scope_label=scope_label,
        is_super_admin_view=is_super_admin(current_user),
        departments=departments,
    )
# ========== NOTIFICATION & PROFILE ROUTES ==========

@app.route('/notifications')
@login_required
def view_notifications():
    category_filter = request.args.get('category', '')

    query = Notification.query.filter_by(user_id=current_user.id)
    if category_filter:
        query = query.join(Complaint, Notification.complaint_id == Complaint.id).filter(Complaint.category == category_filter)

    notifications = query.order_by(Notification.created_at.desc()).all()
    for notification in notifications:
        notification.is_read = True
    db.session.commit()

    categories = [
        ('academic', 'Academic'),
        ('administrative', 'Administrative'),
        ('facility', 'Facility'),
        ('harassment', 'Harassment'),
        ('technical', 'Technical'),
        ('other', 'Other')
    ]

    return render_template('notifications.html', notifications=notifications, categories=categories, category_filter=category_filter)
@app.route('/notification/<int:notification_id>/delete', methods=['POST'])
@login_required
def delete_notification(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id != current_user.id:
        abort(403)
    db.session.delete(notification)
    db.session.commit()
    flash('Notification deleted.', 'success')
    return redirect(url_for('view_notifications', category=request.args.get('category', '')))


@app.route('/notifications/delete-all', methods=['POST'])
@login_required
def delete_all_notifications():
    category_filter = request.args.get('category', '')
    query = Notification.query.filter_by(user_id=current_user.id)
    if category_filter:
        query = query.join(Complaint, Notification.complaint_id == Complaint.id).filter(Complaint.category == category_filter)
    deleted_count = query.delete(synchronize_session=False)
    db.session.commit()
    flash(f'Deleted {deleted_count} notification(s).', 'success')
    return redirect(url_for('view_notifications'))
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
        # Update roll number if present
        current_user.roll_number = form.roll_number.data.strip() if form.roll_number.data else None
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
        # Update roll number if provided
        student.roll_number = form.roll_number.data.strip() if form.roll_number.data else None
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

@app.route('/fix-hod-roles')
@login_required
def fix_hod_roles():
    """One-time data fix: some departments' hod_id was set (via the old
    add/edit department form) without ever updating that user's role to
    'hod', so Manage Departments and Manage Users disagreed on who's HOD.
    This walks every department and corrects it. Safe to run repeatedly."""
    if not is_super_admin(current_user):
        return "Only admin can access this", 403

    try:
        departments = Department.query.filter(Department.hod_id.isnot(None)).all()
        fixed = []

        for dept in departments:
            hod_user = User.query.get(dept.hod_id)
            if hod_user and hod_user.role != 'hod':
                old_role = hod_user.role
                hod_user.role = 'hod'
                fixed.append(f"{hod_user.username} ({dept.name}): {old_role} → hod")

        db.session.commit()

        result = "<html><body><h2>Fixing HOD Roles...</h2>"
        if fixed:
            result += f"<p>Corrected {len(fixed)} user(s):</p><ul>"
            for line in fixed:
                result += f"<li>{line}</li>"
            result += "</ul>"
        else:
            result += "<p>No mismatches found — everything is already in sync.</p>"
        result += "<h2 style='color:green'>✅ Done!</h2>"
        result += "<a href='/admin/manage-users'>Go to Manage Users</a> | <a href='/admin/departments'>Go to Manage Departments</a></body></html>"
        return result
    except Exception as e:
        db.session.rollback()
        return f"<h2 style='color:red'>Error: {str(e)}</h2>"


@app.route('/fix-complaint-ids-randomize')
@login_required
def fix_complaint_ids_randomize():
    """One-time migration: reassigns every EXISTING complaint a random,
    non-sequential ID (see generate_complaint_id() in utils.py), so old
    complaints stop leaking the total complaint count. Safe to run more
    than once."""
    if not is_super_admin(current_user):
        return "Only admin can access this", 403

    try:
        complaints = Complaint.query.order_by(Complaint.created_at).all()
        result = "<html><body><h2>Randomizing Complaint IDs...</h2>"
        result += f"<p>Found {len(complaints)} complaints</p>"
        fixed_count = 0

        for complaint in complaints:
            old_id = complaint.complaint_id
            new_id = generate_complaint_id()
            if old_id != new_id:
                complaint.complaint_id = new_id
                result += f"<p>Changed: {old_id} → {new_id}</p>"
                fixed_count += 1

        db.session.commit()
        result += f"<h2 style='color:green'>✅ Randomized {fixed_count} complaint ID(s)!</h2>"
        result += "<a href='/complaints'>Go to My Complaints</a></body></html>"
        return result
    except Exception as e:
        db.session.rollback()
        return f"<h2 style='color:red'>Error: {str(e)}</h2>"


@app.route('/fix-complaint-ids')
@login_required
def fix_complaint_ids():
    """DISABLED: reverts complaint IDs back to sequential ESEC01/ESEC02/...
    form, which leaks the total complaint count. Use
    /fix-complaint-ids-randomize instead."""
    if not is_super_admin(current_user):
        return "Only admin can access this", 403
    return (
        "<html><body><h2>This tool has been disabled</h2>"
        "<p>Sequential IDs leak the total complaint count. Use "
        "<a href='/fix-complaint-ids-randomize'>/fix-complaint-ids-randomize</a> instead.</p>"
        "</body></html>"
    )


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

@app.route('/api/analytics/complaints')
@login_required
def api_complaint_analytics():
    """Analytics for the board: fixed-category breakdown (Academic/Administrative/
    Facility/Harassment/Technical/Other, from the complaint form), AI-predicted
    category breakdown, status/priority breakdown, monthly trend (last 6 months),
    and auto-generated insights.
    Query params:
      ?days=30        - limit to last N days (does not affect the 6-month trend)
      ?department=X   - (super admin only) drill into one department
    """
    if not (is_super_admin(current_user) or is_department_admin(current_user)):
        abort(403)

    from collections import Counter
    from dateutil.relativedelta import relativedelta

    days = request.args.get('days', type=int)
    year_filter = request.args.get('year') or None
    section_filter = request.args.get('section') or None
    department = None

    if is_department_admin(current_user) and not is_super_admin(current_user):
        department = get_active_department(current_user)
    elif is_super_admin(current_user):
        department = request.args.get('department') or None

    base_query = Complaint.query.join(User, Complaint.user_id == User.id)
    if department:
        base_query = base_query.filter(User.department == department)
    if year_filter:
        base_query = base_query.filter(User.year == year_filter)
    if section_filter:
        base_query = base_query.filter(User.section == section_filter)

    scoped_query = base_query
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        scoped_query = scoped_query.filter(Complaint.created_at >= since)

    complaints = scoped_query.all()

    FORM_CATEGORIES = ['academic', 'administrative', 'facility', 'harassment', 'technical', 'other']
    FORM_CATEGORY_LABELS = {
        'academic': 'Academic',
        'administrative': 'Administrative',
        'facility': 'Facility',
        'harassment': 'Harassment',
        'technical': 'Technical',
        'other': 'Other'
    }

    category_counts = Counter(c.category for c in complaints)
    by_category = {FORM_CATEGORY_LABELS[cat]: category_counts.get(cat, 0) for cat in FORM_CATEGORIES}
    by_ai_category = dict(Counter(c.ai_category or 'Uncategorized' for c in complaints))
    by_status = dict(Counter(c.status for c in complaints))
    
    by_priority = dict(Counter(c.priority for c in complaints))

    total = len(complaints)
    resolved = by_status.get('resolved', 0)
    resolution_rate = round((resolved / total * 100) if total else 0, 1)

    # ----- Operational health metrics -----
    overdue_count = sum(1 for c in complaints if c.is_overdue)
    escalated_count = sum(1 for c in complaints if c.escalation_level and c.escalation_level > 0)
    duplicate_merged_count = sum(1 for c in complaints if c.is_duplicate_of is not None)

    resolved_complaints_list = [c for c in complaints if c.status == 'resolved']
    if resolved_complaints_list:
        total_hours = sum(
            (c.updated_at - c.created_at).total_seconds() / 3600
            for c in resolved_complaints_list
        )
        avg_resolution_hours = round(total_hours / len(resolved_complaints_list), 1)
    else:
        avg_resolution_hours = 0

    complaints_with_deadline_resolved = [c for c in resolved_complaints_list if c.deadline]
    if complaints_with_deadline_resolved:
        met_sla = sum(1 for c in complaints_with_deadline_resolved if c.updated_at <= c.deadline)
        sla_compliance_rate = round((met_sla / len(complaints_with_deadline_resolved)) * 100, 1)
    else:
        sla_compliance_rate = None  # no resolved complaints with a deadline yet to judge SLA against
        # ----- Urgent attention: open high-priority complaints, oldest first -----
    urgent_query = base_query.filter(
        Complaint.priority == 'high',
        Complaint.status.in_(['pending', 'in_progress'])
    ).order_by(Complaint.created_at.asc()).limit(10)

    urgent_complaints = []
    for c in urgent_query.all():
        urgent_complaints.append({
            'id': c.id,
            'complaint_id': c.complaint_id,
            'title': c.title,
            'department': c.author.department if c.author else 'Unknown',
            'status': c.status,
            'created_at': c.created_at.strftime('%d-%m-%Y'),
            'is_overdue': bool(c.is_overdue)
        })

    # ----- Trend vs previous period of the same length -----
   
    compare_days = days or 30
    current_period_start = datetime.utcnow() - timedelta(days=compare_days)
    previous_period_start = current_period_start - timedelta(days=compare_days)

    current_period_count = base_query.filter(Complaint.created_at >= current_period_start).count()
    previous_period_count = base_query.filter(
        Complaint.created_at >= previous_period_start,
        Complaint.created_at < current_period_start
    ).count()

    if previous_period_count > 0:
        trend_change_pct = round(((current_period_count - previous_period_count) / previous_period_count) * 100, 1)
    elif current_period_count > 0:
        trend_change_pct = 100.0
    else:
        trend_change_pct = 0.0

    # Per-category pending count and resolution rate, for insights
    per_cat_pending = {}
    per_cat_resolution = {}
    per_cat_high_priority = {}
    for cat in FORM_CATEGORIES:
        cat_complaints = [c for c in complaints if c.category == cat]
        label = FORM_CATEGORY_LABELS[cat]
        per_cat_pending[label] = sum(1 for c in cat_complaints if c.status == 'pending')
        per_cat_resolution[label] = round(
            (sum(1 for c in cat_complaints if c.status == 'resolved') / len(cat_complaints) * 100)
            if cat_complaints else 0, 1
        )
        per_cat_high_priority[label] = sum(1 for c in cat_complaints if c.priority == 'high')

    # ----- Monthly trend: last 6 months (independent of the `days` filter) -----
    trend_query = Complaint.query.join(User, Complaint.user_id == User.id)
    if department:
        trend_query = trend_query.filter(User.department == department)
    if year_filter:
        trend_query = trend_query.filter(User.year == year_filter)
    if section_filter:
        trend_query = trend_query.filter(User.section == section_filter)

    six_months_ago = (datetime.utcnow() - relativedelta(months=5)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    trend_complaints = trend_query.filter(Complaint.created_at >= six_months_ago).all()

    month_labels = []
    month_keys = []
    cursor = six_months_ago
    for _ in range(6):
        month_labels.append(cursor.strftime('%b %Y'))
        month_keys.append(cursor.strftime('%Y-%m'))
        cursor = cursor + relativedelta(months=1)

    month_counts = Counter(c.created_at.strftime('%Y-%m') for c in trend_complaints)
    monthly_trend = [month_counts.get(k, 0) for k in month_keys]

    # ----- Auto-generated insights -----
    insights = []
    categories_with_data = {k: v for k, v in by_category.items() if v > 0}
    if categories_with_data:
        top_category = max(categories_with_data, key=categories_with_data.get)
        insights.append(f'<strong>{top_category}</strong> is the most frequently reported category ({categories_with_data[top_category]} complaints).')

        top_pending_cat = max(per_cat_pending, key=per_cat_pending.get)
        if per_cat_pending[top_pending_cat] > 0:
            insights.append(f'<strong>{top_pending_cat}</strong> has the highest number of pending complaints and may need attention.')

        cats_with_complaints = {k: v for k, v in per_cat_resolution.items() if by_category.get(k, 0) > 0}
        if cats_with_complaints:
            best_resolution_cat = max(cats_with_complaints, key=cats_with_complaints.get)
            insights.append(f'<strong>{best_resolution_cat}</strong> has the best resolution rate among categories with complaints.')

        top_priority_cat = max(per_cat_high_priority, key=per_cat_high_priority.get)
        if per_cat_high_priority[top_priority_cat] > 0:
            insights.append(f'<strong>{top_priority_cat}</strong> has the most high-priority complaints.')
    else:
        insights.append('No complaints in this scope yet.')

    scope_parts = [department or 'All Departments']
    if year_filter:
        scope_parts.append(year_filter)
    if section_filter:
        scope_parts.append(f'Section {section_filter}')

    return jsonify({
        'total': total,
        'resolved': resolved,
        'resolution_rate': resolution_rate,
        'by_category': by_category,
        'by_ai_category': by_ai_category,
        'by_status': by_status,
        'by_priority': by_priority,
        'category_resolution_rate': per_cat_resolution,
        'monthly_trend_labels': month_labels,
        'monthly_trend_values': monthly_trend,
        'insights': insights,
        'scope': department or 'All Departments',
        'overdue_count': overdue_count,
        'escalated_count': escalated_count,
        'duplicate_merged_count': duplicate_merged_count,
        'avg_resolution_hours': avg_resolution_hours,
        'sla_compliance_rate': sla_compliance_rate,
        'trend_change_pct': trend_change_pct,
        'current_period_count': current_period_count,
        'previous_period_count': previous_period_count,
        'urgent_complaints': urgent_complaints,
    })


@app.route('/api/analytics/departments')
@login_required
def api_department_analytics():
    """Super admin only: per-department totals and resolution rates, for
    the department-wise comparison chart on the analytics board."""
    if not is_super_admin(current_user):
        abort(403)

    days = request.args.get('days', type=int)
    departments = Department.query.order_by(Department.name).all()

    result = []
    for dept in departments:
        query = Complaint.query.join(User, Complaint.user_id == User.id).filter(User.department == dept.name)
        if days:
            since = datetime.utcnow() - timedelta(days=days)
            query = query.filter(Complaint.created_at >= since)
        complaints = query.all()
        total = len(complaints)
        resolved = sum(1 for c in complaints if c.status == 'resolved')
        result.append({
            'department': dept.name,
            'total': total,
            'resolved': resolved,
            'resolution_rate': round((resolved / total * 100) if total else 0, 1)
        })

    return jsonify(result)

def get_merge_original(complaint):
    """If this complaint was merged as a duplicate, return the original it was merged into."""
    if complaint.is_duplicate_of:
        return Complaint.query.get(complaint.is_duplicate_of)
    return None


def get_duplicate_count(complaint):
    """How many other complaints were merged into this one."""
    return Complaint.query.filter_by(is_duplicate_of=complaint.id).count()

@app.route('/api/analytics/staff-performance')
@login_required
def api_staff_performance():
    """Top staff/mentors ranked by resolved complaints, scoped to the caller's
    department (HOD) or a selected/all departments (super admin)."""
    if not (is_super_admin(current_user) or is_department_admin(current_user)):
        abort(403)

    department = None
    if is_department_admin(current_user) and not is_super_admin(current_user):
        department = get_active_department(current_user)
    elif is_super_admin(current_user):
        department = request.args.get('department') or None

    staff_query = User.query.filter(User.role.in_(['staff', 'mentor']))
    if department:
        staff_query = staff_query.filter(User.department == department)

    result = []
    for staff in staff_query.all():
        assigned = Complaint.query.filter(
            (Complaint.assigned_to == staff.id) | (Complaint.mentor_id == staff.id)
        ).all()
        if not assigned:
            continue
        resolved = [c for c in assigned if c.status == 'resolved']
        result.append({
            'name': staff.username,
            'department': staff.department,
            'assigned': len(assigned),
            'resolved': len(resolved),
            'resolution_rate': round((len(resolved) / len(assigned)) * 100, 1)
        })

    result.sort(key=lambda r: r['resolved'], reverse=True)
    return jsonify(result[:10])
@app.context_processor
def utility_processor():
    return {
        'is_super_admin': is_super_admin,
        'is_department_admin': is_department_admin,
        'can_manage_user': can_manage_user,
        'get_merge_original': get_merge_original,
        'get_duplicate_count': get_duplicate_count,
        'get_hod_departments': get_hod_departments,
        'get_active_department': get_active_department,
    }


if __name__ == '__main__':
    # Initialize/upgrade the database schema (adds missing columns to local SQLite) before starting
    try:
        initialize_database()
    except Exception as e:
        app.logger.exception('Database initialization failed at startup')
    app.run(debug=True)