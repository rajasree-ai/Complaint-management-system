from app import app
from models import User, Department

with app.app_context():
    print("--- Department table entries ---")
    for d in Department.query.all():
        print(repr(d.name))

    print("--- Distinct department values on User rows (students only) ---")
    distinct_depts = set(
        u.department for u in User.query.filter_by(role="student").all()
    )
    for d in distinct_depts:
        print(repr(d))

    print("--- Exact filter_by test ---")
    target = "Artificial Intelligence and Data Science"
    count = User.query.filter_by(department=target, role="student").count()
    print(f"filter_by(department={target!r}, role=student).count() = {count}")
