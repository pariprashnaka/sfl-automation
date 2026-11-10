import os, smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
load_dotenv(r"D:\vault\automation\.env")
u, pw, to = os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD"), os.getenv("EMAIL_TO")
m = MIMEText("Test from your laptop. If you can read this, the morning digest email will work.")
m["Subject"], m["From"], m["To"] = "Digest email test", u, to
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(u, pw)
    s.send_message(m)
print("Sent. Check", to)
