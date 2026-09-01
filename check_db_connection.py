from app import app, database_url
from models import User

print("Connected to:", database_url)

with app.app_context():
    total_users = User.query.count()
    aids_students = User.query.filter_by(
        department="Artificial Intelligence and Data Science",
        role="student"
    ).count()
    print(f"Total users in this database: {total_users}")
    print(f"AI&DS students in this database: {aids_students}")
