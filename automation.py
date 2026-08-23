"""
automation.py
==============
Background automation for the Grievance Hub.

Implements:
- Escalation          : every 2 days   -> escalates stale complaints to HOD, then Principal/Admin
- Assignment           : every 24 hours -> assigns unassigned complaints to a staff member
- Duplicate detection  : every 12 hours -> flags likely-duplicate complaints for review
- Weekly reports       : every Monday 9 AM -> emails each HOD a summary of their department
- Reminders            : every 6 hours  -> reminds assignees about near-deadline complaints
- Backup               : daily at midnight -> backs up the database
- Status update        : every 6 hours  -> marks complaints past their deadline as overdue
- Auto-response        : real-time -> replies to common complaint patterns as soon as they're filed

All scheduled jobs run inside the Flask app context (needed for SQLAlchemy).
Uses APScheduler's BackgroundScheduler, which runs inside the same process as Flask -
no separate worker or cron job needed.

Install:
    pip install apscheduler

Wiring (already added in app.py):
    from automation import init_automation, compute_deadline, run_auto_responder
    init_automation(app)
"""

import os
import shutil
import difflib
import logging
import subprocess
from datetime import datetime, timedelta
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import db
from models import Complaint, User, Department, Comment
from utils import create_notification, send_email_notification, calculate_complaint_stats
from ai_analytics import classify_unclassified_complaints

logger = logging.getLogger('automation')

# ---------------------------------------------------------------------------
# Configuration (override via environment variables if you want different SLAs)
# ---------------------------------------------------------------------------
DEADLINE_HOURS = {
    'high': int(os.environ.get('SLA_HIGH_HOURS', 24)),
    'medium': int(os.environ.get('SLA_MEDIUM_HOURS', 72)),
    'low': int(os.environ.get('SLA_LOW_HOURS', 120)),
}
ESCALATION_INTERVAL_DAYS = int(os.environ.get('ESCALATION_INTERVAL_DAYS', 2))
REMINDER_WINDOW_HOURS = int(os.environ.get('REMINDER_WINDOW_HOURS', 24))
DUPLICATE_SIMILARITY_THRESHOLD = float(os.environ.get('DUPLICATE_SIMILARITY_THRESHOLD', 0.75))
DUPLICATE_LOOKBACK_DAYS = int(os.environ.get('DUPLICATE_LOOKBACK_DAYS', 14))
BACKUP_DIR = os.environ.get('BACKUP_DIR', 'backups')
BACKUP_RETENTION = int(os.environ.get('BACKUP_RETENTION', 7))
OPEN_STATUSES = ('pending', 'in_progress')

# Simple keyword -> canned response map for the real-time auto-responder.
# Extend this dict as you learn what common complaint categories/patterns show up.
AUTO_RESPONSE_PATTERNS = [
    (
        ['wifi', 'wi-fi', 'internet', 'network'],
        "Thanks for reporting this connectivity issue. Our IT team has been notified "
        "and typically resolves network complaints within 24 hours. We'll update you here."
    ),
    (
        ['hostel', 'room', 'mess', 'food', 'canteen'],
        "Thanks for flagging this hostel/mess issue. It has been routed to the hostel "
        "warden for review. You'll be notified as soon as there's an update."
    ),
    (
        ['harassment', 'ragging', 'bully', 'bullying'],
        "Thank you for reporting this. Complaints of this nature are treated with the "
        "highest priority and confidentiality, and have been escalated directly to the "
        "department HOD for immediate attention."
    ),
    (
        ['fee', 'fees', 'payment', 'refund'],
        "Thanks for reaching out about this fee/payment concern. This has been forwarded "
        "to the accounts section, who will get back to you shortly."
    ),
    (
        ['exam', 'marks', 'grade', 'result'],
        "Thanks for your complaint regarding exam/results. This has been routed to the "
        "examination cell for review."
    ),
]

AUTOMATION_NOTIFICATION_TYPE = 'automation'


# ---------------------------------------------------------------------------
# Helpers shared across jobs
# ---------------------------------------------------------------------------

def compute_deadline(priority, from_time=None):
    """Compute an SLA deadline for a complaint based on its priority.
    Called when a complaint is created (see app.py new_complaint route)."""
    from_time = from_time or datetime.utcnow()
    hours = DEADLINE_HOURS.get((priority or 'medium').lower(), DEADLINE_HOURS['medium'])
    return from_time + timedelta(hours=hours)


def _is_super_admin(user):
    return user.role == 'admin' and user.email == 'vanitha.sty3375@gmail.com'


def _get_department_hod(department_name):
    dept = Department.query.filter_by(name=department_name).first()
    return User.query.get(dept.hod_id) if dept and dept.hod_id else None


def _get_super_admins():
    return User.query.filter_by(role='admin').all()


def _notify(user, complaint, message, ntype=AUTOMATION_NOTIFICATION_TYPE):
    if not user:
        return
    create_notification(user.id, complaint.id if complaint else None, message, ntype)


# ---------------------------------------------------------------------------
# 1. ESCALATION - every 2 days
# ---------------------------------------------------------------------------

def escalate_complaints():
    """Escalate complaints that have been open too long.
    Level 0 -> 1: escalate to the department HOD.
    Level 1 -> 2: escalate to the Principal / super admin.
    """
    threshold = datetime.utcnow() - timedelta(days=ESCALATION_INTERVAL_DAYS)
    stale = Complaint.query.filter(
        Complaint.status.in_(OPEN_STATUSES),
        Complaint.created_at <= threshold,
    ).all()

    escalated_count = 0
    for complaint in stale:
        last_action = complaint.last_escalated_at or complaint.created_at
        if last_action > threshold:
            continue  # not stale *since* the last escalation yet

        author = complaint.author
        if complaint.escalation_level == 0:
            hod = _get_department_hod(author.department) if author else None
            if hod:
                message = (
                    f"Complaint {complaint.complaint_id} ('{complaint.title}') has been open "
                    f"for {ESCALATION_INTERVAL_DAYS}+ days without resolution and is escalated to you."
                )
                _notify(hod, complaint, message)
                send_email_notification(
                    hod.email,
                    f'Escalation: Complaint {complaint.complaint_id} needs attention',
                    message,
                )
                complaint.escalation_level = 1
                complaint.last_escalated_at = datetime.utcnow()
                escalated_count += 1
        elif complaint.escalation_level == 1:
            for admin in _get_super_admins():
                message = (
                    f"Complaint {complaint.complaint_id} ('{complaint.title}') remains unresolved "
                    f"after HOD escalation and is now escalated to Principal/Admin level."
                )
                _notify(admin, complaint, message)
                send_email_notification(
                    admin.email,
                    f'Escalation: Complaint {complaint.complaint_id} needs Principal attention',
                    message,
                )
            complaint.escalation_level = 2
            complaint.last_escalated_at = datetime.utcnow()
            escalated_count += 1
        # level 2 = already at the top; no further escalation

    if escalated_count:
        db.session.commit()
    logger.info('[escalation] escalated %s complaint(s)', escalated_count)


# ---------------------------------------------------------------------------
# 2. ASSIGNMENT - every 24 hours
# ---------------------------------------------------------------------------

def assign_unassigned_complaints():
    """Assign any unassigned complaint to the least-loaded staff/mentor in that department."""
    unassigned = Complaint.query.filter(
        Complaint.assigned_to.is_(None),
        Complaint.status.in_(OPEN_STATUSES),
    ).all()

    assigned_count = 0
    for complaint in unassigned:
        author = complaint.author
        if not author or not author.department:
            continue

        staff_list = User.query.filter_by(department=author.department, role='staff').all()
        if not staff_list:
            continue  # nobody to assign to in this department

        # Load-balance: pick the staff member with the fewest currently-open complaints
        def open_load(staff):
            return Complaint.query.filter(
                Complaint.assigned_to == staff.id,
                Complaint.status.in_(OPEN_STATUSES),
            ).count()

        chosen = min(staff_list, key=open_load)
        complaint.assigned_to = chosen.id

        message = f'Complaint {complaint.complaint_id} was auto-assigned to you (unassigned for 24h+).'
        _notify(chosen, complaint, message)
        send_email_notification(
            chosen.email,
            f'Auto-Assigned: Complaint {complaint.complaint_id}',
            f'''Dear {chosen.username},

Complaint {complaint.complaint_id} ("{complaint.title}") had no assigned staff member and
has been automatically assigned to you.

Category: {complaint.category}
Priority: {complaint.priority}

Please review it at your earliest convenience.

Thank you,
Grievance Hub''',
        )
        assigned_count += 1

    if assigned_count:
        db.session.commit()
    logger.info('[assignment] auto-assigned %s complaint(s)', assigned_count)


# ---------------------------------------------------------------------------
# 3. DUPLICATE DETECTION & MERGE - every 12 hours
# ---------------------------------------------------------------------------

def detect_duplicate_complaints():
    """Detect and merge duplicate complaints (non-destructive - nothing is deleted).

    Two complaints are considered duplicates when they're in the same department
    and category, filed within DUPLICATE_LOOKBACK_DAYS of each other, and their
    title+description text is similar above DUPLICATE_SIMILARITY_THRESHOLD.
    This intentionally compares across DIFFERENT students too, since the most
    common real-world case is several students reporting the same facility issue
    (e.g. "AC broken in room 204") separately.

    On merge:
      - The newer complaint's comments are copied over to the original.
      - The newer complaint is marked resolved-as-duplicate (status stays within
        the app's existing status vocabulary; is_duplicate_of + action_taken
        record exactly what happened) and its author is notified.
      - The original complaint gets a note that another student reported the
        same issue, and its priority is bumped once 3+ reports stack up.
    """
    since = datetime.utcnow() - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
    candidates = Complaint.query.filter(
        Complaint.created_at >= since,
        Complaint.is_duplicate_of.is_(None),
    ).order_by(Complaint.created_at.asc()).all()

    # Group by (department, category) - the realistic unit of "same issue"
    groups = {}
    for c in candidates:
        dept = c.author.department if c.author else None
        groups.setdefault((dept, c.category), []).append(c)

    merged_count = 0
    for _, complaints in groups.items():
        if len(complaints) < 2:
            continue
        for i in range(1, len(complaints)):
            newer = complaints[i]
            if newer.is_duplicate_of:
                continue
            for j in range(i):
                older = complaints[j]
                if older.is_duplicate_of:  # don't chain onto something already merged
                    continue
                title_similarity = difflib.SequenceMatcher(None, older.title.lower(), newer.title.lower()).ratio()
                desc_similarity = difflib.SequenceMatcher(None, older.description.lower(), newer.description.lower()).ratio()
                # Weight title higher — students titling the same real-world issue tend to use
                # near-identical short titles even when their descriptions are phrased very differently.
                similarity = (title_similarity * 0.6) + (desc_similarity * 0.4)
                if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                    _merge_complaint(newer, older, similarity)
                    merged_count += 1
                    break  # stop comparing this "newer" against further originals

    if merged_count:
        db.session.commit()
    logger.info('[duplicates] merged %s duplicate complaint(s)', merged_count)


def _merge_complaint(duplicate, original, similarity):
    """Merge `duplicate` into `original`. Keeps both rows (soft-merge) but moves
    the conversation over and closes the duplicate so staff only work the original."""
    # Copy over any comments already on the duplicate, tagged with their author
    for comment in list(duplicate.comments):
        note = f'[Merged from duplicate complaint {duplicate.complaint_id} by {comment.user.username}]: {comment.content}'
        db.session.add(Comment(content=note, user_id=comment.user_id, complaint_id=original.id))

    # Record the merge itself as a comment on the original, from the duplicate's author
    db.session.add(Comment(
        content=(
            f'Another student ({duplicate.author.username if duplicate.author else "unknown"}) reported the '
            f'same issue as complaint {duplicate.complaint_id} ({similarity:.0%} similar) - merged here automatically.'
        ),
        user_id=duplicate.user_id,
        complaint_id=original.id,
    ))

    duplicate.is_duplicate_of = original.id
    duplicate.status = 'resolved'
    duplicate.action_taken = f'Automatically merged into complaint {original.complaint_id} as a duplicate report of the same issue.'

    # If enough students report the same issue, treat it as more urgent
    merge_count = Complaint.query.filter_by(is_duplicate_of=original.id).count()
    if merge_count >= 3 and original.priority != 'high':
        original.priority = 'high'
        original_note = f'{merge_count} students have now reported this issue - priority raised to High automatically.'
        db.session.add(Comment(content=original_note, user_id=original.user_id, complaint_id=original.id))
        assignee = User.query.get(original.assigned_to) if original.assigned_to else None
        _notify(assignee, original, original_note)

    # Notify the duplicate's author that their report was merged
    if duplicate.author:
        send_email_notification(
            duplicate.author.email,
            f'Complaint {duplicate.complaint_id} merged with an existing report',
            f'''Dear {duplicate.author.username},

Your complaint {duplicate.complaint_id} ("{duplicate.title}") matches an existing report
({original.complaint_id}) that's already being worked on, so we've merged them to avoid
duplicate effort. You'll continue to get updates - please track complaint {original.complaint_id}
going forward.

Thank you,
Grievance Hub''',
        )
        _notify(duplicate.author, duplicate, f'Your complaint was merged into {original.complaint_id} (same issue already reported).')


# ---------------------------------------------------------------------------
# 4. WEEKLY REPORTS - every Monday 9 AM
# ---------------------------------------------------------------------------

def send_weekly_reports():
    """Email each HOD a summary of their department's complaints for the past week."""
    week_ago = datetime.utcnow() - timedelta(days=7)
    departments = Department.query.filter(Department.hod_id.isnot(None)).all()

    sent_count = 0
    for dept in departments:
        hod = User.query.get(dept.hod_id)
        if not hod:
            continue

        dept_complaints = Complaint.query.join(User, Complaint.user_id == User.id).filter(
            User.department == dept.name
        ).all()
        weekly_complaints = [c for c in dept_complaints if c.created_at >= week_ago]

        stats_all_time = calculate_complaint_stats(dept_complaints)
        stats_week = calculate_complaint_stats(weekly_complaints)

        body = f'''Dear {hod.username},

Here is the weekly summary for the {dept.name} department.

This week ({week_ago.strftime('%d-%m-%Y')} to {datetime.utcnow().strftime('%d-%m-%Y')}):
  New complaints : {stats_week['total']}
  Pending        : {stats_week['pending']}
  In progress    : {stats_week['in_progress']}
  Resolved       : {stats_week['resolved']}
  Rejected       : {stats_week['rejected']}

All-time department totals:
  Total complaints  : {stats_all_time['total']}
  Resolution rate   : {stats_all_time['resolution_rate']}%

Please log in to the Grievance Hub dashboard for full details.

Thank you,
Grievance Hub'''

        send_email_notification(hod.email, f'Weekly Complaint Report - {dept.name}', body)
        sent_count += 1

    logger.info('[weekly-report] sent %s report(s)', sent_count)


# ---------------------------------------------------------------------------
# 5. REMINDERS - every 6 hours
# ---------------------------------------------------------------------------

def send_deadline_reminders():
    """Remind the assignee about complaints approaching their SLA deadline."""
    now = datetime.utcnow()
    window_end = now + timedelta(hours=REMINDER_WINDOW_HOURS)

    due_soon = Complaint.query.filter(
        Complaint.status.in_(OPEN_STATUSES),
        Complaint.deadline.isnot(None),
        Complaint.deadline <= window_end,
        Complaint.deadline >= now,
    ).all()

    reminded_count = 0
    for complaint in due_soon:
        if complaint.last_reminded_at and complaint.last_reminded_at >= now - timedelta(hours=6):
            continue  # already reminded this cycle

        recipient = User.query.get(complaint.assigned_to) if complaint.assigned_to else None
        if not recipient:
            continue

        hours_left = max(0, int((complaint.deadline - now).total_seconds() // 3600))
        message = f'Complaint {complaint.complaint_id} is due within {hours_left}h - please take action.'
        _notify(recipient, complaint, message)
        send_email_notification(
            recipient.email,
            f'Reminder: Complaint {complaint.complaint_id} due soon',
            f'''Dear {recipient.username},

Complaint {complaint.complaint_id} ("{complaint.title}") is approaching its deadline
({complaint.deadline.strftime('%d-%m-%Y %H:%M')} UTC, about {hours_left}h left).

Please review and update its status.

Thank you,
Grievance Hub''',
        )
        complaint.last_reminded_at = now
        reminded_count += 1

    if reminded_count:
        db.session.commit()
    logger.info('[reminders] sent %s reminder(s)', reminded_count)


# ---------------------------------------------------------------------------
# 6. BACKUP - daily at midnight
# ---------------------------------------------------------------------------

def backup_database():
    """Back up the database. Works for SQLite (file copy) and Postgres (pg_dump, if available)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    database_url = os.environ.get('DATABASE_URL') or 'sqlite:///grievance_hub.db'
    database_url = database_url.strip().strip('"').strip("'")
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    parsed = urlparse(database_url)

    try:
        if parsed.scheme.startswith('sqlite'):
            db_path = parsed.path.lstrip('/') if parsed.path else 'grievance_hub.db'
            if os.path.exists(db_path):
                dest = os.path.join(BACKUP_DIR, f'backup_{timestamp}.db')
                shutil.copy2(db_path, dest)
                logger.info('[backup] SQLite database backed up to %s', dest)
            else:
                logger.warning('[backup] SQLite file not found at %s', db_path)
        elif parsed.scheme.startswith('postgresql'):
            dest = os.path.join(BACKUP_DIR, f'backup_{timestamp}.sql')
            try:
                with open(dest, 'wb') as f:
                    subprocess.run(['pg_dump', database_url], stdout=f, check=True)
                logger.info('[backup] Postgres database backed up to %s', dest)
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                logger.warning('[backup] pg_dump unavailable/failed (%s). '
                                'Configure managed backups on your Postgres host instead.', e)
        else:
            logger.warning('[backup] Unsupported database scheme for backup: %s', parsed.scheme)

        # Retention: keep only the most recent N backups
        backups = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith('backup_')),
            reverse=True,
        )
        for old_file in backups[BACKUP_RETENTION:]:
            os.remove(os.path.join(BACKUP_DIR, old_file))
    except Exception:
        logger.exception('[backup] Database backup failed')


# ---------------------------------------------------------------------------
# 7. STATUS UPDATE (mark overdue) - every 6 hours
# ---------------------------------------------------------------------------

def mark_overdue_complaints():
    """Mark complaints past their SLA deadline as overdue (does not change `status`,
    only sets the `is_overdue` flag so the UI/reports can surface it)."""
    now = datetime.utcnow()
    overdue = Complaint.query.filter(
        Complaint.status.in_(OPEN_STATUSES),
        Complaint.deadline.isnot(None),
        Complaint.deadline < now,
        Complaint.is_overdue.is_(False),
    ).all()

    for complaint in overdue:
        complaint.is_overdue = True
        recipient = User.query.get(complaint.assigned_to) if complaint.assigned_to else complaint.author
        _notify(recipient, complaint, f'Complaint {complaint.complaint_id} is now overdue.')

    if overdue:
        db.session.commit()
    logger.info('[status-update] marked %s complaint(s) overdue', len(overdue))


# ---------------------------------------------------------------------------
# 8. AUTO-RESPONSE - real-time (called directly from the complaint-creation route)
# ---------------------------------------------------------------------------

def run_auto_responder(complaint):
    """Post an automatic canned reply if the complaint text matches a known pattern.
    Call this right after a complaint is created (see app.py new_complaint route)."""
    text = f'{complaint.title} {complaint.description}'.lower()
    for keywords, response in AUTO_RESPONSE_PATTERNS:
        if any(keyword in text for keyword in keywords):
            comment = Comment(content=response, user_id=complaint.user_id, complaint_id=complaint.id)
            db.session.add(comment)
            complaint.auto_response_sent = True
            db.session.commit()
            logger.info('[auto-response] replied to %s (matched %s)', complaint.complaint_id, keywords[0])
            return True
    return False


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------

def init_automation(app):
    """Register and start all scheduled automation jobs against the given Flask app.
    Safe to call once at startup (guards against double-start under the Flask reloader)."""
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' and app.debug:
        # In debug mode Flask's reloader spawns a second process; only the child
        # (WERKZEUG_RUN_MAIN=true) should actually run the scheduler.
        pass
    elif app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return None

    if getattr(app, '_automation_scheduler', None):
        return app._automation_scheduler  # already started

    scheduler = BackgroundScheduler(timezone='UTC')

    def _wrap(job_func):
        """Run a job inside the Flask app context so SQLAlchemy queries work."""
        def _runner():
            with app.app_context():
                try:
                    job_func()
                except Exception:
                    logger.exception('Automation job %s failed', job_func.__name__)
        return _runner

    scheduler.add_job(_wrap(escalate_complaints), IntervalTrigger(days=ESCALATION_INTERVAL_DAYS), id='escalation')
    scheduler.add_job(_wrap(assign_unassigned_complaints), IntervalTrigger(hours=24), id='assignment')
    scheduler.add_job(_wrap(detect_duplicate_complaints), IntervalTrigger(hours=12), id='duplicates')
    scheduler.add_job(_wrap(send_weekly_reports), CronTrigger(day_of_week='mon', hour=9, minute=0), id='weekly_reports')
    scheduler.add_job(_wrap(send_deadline_reminders), IntervalTrigger(hours=6), id='reminders')
    scheduler.add_job(_wrap(backup_database), CronTrigger(hour=0, minute=0), id='backup')
    scheduler.add_job(_wrap(mark_overdue_complaints), IntervalTrigger(hours=6), id='status_update')
    scheduler.add_job(_wrap(classify_unclassified_complaints), IntervalTrigger(minutes=30), id='ai_classification_backfill')

    scheduler.start()
    app._automation_scheduler = scheduler
    logger.info('Automation scheduler started with jobs: %s', [j.id for j in scheduler.get_jobs()])
    return scheduler