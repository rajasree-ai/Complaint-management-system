from app import app
import automation
with app.app_context():
    automation.escalate_complaints()
