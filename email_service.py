import os
import requests

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
print("BREVO_API_KEY exists:", bool(os.getenv("BREVO_API_KEY")))
print("MAIL_DEFAULT_SENDER:", os.getenv("MAIL_DEFAULT_SENDER"))

def send_email(to_email: str, subject: str, html_content: str) -> bool:
    api_key = os.environ.get("BREVO_API_KEY")
    sender_email = os.environ.get("MAIL_DEFAULT_SENDER")

    if not api_key or not sender_email:
        print("❌ BREVO_API_KEY or MAIL_DEFAULT_SENDER environment variable is missing.")
        return False

    payload = {
        "sender": {"email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key,
    }

    try:
        response = requests.post(
            BREVO_API_URL,
            json=payload,
            headers=headers,
            timeout=15
        )

        print("Brevo status:", response.status_code)
        print("Brevo response:", response.text)

        return response.ok

    except Exception as exc:
        print("❌ Error sending email:", exc)
        return False