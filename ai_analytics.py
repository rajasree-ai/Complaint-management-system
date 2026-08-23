"""
ai_analytics.py
================
AI-powered complaint categorization and analytics.

- classify_complaint(complaint) : calls OpenAI to tag a complaint with a category
  (Facility, Administration, Academic, etc.) - called in real-time right after a
  complaint is created (see app.py new_complaint route), with graceful fallback
  if the API key is missing or the call fails.
- classify_unclassified_complaints() : batch job (registered in automation.py) that
  catches any complaints that failed to classify at creation time (e.g. API hiccup).
- get_analytics_summary(...) : aggregates category/department/status data for the
  analytics dashboard / API endpoint.

Install:
    pip install openai

Setup:
    Set the OPENAI_API_KEY environment variable. Without it, classify_complaint()
    falls back to a simple keyword-based classifier so the app keeps working.
"""

import os
import logging
from datetime import datetime, timedelta
from collections import Counter

from database import db
from models import Complaint, User

logger = logging.getLogger('ai_analytics')

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
OPENAI_MODEL = os.environ.get('OPENAI_ANALYTICS_MODEL', 'gpt-4o-mini')

CATEGORIES = [
    'Facility',            # buildings, electricity, water, furniture, classrooms
    'Hostel & Mess',       # hostel rooms, food, mess-related
    'Administration',      # admin office, paperwork, procedures, staff conduct
    'Academic',            # exams, marks, results, teaching, curriculum
    'Fees & Finance',       # fees, refunds, scholarships, payments
    'Technology & IT',      # wifi, internet, portal, software, labs
    'Harassment & Ragging',  # harassment, ragging, bullying, safety
    'Transport',            # bus, transport, parking
    'Other',
]

# Lightweight fallback used when no OpenAI key is configured, or a call fails.
_FALLBACK_KEYWORDS = {
    'Facility': ['building', 'electricity', 'power', 'water', 'furniture', 'classroom', 'toilet', 'infrastructure'],
    'Hostel & Mess': ['hostel', 'room', 'mess', 'food', 'canteen', 'warden'],
    'Administration': ['admin', 'office', 'certificate', 'paperwork', 'procedure', 'staff behavior'],
    'Academic': ['exam', 'marks', 'grade', 'result', 'lecture', 'teaching', 'syllabus', 'attendance'],
    'Fees & Finance': ['fee', 'fees', 'payment', 'refund', 'scholarship', 'finance'],
    'Technology & IT': ['wifi', 'wi-fi', 'internet', 'network', 'portal', 'software', 'lab', 'computer'],
    'Harassment & Ragging': ['harassment', 'ragging', 'bully', 'bullying', 'unsafe', 'abuse'],
    'Transport': ['bus', 'transport', 'parking', 'shuttle'],
}


def _fallback_classify(text):
    text = text.lower()
    for category, keywords in _FALLBACK_KEYWORDS.items():
        if any(k in text for k in keywords):
            return category
    return 'Other'


def classify_complaint(complaint):
    """Classify a single complaint into one of CATEGORIES and save it.
    Uses OpenAI if OPENAI_API_KEY is set, otherwise a keyword fallback.
    Never raises - on any failure it stores 'Other' rather than blocking complaint creation.
    """
    text = f'{complaint.title}. {complaint.description}'

    category = None
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            'You classify student grievance complaints into exactly one category. '
                            f'Valid categories: {", ".join(CATEGORIES)}. '
                            'Reply with ONLY the category name, nothing else.'
                        ),
                    },
                    {'role': 'user', 'content': text[:2000]},
                ],
                max_tokens=10,
                temperature=0,
            )
            raw = response.choices[0].message.content.strip()
            # Match against known categories (case-insensitive, tolerant of minor formatting)
            for c in CATEGORIES:
                if c.lower() == raw.lower() or c.lower() in raw.lower():
                    category = c
                    break
        except Exception:
            logger.exception('[ai_analytics] OpenAI classification failed for %s, using fallback', complaint.complaint_id)

    if not category:
        category = _fallback_classify(text)

    complaint.ai_category = category
    db.session.commit()
    logger.info('[ai_analytics] classified %s as %s', complaint.complaint_id, category)
    return category


def classify_unclassified_complaints(batch_size=25):
    """Batch-classify any complaints that don't have an ai_category yet
    (e.g. classification failed at creation time, or ran before this feature existed)."""
    pending = Complaint.query.filter(Complaint.ai_category.is_(None)).limit(batch_size).all()
    for complaint in pending:
        classify_complaint(complaint)
    if pending:
        logger.info('[ai_analytics] backfilled classification for %s complaint(s)', len(pending))
    return len(pending)


def get_analytics_summary(department=None, days=None):
    """Aggregate complaint data for the analytics dashboard.

    Returns a dict with:
      - by_category: {category: count}
      - by_status: {status: count}
      - by_department: {department: count}
      - by_priority: {priority: count}
      - total, resolved, resolution_rate
      - category_resolution: {category: resolution_rate %}  (which issue types get resolved fastest/slowest)
    """
    query = Complaint.query.join(User, Complaint.user_id == User.id)
    if department:
        query = query.filter(User.department == department)
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        query = query.filter(Complaint.created_at >= since)

    complaints = query.all()

    by_category = Counter(c.ai_category or 'Uncategorized' for c in complaints)
    by_status = Counter(c.status for c in complaints)
    by_department = Counter((c.author.department or 'Unknown') for c in complaints)
    by_priority = Counter(c.priority for c in complaints)

    category_resolution = {}
    for category in set(by_category):
        cat_complaints = [c for c in complaints if (c.ai_category or 'Uncategorized') == category]
        resolved = sum(1 for c in cat_complaints if c.status == 'resolved')
        category_resolution[category] = round((resolved / len(cat_complaints) * 100) if cat_complaints else 0, 1)

    total = len(complaints)
    resolved = by_status.get('resolved', 0)

    return {
        'total': total,
        'resolved': resolved,
        'resolution_rate': round((resolved / total * 100) if total else 0, 1),
        'by_category': dict(by_category),
        'by_status': dict(by_status),
        'by_department': dict(by_department),
        'by_priority': dict(by_priority),
        'category_resolution_rate': category_resolution,
    }
 # Keywords mapped to your actual complaint-form categories (different from the
# richer CATEGORIES list above, which is only used for the analytics dashboard).
FORM_CATEGORIES = ['academic', 'administrative', 'facility', 'harassment', 'technical', 'other']

_FORM_CATEGORY_KEYWORDS = {
    'facility': ['charging port', 'charging', 'ac', 'air condition', 'electricity', 'power', 'water',
                 'furniture', 'classroom', 'toilet', 'infrastructure', 'building', 'fan', 'light'],
    'technical': ['wifi', 'wi-fi', 'internet', 'network', 'tower', 'signal', 'portal', 'software',
                  'lab', 'computer', 'system', 'projector'],
    'academic': ['exam', 'marks', 'grade', 'result', 'lecture', 'teaching', 'syllabus', 'attendance',
                 'assignment', 'project', 'faculty', 'professor', 'class schedule'],
    'administrative': ['admin', 'office', 'certificate', 'paperwork', 'procedure', 'fee', 'fees',
                        'payment', 'refund', 'scholarship', 'id card', 'bonafide'],
    'harassment': ['harassment', 'ragging', 'bully', 'bullying', 'unsafe', 'abuse', 'threat'],
}


def suggest_form_category(title, description):
    """Suggest one of the 6 manual complaint-form categories in real time, as the
    student types. Tries OpenAI first (same key as classify_complaint), falls back
    to keyword matching. Always returns a valid category, never raises."""
    text = f'{title} {description}'.lower()

    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            'Classify this student complaint into exactly one category. '
                            f'Valid categories: {", ".join(FORM_CATEGORIES)}. '
                            'Reply with ONLY the category name in lowercase, nothing else.'
                        ),
                    },
                    {'role': 'user', 'content': text[:1000]},
                ],
                max_tokens=6,
                temperature=0,
            )
            raw = response.choices[0].message.content.strip().lower()
            for c in FORM_CATEGORIES:
                if c == raw or c in raw:
                    return c
        except Exception:
            logger.info('[ai_analytics] suggest_form_category: OpenAI unavailable, using keyword fallback')

    for category, keywords in _FORM_CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return category
    return 'other'