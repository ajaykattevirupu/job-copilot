"""
Gmail Monitor — watches your inbox for recruiter emails and auto-drafts replies.

How it works:
1. Connects to Gmail via IMAP (no browser needed)
2. Polls every N seconds for new unread emails
3. Detects recruiter/job-related emails using keywords
4. Drafts a reply using GPT and saves it to Gmail Drafts
5. You review the draft in Gmail and hit Send when ready

Setup:
1. Enable IMAP in Gmail Settings → See all settings → Forwarding and POP/IMAP
2. Create a Gmail App Password:
   Google Account → Security → 2-Step Verification → App Passwords
   (requires 2FA to be enabled)
3. Use that App Password here (NOT your regular Gmail password)
"""

import imaplib
import email
import email.utils
import os
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tailor import draft_email as ai_draft_email, check_relevance


# Keywords that suggest a recruiter / job opportunity email
RECRUITER_KEYWORDS = [
    "opportunity", "position", "role", "opening", "hiring", "recruiter",
    "talent", "candidate", "interview", "your background", "your profile",
    "java", "full stack", "software engineer", "developer", "spring boot",
    "excited to share", "reach out", "connect with you",
]

# Emails to ignore (job boards, automated notifications)
IGNORE_SENDERS = [
    "noreply", "no-reply", "notifications", "alerts", "donotreply",
    "linkedin.com", "indeed.com", "dice.com", "glassdoor.com",
    "monster.com", "ziprecruiter.com",
]


class EmailMonitor:

    def __init__(self, gmail_address: str, app_password: str, openai_client, resume: str):
        self.address    = gmail_address
        self.password   = app_password
        self.client     = openai_client
        self.resume     = resume
        self.imap       = None
        self.seen_ids   = set()   # track already-processed message IDs

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        """Connect to Gmail IMAP."""
        print("  Connecting to Gmail...")
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        self.imap.login(self.address, self.password)
        print("  Connected!")

    def disconnect(self):
        if self.imap:
            try:
                self.imap.logout()
            except Exception:
                pass

    # ── Email fetching ────────────────────────────────────────────────────────

    def _get_body(self, msg) -> str:
        """Extract plain text body from email."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="ignore")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="ignore")
        return ""

    def _is_recruiter_email(self, sender: str, subject: str, body: str) -> bool:
        """Decide if an email is from a recruiter / about a job."""
        # skip ignored senders
        sender_lower = sender.lower()
        if any(ignore in sender_lower for ignore in IGNORE_SENDERS):
            return False

        # check keywords in subject + body
        text = (subject + " " + body[:500]).lower()
        matches = sum(1 for kw in RECRUITER_KEYWORDS if kw in text)
        return matches >= 2  # require at least 2 keyword hits

    def check_inbox(self) -> list:
        """
        Check INBOX for new unread emails from recruiters.
        Returns list of dicts with email details.
        """
        self.imap.select("INBOX")
        _, msg_ids = self.imap.search(None, "UNSEEN")

        recruiter_emails = []

        for raw_id in msg_ids[0].split():
            msg_id = raw_id.decode()

            if msg_id in self.seen_ids:
                continue

            try:
                _, msg_data = self.imap.fetch(raw_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                sender  = msg.get("From", "")
                subject = msg.get("Subject", "")
                body    = self._get_body(msg)
                date    = msg.get("Date", "")

                if self._is_recruiter_email(sender, subject, body):
                    self.seen_ids.add(msg_id)
                    recruiter_emails.append({
                        "id":      msg_id,
                        "sender":  sender,
                        "subject": subject,
                        "body":    body,
                        "date":    date,
                    })

            except Exception as e:
                print(f"  Error reading email {msg_id}: {e}")
                continue

        return recruiter_emails

    # ── Reply drafting ────────────────────────────────────────────────────────

    def draft_reply(self, email_data: dict) -> str:
        """Use GPT to draft a reply to a recruiter email."""
        recruiter_message = f"Subject: {email_data['subject']}\n\n{email_data['body'][:1000]}"
        return ai_draft_email(self.client, self.resume, recruiter_message)

    def save_to_drafts(self, to_addr: str, subject: str, body: str):
        """Save the reply as a Gmail draft (you review before sending)."""
        msg = MIMEMultipart()
        msg["To"]      = to_addr
        msg["From"]    = self.address
        msg["Subject"] = f"Re: {subject}"
        msg.attach(MIMEText(body, "plain"))

        try:
            self.imap.select('"[Gmail]/Drafts"')
            self.imap.append(
                '"[Gmail]/Drafts"',
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
            print("  Saved to Gmail Drafts — review and send when ready!")
        except Exception as e:
            # fallback: save locally
            print(f"  Could not save to Gmail Drafts ({e}) — saving locally...")
            self._save_locally(to_addr, subject, body)

    def _save_locally(self, to_addr: str, subject: str, body: str):
        """Fallback — save draft reply as a text file in output/."""
        os.makedirs("output", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join("output", f"email_draft_{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"TO: {to_addr}\nSUBJECT: Re: {subject}\n\n{body}")
        print(f"  Draft saved to: {path}")

    # ── Application confirmation detection ───────────────────────────────────

    def check_confirmations(self) -> list:
        """
        Detect application confirmation emails (company saying 'we received your application').
        Returns list of (company, role) tuples.
        """
        confirmation_keywords = [
            "received your application", "thank you for applying",
            "application received", "we have received", "application submitted",
            "application confirmation", "thank you for your interest",
        ]

        self.imap.select("INBOX")
        _, msg_ids = self.imap.search(None, "UNSEEN")
        confirmations = []

        for raw_id in msg_ids[0].split():
            try:
                _, msg_data = self.imap.fetch(raw_id, "(RFC822)")
                msg    = email.message_from_bytes(msg_data[0][1])
                subject = msg.get("Subject", "").lower()
                body   = self._get_body(msg).lower()
                sender = msg.get("From", "")

                text = subject + " " + body[:300]
                if any(kw in text for kw in confirmation_keywords):
                    confirmations.append({
                        "sender":  sender,
                        "subject": msg.get("Subject", ""),
                        "date":    msg.get("Date", ""),
                    })
            except Exception:
                continue

        return confirmations

    # ── Main monitoring loop ──────────────────────────────────────────────────

    def run(self, interval_seconds: int = 60):
        """
        Poll Gmail every N seconds.
        - New recruiter emails → draft reply → save to Gmail Drafts
        - Confirmation emails → log them
        """
        print(f"\n  Email monitor running — checking every {interval_seconds}s")
        print(f"  Watching: {self.address}")
        print("  Press Ctrl+C to stop.\n")

        self.connect()

        try:
            while True:
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] Checking inbox...")

                # check for recruiter emails
                recruiter_emails = self.check_inbox()
                if recruiter_emails:
                    print(f"  Found {len(recruiter_emails)} recruiter email(s)!")
                    for em in recruiter_emails:
                        print(f"\n  From:    {em['sender']}")
                        print(f"  Subject: {em['subject']}")
                        print(f"  Drafting reply...")
                        reply = self.draft_reply(em)
                        print(f"\n  Draft:\n  {reply}\n")
                        self.save_to_drafts(em["sender"], em["subject"], reply)
                else:
                    print("  No new recruiter emails.")

                # check for application confirmations
                confirmations = self.check_confirmations()
                if confirmations:
                    print(f"\n  {len(confirmations)} application confirmation(s):")
                    for conf in confirmations:
                        print(f"  ✓ {conf['subject']} — {conf['sender']}")

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print("\n  Email monitor stopped.")
        finally:
            self.disconnect()
