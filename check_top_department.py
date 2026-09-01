from app import app, get_top_complaint_department

with app.app_context():
    result = get_top_complaint_department()
    if result:
        dept, count = result
        print(f"Top department right now: {dept} ({count} complaints)")
    else:
        print("No complaints in the database yet")
