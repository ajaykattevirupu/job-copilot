"""
Indeed Easy Apply Agent — self-contained, creates its own browser.

Searches Indeed, checks relevance, tailors resume, and applies to
jobs that have Indeed's native "Easy Apply" (not external portals).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from tailor import check_relevance, tailor_resume
from tracker import add_application, is_already_applied
from agent.pdf_generator import generate_pdf
from agent.docx_generator import generate_docx
from agent import browser as B
from user_profile import get_immigration_answers, requires_clearance, requires_citizenship

DATE_MAP = {"24h": "1", "week": "7", "month": "30", "any": ""}


class IndeedAgent:

    def __init__(self, openai_client, resume_text: str, profile: dict, bridge=None):
        self.client        = openai_client
        self.resume_text   = resume_text
        self.profile       = profile
        self.bridge        = bridge
        self.page          = None
        self.playwright    = None
        self.ctx           = None
        self.applied       = 0
        self.skipped       = 0
        self._current_job      = {}
        self._current_tailored = ""

    # ── Logging ───────────────────────────────────────────────────

    def _log(self, msg, level="info", job=None):
        if self.bridge:
            self.bridge.log(msg, level=level, job=job or self._current_job, tool="Indeed")
        else:
            print(f"  [Indeed] {msg}")

    # ── Browser ───────────────────────────────────────────────────

    def start_browser(self):
        self.playwright = sync_playwright().start()
        self.ctx  = B.launch(self.playwright)
        self.page = self.ctx.new_page()

    def close_browser(self):
        if self.ctx:        self.ctx.close()
        if self.playwright: self.playwright.stop()

    # ── Search ────────────────────────────────────────────────────

    def search(self, query: str, location: str, date_filter: str = "week"):
        from urllib.parse import quote
        fromage = DATE_MAP.get(date_filter, "7")
        url = f"https://www.indeed.com/jobs?q={quote(query)}&l={quote(location)}"
        if fromage:
            url += f"&fromage={fromage}"
        self._log(f"Searching: {query} · {location}")
        self.page.goto(url)
        B.wait_for_navigation(self.page)
        B.pause()

    # ── Job details ───────────────────────────────────────────────

    def get_job_cards(self):
        return self.page.query_selector_all(".job_seen_beacon, [data-jk]")

    def get_job_details(self) -> dict:
        d = {"title": "", "company": "", "jd": "", "location": ""}
        selectors = {
            "title":    ["h2.jobsearch-JobInfoHeader-title span",
                         "h1.jobTitle span"],
            "company":  ['[data-testid="inlineHeader-companyName"] a',
                         ".jobsearch-InlineCompanyRating-companyName"],
            "jd":       ["#jobDescriptionText", ".jobsearch-jobDescriptionText"],
            "location": ['[data-testid="job-location"]',
                         ".jobsearch-JobInfoHeader-subtitle span"],
        }
        for field, sels in selectors.items():
            for sel in sels:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        d[field] = el.inner_text().strip()
                        break
                except Exception:
                    continue
        return d

    def has_easy_apply(self) -> bool:
        return self.page.query_selector(
            '.ia-IndeedApplyButton, '
            '[data-tn-element="applyButton"], '
            'button[aria-label*="Easily apply"], '
            'span.ia-IndeedApplyButton'
        ) is not None

    # ── Apply flow ────────────────────────────────────────────────

    def do_easy_apply(self, resume_pdf: str) -> bool:
        """
        Click Easy Apply, walk through the popup form, get user approval, submit.
        Indeed Easy Apply opens in a popup window.
        """
        btn = self.page.query_selector(
            '.ia-IndeedApplyButton button, '
            '[data-tn-element="applyButton"], '
            'button.indeedApplyButton'
        )
        if not btn:
            return False

        try:
            with self.page.expect_popup() as popup_ctx:
                btn.click()
            popup = popup_ctx.value
        except Exception:
            return False

        B.wait_for_navigation(popup)
        B.pause()

        personal = self.profile.get("personal", {})
        name_parts = personal.get("name", "").split()
        first_name = name_parts[0] if name_parts else ""
        last_name  = name_parts[-1] if len(name_parts) > 1 else ""

        for _step in range(10):
            B.short_pause()

            # Fill personal fields if empty (human-like typing)
            fills = [
                ('input[name*="firstName"], input[id*="firstName"]', first_name),
                ('input[name*="lastName"],  input[id*="lastName"]',  last_name),
                ('input[type="email"]',                               personal.get("email", "")),
                ('input[type="tel"], input[name*="phone"]',           personal.get("phone", "")),
            ]
            for sel, value in fills:
                try:
                    el = popup.query_selector(sel)
                    if el and not el.input_value():
                        B.type_into(el, value)
                except Exception:
                    pass

            # Resume upload
            file_input = popup.query_selector('input[type="file"]')
            if file_input and resume_pdf:
                try:
                    file_input.set_input_files(resume_pdf)
                    B.short_pause()
                except Exception:
                    pass

            # Submit / Continue
            submit = popup.query_selector(
                'button[id*="submit"], button[aria-label*="Submit"], '
                'button[data-tn-element*="submit"]'
            )
            cont = popup.query_selector(
                'button[id*="continue"], button[aria-label*="Continue"], '
                'button[data-tn-element*="continue"]'
            )

            if submit:
                if self.bridge:
                    ok = self.bridge.request_approval(
                        self._current_job, self._current_tailored
                    )
                else:
                    ok = input("  Submit to Indeed? (y/n): ").strip().lower() == "y"

                if ok:
                    B.click_el(popup, submit)
                    B.pause()
                    popup.close()
                    return True
                else:
                    popup.close()
                    return False

            elif cont:
                B.click_el(popup, cont)
            else:
                # Try any primary button as fallback
                primary = popup.query_selector(
                    'button[class*="primary"], button[class*="continue"]'
                )
                if primary:
                    B.click_el(popup, primary)
                else:
                    break

        popup.close()
        return False

    # ── Main run loop ──────────────────────────────────────────────

    def run(self, query: str = "Java Full Stack Engineer",
            location: str = "United States",
            max_applications: int = 20,
            date_filter: str = "week"):

        imm            = get_immigration_answers(self.profile)
        skip_clearance = imm.get("skip_clearance", True)

        self.start_browser()
        seen = set()

        try:
            self.search(query, location, date_filter)
            scroll_count = 0

            while self.applied < max_applications:
                cards = self.get_job_cards()
                if not cards:
                    break

                for card in cards:
                    if self.applied >= max_applications:
                        break
                    try:
                        B.click_el(self.page, card)
                        B.pause()

                        d       = self.get_job_details()
                        title   = d["title"]   or "Unknown Role"
                        company = d["company"] or "Unknown Company"
                        jd      = d["jd"]

                        # Deduplicate
                        key = f"{company[:20]}|{title[:20]}"
                        if key in seen or not jd:
                            continue
                        seen.add(key)

                        job_ref = {"title": title, "company": company}
                        self._log(f"{title} @ {company}", job=job_ref)

                        # Already applied?
                        if is_already_applied(company, title):
                            self._log("Already applied — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Immigration filters
                        if skip_clearance and requires_clearance(jd):
                            self._log("Requires security clearance — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        if skip_clearance and requires_citizenship(jd):
                            self._log("US citizens / GC only — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Relevance check
                        rel = check_relevance(self.client, jd)
                        self._log(
                            f"[{rel['score']}/10] {rel['decision']} — {rel['reason']}",
                            job=job_ref
                        )

                        if rel["decision"] in ("SKIP", "MAYBE"):
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Indeed Easy Apply only
                        if not self.has_easy_apply():
                            self._log("No Indeed Easy Apply — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Tailor + apply
                        self._log("Tailoring resume…", job=job_ref)
                        tailored = tailor_resume(self.client, self.resume_text, jd)
                        self._current_job = {
                            "title":    title,
                            "company":  company,
                            "location": d.get("location", ""),
                            "jd":       jd[:3000],
                            "score":    rel.get("score", 0),
                        }
                        self._current_tailored = tailored
                        resume_pdf = generate_pdf(tailored)
                        generate_docx(tailored)

                        if self.do_easy_apply(resume_pdf):
                            self.applied += 1
                            add_application(company, title, "Indeed Easy Apply")
                            self._log(
                                f"Applied ({self.applied}/{max_applications})",
                                "success", self._current_job
                            )
                            if self.bridge: self.bridge.inc_applied()
                        else:
                            self._log("Could not complete application", "warn", job_ref)

                    except PlaywrightTimeout:
                        self._log("Timeout — moving on", "warn")
                    except Exception as e:
                        self._log(f"Error: {e}", "error")

                # Scroll for more results
                B.scroll_down(self.page, 2000)
                B.pause()
                scroll_count += 1
                if scroll_count > 15:
                    break

        finally:
            self.close_browser()
