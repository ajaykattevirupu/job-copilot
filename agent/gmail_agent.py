"""
Gmail Reply Agent — opens Gmail in browser, finds recruiter emails,
drafts AI replies, shows for approval, then sends.

Self-contained: creates its own browser window.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from tailor import _call_openai
from prompts import LINKEDIN_MESSAGE_REPLY_PROMPT
from agent import browser as B


RECRUITER_KEYWORDS = [
    "opportunity", "position", "role", "opening", "hiring",
    "interview", "recruiter", "talent", "candidate",
    "resume", "developer", "engineer", "job offer",
    "we found your profile", "your background", "reaching out",
]


class GmailAgent:

    def __init__(self, openai_client, profile: dict, resume_text: str, bridge=None, ctx=None):
        self.client      = openai_client
        self.profile     = profile
        self.resume_text = resume_text
        self.bridge      = bridge
        self.page        = None
        self.playwright  = None
        self.ctx         = None
        self._shared_ctx = ctx   # reuse existing browser context if provided
        self.replied     = 0

    # ── Logging ───────────────────────────────────────────────────

    def _log(self, msg, level="info"):
        if self.bridge:
            self.bridge.log(msg, level=level, tool="Gmail")
        else:
            print(f"  [Gmail] {msg}")

    # ── Browser ───────────────────────────────────────────────────

    def start_browser(self):
        if self._shared_ctx is not None:
            # Reuse the existing browser context (e.g. LinkedIn's) — just open a new tab
            self.ctx  = self._shared_ctx
            self.page = self.ctx.new_page()
        else:
            self.playwright = sync_playwright().start()
            self.ctx  = B.launch(self.playwright)
            self.page = self.ctx.new_page()

    def close_browser(self):
        # Close only our tab if we borrowed someone else's context
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        if self._shared_ctx is None:
            if self.ctx:        self.ctx.close()
            if self.playwright: self.playwright.stop()

    # ── Gmail navigation ──────────────────────────────────────────

    def open_inbox(self) -> bool:
        self._log("Opening Gmail inbox…")
        self.page.goto("https://mail.google.com/mail/u/0/#inbox")
        B.wait_for_navigation(self.page)
        B.pause()

        if "accounts.google.com" in self.page.url:
            self._log("Not logged into Gmail — waiting 90s for manual login", "warn")
            # Give user time to log in manually
            for _ in range(18):
                time.sleep(5)
                if "mail.google.com" in self.page.url:
                    break

        return "mail.google.com" in self.page.url

    def go_to_inbox(self):
        """Navigate back to inbox after reading an email."""
        try:
            back = self.page.query_selector('[aria-label*="Back to"], .hA')
            if back:
                B.click_el(self.page, back)
                return
        except Exception:
            pass
        self.page.goto("https://mail.google.com/mail/u/0/#inbox")
        B.pause()

    # ── Email scanning ────────────────────────────────────────────

    def get_recruiter_threads(self) -> list:
        """Scan inbox for threads that look like recruiter outreach."""
        threads = []
        try:
            rows = self.page.query_selector_all("tr.zA")  # all thread rows
            self._log(f"Scanning {len(rows)} inbox threads…")

            for row in rows[:60]:
                try:
                    subject_el = row.query_selector(".bog, .y6")
                    sender_el  = row.query_selector(".yP, .zF")
                    if not subject_el or not sender_el:
                        continue

                    subject = subject_el.inner_text().strip()
                    sender  = (
                        sender_el.get_attribute("name")
                        or sender_el.inner_text()
                    ).strip()

                    if any(kw in subject.lower() for kw in RECRUITER_KEYWORDS):
                        threads.append({
                            "row":     row,
                            "subject": subject,
                            "sender":  sender,
                        })
                except Exception:
                    continue
        except Exception as e:
            self._log(f"Inbox scan error: {e}", "error")

        return threads

    def read_thread_body(self, row) -> str:
        """Click a thread row and return the latest message body text."""
        try:
            B.click_el(self.page, row)
            B.wait_for_navigation(self.page)

            # Gmail message body selectors (try several)
            for sel in [".a3s.aiL", ".ii.gt div", ".gs .a3s"]:
                body_el = self.page.query_selector(sel)
                if body_el:
                    text = body_el.inner_text().strip()
                    if text:
                        return text
        except Exception as e:
            self._log(f"Could not read email: {e}", "warn")
        return ""

    # ── AI drafting ───────────────────────────────────────────────

    def draft_reply(self, email_body: str) -> str:
        prompt = LINKEDIN_MESSAGE_REPLY_PROMPT.format(message=email_body[:2000])
        return _call_openai(self.client, prompt, max_tokens=200)

    # ── Sending ───────────────────────────────────────────────────

    def send_reply(self, draft: str) -> bool:
        """Click Reply, type the draft, click Send."""
        try:
            # Find and click Reply
            clicked = False
            for sel in [
                'button[aria-label*="Reply"]',
                'span[data-tooltip*="Reply"]',
                '[data-tooltip="Reply"]',
                '.ams.bkH',     # Gmail internal classes
            ]:
                btn = self.page.query_selector(sel)
                if btn:
                    B.click_el(self.page, btn)
                    clicked = True
                    B.pause()
                    break

            if not clicked:
                return False

            # Find compose text area
            compose = None
            for sel in [
                '.Am.Al.editable.LW-avf',
                '[aria-label="Message Body"][contenteditable="true"]',
                '[contenteditable="true"].Am',
                'div[role="textbox"]',
            ]:
                compose = self.page.query_selector(sel)
                if compose:
                    break

            if not compose:
                return False

            # Focus, select-all, delete — then type the draft
            B.click_el(self.page, compose)
            B.short_pause()
            compose.press("Control+a")
            B.micro_pause()
            compose.press("Delete")
            B.short_pause()
            B.type_into(compose, draft)
            B.short_pause()

            # Click Send
            for sel in [
                'div[aria-label*="Send"]',
                '[data-tooltip="Send"]',
                'button[aria-label*="Send"]',
                '.T-I.J-J5-Ji.aoO',
            ]:
                send_btn = self.page.query_selector(sel)
                if send_btn:
                    B.click_el(self.page, send_btn)
                    B.pause()
                    return True

        except Exception as e:
            self._log(f"Send error: {e}", "error")

        return False

    # ── Main run loop ─────────────────────────────────────────────

    def run(self, max_replies: int = 10):
        self.start_browser()
        try:
            if not self.open_inbox():
                self._log("Could not open Gmail — skipping", "error")
                return

            threads = self.get_recruiter_threads()
            self._log(f"Found {len(threads)} potential recruiter thread(s)")

            for info in threads[:max_replies]:
                try:
                    body = self.read_thread_body(info["row"])
                    if not body:
                        self.go_to_inbox()
                        continue

                    self._log(f"Drafting reply — {info['subject'][:60]}")
                    draft = self.draft_reply(body)

                    # Request human approval
                    if self.bridge:
                        ok = self.bridge.request_approval(
                            job={
                                "title":   f"Reply: {info['subject'][:60]}",
                                "company": info["sender"],
                                "jd":      body[:3000],
                                "score":   0,
                            },
                            tailored_resume=draft,
                            modal_type="email",
                        )
                    else:
                        print(f"\n  Draft reply to {info['sender']}:\n\n{draft}\n")
                        ok = input("  Send this reply? (y/n): ").strip().lower() == "y"

                    if ok:
                        if self.send_reply(draft):
                            self.replied += 1
                            self._log(f"Reply sent to {info['sender']}", "success")
                            if self.bridge: self.bridge.inc_emails()
                        else:
                            self._log("Failed to send — reply saved as draft", "warn")
                    else:
                        self._log("Reply skipped", "skip")

                    self.go_to_inbox()
                    B.pause()

                except PlaywrightTimeout:
                    self._log("Timeout — moving on", "warn")
                    self.go_to_inbox()
                except Exception as e:
                    self._log(f"Error: {e}", "error")
                    self.go_to_inbox()

        finally:
            self.close_browser()
