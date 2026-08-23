from app import app, renumber_student_ids
from database import db
from models import User, StudentStaffAssignment, Notification, Comment, Complaint

STUDENT_IDS = list(range(566, 626))  # ids 566 to 625 inclusive

with app.app_context():
    students = User.query.filter(User.id.in_(STUDENT_IDS), User.role == 'student').all()
    print(f"Found {len(students)} matching students to delete")

    deleted_count = 0
    skipped = []

    for student in students:
        # Same safety check your app already uses: don't delete students who filed complaints
        if student.complaints:
            skipped.append(f"{student.username} (id={student.id}) has {len(student.complaints)} complaint(s)")
            continue

        try:
            StudentStaffAssignment.query.filter_by(student_id=student.id).delete(synchronize_session=False)
            Notification.query.filter_by(user_id=student.id).delete(synchronize_session=False)
            Comment.query.filter_by(user_id=student.id).delete(synchronize_session=False)
            db.session.flush()
            db.session.delete(student)
            db.session.commit()
            deleted_count += 1
        except Exception as e:
            db.session.rollback()
            skipped.append(f"{student.username} (id={student.id}) failed: {e}")

    print(f"Deleted {deleted_count} student(s)")
    if skipped:
        print(f"Skipped {len(skipped)} student(s):")
        for s in skipped:
            print(f"  - {s}")

    if deleted_count > 0:
        renumber_student_ids()
        print("Renumbered student IDs")
