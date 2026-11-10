import os, smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
load_dotenv(r"D:\vault\automation\.env")
u, pw, to = os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD"), os.getenv("EMAIL_TO")
print("user:", u, "| password length:", len(pw or ""), "| to:", to)
m = MIMEText("Test from your laptop. If you can read this, the morning digest email will work.")
m["Subject"], m["From"], m["To"] = "Digest email test", u, to
with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
    s.starttls()
    s.login(u, pw)
    s.send_message(m)
print("Sent. Check", to)
