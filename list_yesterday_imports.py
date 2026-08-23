from app import app
from models import User
from datetime import datetime, timedelta, timezone

with app.app_context():
    # Your server stores created_at in UTC, but you are in India (IST = UTC+5:30).
    # "Yesterday" here means yesterday in IST, converted to the matching UTC range.
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(ist)
    yesterday_ist = (now_ist - timedelta(days=1)).date()

    start_ist = datetime.combine(yesterday_ist, datetime.min.time(), tzinfo=ist)
    end_ist = datetime.combine(yesterday_ist, datetime.max.time(), tzinfo=ist)

    start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_ist.astimezone(timezone.utc).replace(tzinfo=None)

    students = User.query.filter(
        User.role == 'student',
        User.created_at >= start_utc,
        User.created_at <= end_utc
    ).order_by(User.created_at).all()

    print(f"Students created on {yesterday_ist} (IST): {len(students)}")
    for s in students:
        print(f"  id={s.id} | {s.username} | {s.email} | roll={s.roll_number} | dept={s.department} | created={s.created_at}")
