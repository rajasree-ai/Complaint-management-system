from app import app
import automation

with app.app_context():
    automation.detect_duplicate_complaints()
