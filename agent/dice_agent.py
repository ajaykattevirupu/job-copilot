"""
Dice.com Job Agent — searches Dice, logs in, and applies to matching jobs.

Apply flow:
  1. Search with filters → scan job cards
  2. Open each job detail page
  3. Click "Apply Now" → handle the "you need to log in" modal → log in
  4. If Dice Quick Apply → walk the native form
  5. If external ATS → hand off to PortalAgent (Workday / Greenhouse / etc.)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import quote
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent import browser as B
from tailor import check_relevance, tailor_resume, analyze_skills_gap
from tracker import add_application, is_already_applied
from agent.docx_generator import generate_docx
from agent.portal_agent import PortalAgent
from user_profile import get_immigration_answers, requires_clearance, requires_citizenship

# Dice date-posted param values
DATE_MAP = {
    "24h":   "ONE",
    "week":  "SEVEN",
    "month": "THIRTY",
    "any":   "",
}


class DiceAgent:

    def __init__(self, openai_client, resume_text: str, profile: dict,
                 email: str = "", password: str = "",
                 resume_docx: str = "", resume_pdf: str = "",
                 bridge=None):
        self.client       = openai_client
        self.resume_text  = resume_text
        self.profile      = profile
        self.email        = email or profile.get("dice", {}).get("email", "")
        self.password     = password or profile.get("dice", {}).get("password", "")
        self.resume_docx  = resume_docx
        self.resume_pdf   = resume_pdf
        self.bridge       = bridge
        self.page         = None
        self.playwright   = None
        self.ctx          = None
        self.applied      = 0
        self.skipped      = 0
        self._current_job     = {}
        self._current_tailored = ""
        self._logged_in   = False

    # ── Logging ───────────────────────────────────────────────────

    def _log(self, msg, level="info", job=None):
        if self.bridge:
            self.bridge.log(msg, level=level, job=job or self._current_job, tool="Dice")
        else:
            print(f"  [Dice] {msg}")

    # ── Browser ───────────────────────────────────────────────────

    def start_browser(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.ctx  = B.launch(self.playwright)
        self.page = self.ctx.new_page()

    def close_browser(self):
        if self.ctx:        self.ctx.close()
        if self.playwright: self.playwright.stop()

    # ── Login ─────────────────────────────────────────────────────

    def _do_login(self, page=None) -> bool:
        """
        Log in to Dice using stored credentials.
        `page` defaults to self.page; pass a different page for the inline
        login form that appears after clicking "Log in" in the apply modal.
        """
        p = page or self.page
        if not self.email or not self.password:
            self._log("No Dice credentials configured — skipping login", "warn")
            return False

        try:
            # Navigate to login page if not already there
            if "login" not in p.url.lower() and "signin" not in p.url.lower():
                p.goto("https://www.dice.com/dashboard/login")
                B.wait_for_navigation(p)
                B.pause()

            # Fill email
            for sel in [
                'input[id="email"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[placeholder*="Email"]',
            ]:
                el = p.query_selector(sel)
                if el and el.is_visible():
                    B.type_into(el, self.email)
                    break

            B.short_pause()

            # Fill password
            for sel in [
                'input[id="password"]',
                'input[name="password"]',
                'input[type="password"]',
            ]:
                el = p.query_selector(sel)
                if el and el.is_visible():
                    B.type_into(el, self.password)
                    break

            B.short_pause()

            # Click Sign In
            for sel in [
                'button[type="submit"]',
                'button:has-text("Sign In")',
                'button:has-text("Log In")',
                'button:has-text("Login")',
            ]:
                btn = p.query_selector(sel)
                if btn and btn.is_visible():
                    B.click_el(p, btn)
                    break

            B.wait_for_navigation(p)
            B.pause(1.5, 2.5)

            self._logged_in = "login" not in p.url.lower()
            if self._logged_in:
                self._log("Logged in to Dice", "success")
            else:
                self._log("Dice login may have failed — check credentials", "warn")
            return self._logged_in

        except Exception as e:
            self._log(f"Login error: {e}", "error")
            return False

    def _ensure_logged_in(self) -> bool:
        """Log in once per session; no-op if already logged in."""
        if self._logged_in:
            return True
        return self._do_login()

    # ── Search ────────────────────────────────────────────────────

    def _search(self, query: str, location: str,
                date_filter: str = "week") -> bool:
        date_val = DATE_MAP.get(date_filter, "SEVEN")
        url = (
            f"https://www.dice.com/jobs?q={quote(query)}"
            f"&location={quote(location)}&radius=30&radiusUnit=mi"
            f"&page=1&pageSize=20&language=en"
        )
        if date_val:
            url += f"&filters.postedDate={date_val}"

        self._log(f"Searching: {query} in {location}")
        self.page.goto(url)
        B.wait_for_navigation(self.page)
        B.pause()
        return True

    # ── Job cards ─────────────────────────────────────────────────

    def _get_cards(self) -> list:
        """Return all visible job-card elements on the current search page."""
        return self.page.query_selector_all(
            'dhi-search-result, '
            '[data-cy="search-result-item"], '
            'div.card.search-card, '
            'a[data-cy="card-title-link"]'
        )

    def _read_card(self, card) -> dict:
        """Extract title, company, and detail URL from a search card."""
        d = {"title": "", "company": "", "url": ""}
        try:
            title_el = card.query_selector(
                '[data-cy="card-title-link"], h5 a, .card-title a, a.card-title'
            )
            if title_el:
                d["title"] = title_el.inner_text().strip()
                href = title_el.get_attribute("href") or ""
                if href.startswith("http"):
                    d["url"] = href
                elif href:
                    d["url"] = "https://www.dice.com" + href
        except Exception:
            pass
        try:
            comp_el = card.query_selector(
                '[data-cy="search-result-company-name"], '
                '.company-name, .employer-name'
            )
            if comp_el:
                d["company"] = comp_el.inner_text().strip()
        except Exception:
            pass
        return d

    # ── Job detail ────────────────────────────────────────────────

    def _get_jd(self) -> str:
        """Extract the job description from the current detail page."""
        for sel in [
            '[data-cy="jobDescription"]',
            '#jobDescription',
            '.job-description',
            '[class*="description"]',
        ]:
            try:
                el = self.page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if len(text) > 100:
                        return text
            except Exception:
                continue
        return ""

    def _get_apply_url(self) -> str:
        """Return the external ATS URL embedded in the Apply button, if any."""
        for sel in [
            'a[data-cy="apply-button-top"][href]',
            'a[data-testid="apply-link"][href]',
            'a.btn-apply[href]',
        ]:
            try:
                el = self.page.query_selector(sel)
                if el:
                    href = el.get_attribute("href") or ""
                    if href.startswith("http") and "dice.com" not in href:
                        return href
            except Exception:
                continue
        return ""

    # ── Apply-modal login handler ─────────────────────────────────

    def _handle_apply_modal(self) -> bool:
        """
        Dice shows an 'Apply to job — you need to log in' modal.
        Click 'Log in', wait for the inline form, fill credentials.
        Returns True if we got past the modal.
        """
        # Check if the modal is visible
        modal = self.page.query_selector(
            '[role="dialog"]:has-text("Apply to job"), '
            '.modal:has-text("log in"), '
            'div:has-text("To apply to this job, you need to")'
        )
        if not modal:
            return True   # no modal — already logged in or direct apply

        self._log("Login modal detected — clicking 'Log in'")

        # Click the Log In button inside the modal
        for sel in [
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            'button:has-text("Login")',
        ]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    B.click_el(self.page, btn)
                    B.pause(1.5, 2.5)
                    break
            except Exception:
                continue

        # Now fill the login form (may be inline or new page)
        return self._do_login()

    # ── Quick Apply (Dice native form) ────────────────────────────

    def _dice_quick_apply(self) -> bool:
        """
        Handle Dice's native Quick Apply multi-step form.
        Returns True on success.
        """
        personal = self.profile.get("personal", {})
        name_parts = personal.get("name", "").split()
        first = name_parts[0] if name_parts else ""
        last  = name_parts[-1] if len(name_parts) > 1 else ""

        for step in range(8):
            B.short_pause()

            # Fill standard fields
            fills = [
                ('input[name*="firstName"], input[id*="firstName"], input[placeholder*="First"]', first),
                ('input[name*="lastName"],  input[id*="lastName"],  input[placeholder*="Last"]',  last),
                ('input[type="email"]',                                                           personal.get("email", "")),
                ('input[type="tel"], input[name*="phone"]',                                       personal.get("phone", "")),
            ]
            for sel, val in fills:
                if not val:
                    continue
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible() and not el.input_value():
                        B.type_into(el, val)
                except Exception:
                    pass

            # Resume upload
            for inp in self.page.query_selector_all('input[type="file"]'):
                try:
                    if not inp.is_visible():
                        continue
                    accept = (inp.get_attribute("accept") or "").lower()
                    pdf_only = ".pdf" in accept and ".doc" not in accept
                    if self.resume_docx and not pdf_only:
                        inp.set_input_files(self.resume_docx)
                    elif self.resume_pdf:
                        inp.set_input_files(self.resume_pdf)
                    B.short_pause()
                    break
                except Exception:
                    pass

            # Submit
            submit = self.page.query_selector(
                'button[type="submit"]:has-text("Submit"), '
                'button:has-text("Submit application"), '
                'button:has-text("Apply")'
            )
            if submit and submit.is_visible():
                if self.bridge:
                    ok = self.bridge.request_approval(
                        self._current_job, self._current_tailored
                    )
                else:
                    ok = input("  Submit to Dice? (y/n): ").strip().lower() == "y"
                if ok:
                    B.click_el(self.page, submit)
                    B.pause()
                    return True
                return False

            # Next / Continue
            nxt = self.page.query_selector(
                'button:has-text("Next"), button:has-text("Continue"), '
                'button[type="button"]:has-text("Next step")'
            )
            if nxt and nxt.is_visible():
                B.click_el(self.page, nxt)
            else:
                break

        return False

    # ── Main apply flow for one job ───────────────────────────────

    def _apply_to_job(self, detail_url: str, title: str, company: str,
                      jd: str, tailored: str) -> bool:
        """
        Navigate to the job detail page, click Apply Now, handle modal/login,
        then either use Quick Apply or hand off to PortalAgent.
        """
        if self.page.url != detail_url:
            self.page.goto(detail_url)
            B.wait_for_navigation(self.page)
            B.pause()

        # Click the primary Apply Now button
        apply_clicked = False
        for sel in [
            '[data-cy="apply-button-top"]',
            'button:has-text("Apply Now")',
            'a:has-text("Apply Now")',
            'button:has-text("Easy Apply")',
            '[data-testid="apply-button"]',
        ]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    # Check if it's an external link
                    href = btn.get_attribute("href") or ""
                    if href.startswith("http") and "dice.com" not in href:
                        # Direct external ATS link — no modal needed
                        ext_url = href
                        apply_clicked = True
                        break
                    B.click_el(self.page, btn)
                    B.pause(1.5, 2.5)
                    apply_clicked = True
                    break
            except Exception:
                continue

        if not apply_clicked:
            self._log("No Apply button found", "warn")
            return False

        # Handle login modal if it appeared
        if not self._handle_apply_modal():
            self._log("Could not pass the login modal", "warn")
            return False

        # After login, we might be redirected to an external ATS
        B.pause(1.5, 2.5)
        current_url = self.page.url

        # Check if we've landed on an external ATS page
        from agent.portal_agent import detect_portal
        portal = detect_portal(current_url)

        if portal != "generic" or "dice.com" not in current_url:
            # External ATS — hand off to PortalAgent
            self._log(f"Redirected to external portal: {portal}")
            agent = PortalAgent(
                page               = self.page,
                openai_client      = self.client,
                profile            = self.profile,
                resume_text        = tailored,
                resume_pdf         = self.resume_pdf,
                resume_docx        = self.resume_docx,
                review_before_submit = True,
                bridge             = self.bridge,
            )
            agent._current_job = self._current_job
            return agent.apply(
                url        = current_url,
                job_title  = title,
                company    = company,
                jd         = jd,
                job_meta   = self._current_job,
            )

        # Still on Dice — use Quick Apply
        self._log("Using Dice Quick Apply")
        return self._dice_quick_apply()

    # ── Pagination ────────────────────────────────────────────────

    def _next_page(self) -> bool:
        for sel in [
            'a[aria-label="Next page"]',
            'button[aria-label="Next page"]',
            'li.pagination-next a',
            '[data-cy="pagination-next"]',
            'button:has-text("Next")',
        ]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    B.click_el(self.page, btn)
                    B.wait_for_navigation(self.page)
                    B.pause()
                    return True
            except Exception:
                continue
        return False

    # ── Main run loop ──────────────────────────────────────────────

    def run(self, query: str = "Java Full Stack Engineer",
            location: str = "United States",
            max_applications: int = 20,
            date_filter: str = "week",
            **_kwargs):

        imm            = get_immigration_answers(self.profile)
        skip_clearance = imm.get("skip_clearance", True)

        self.start_browser()
        seen = set()

        try:
            # Log in first so the apply modal is bypassed on most jobs
            self._ensure_logged_in()

            self._search(query, location, date_filter)

            page_num = 1
            while self.applied < max_applications:
                if self.bridge and not self.bridge.is_running:
                    break

                cards = self._get_cards()
                if not cards:
                    self._log("No job cards found on this page", "warn")
                    break

                self._log(f"Page {page_num} — {len(cards)} cards")

                for card in cards:
                    if self.applied >= max_applications:
                        break
                    if self.bridge and not self.bridge.is_running:
                        break

                    try:
                        info = self._read_card(card)
                        title   = info["title"]   or "Unknown Role"
                        company = info["company"]  or "Unknown Company"
                        url     = info["url"]

                        if not url:
                            continue

                        key = f"{company[:20]}|{title[:20]}"
                        if key in seen:
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

                        # Open job detail page
                        self.page.goto(url)
                        B.wait_for_navigation(self.page)
                        B.pause()

                        jd = self._get_jd()
                        if not jd:
                            self._log("No job description found — skipping", "warn", job_ref)
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

                        # Tailor resume
                        self._log("Tailoring resume…", job=job_ref)
                        tailored = tailor_resume(self.client, self.resume_text, jd)

                        self._current_job = {
                            "title":    title,
                            "company":  company,
                            "jd":       jd[:3000],
                            "score":    rel.get("score", 0),
                        }
                        self._current_tailored = tailored

                        # Generate DOCX
                        if not self.resume_docx:
                            self.resume_docx = generate_docx(tailored)

                        # Apply
                        if self._apply_to_job(url, title, company, jd, tailored):
                            self.applied += 1
                            add_application(company, title, "Dice")
                            self._log(
                                f"Applied ({self.applied}/{max_applications})",
                                "success", self._current_job
                            )
                            if self.bridge: self.bridge.inc_applied()
                        else:
                            self._log("Could not complete application", "warn", job_ref)

                        # Go back to search results for the next card
                        self.page.go_back()
                        B.wait_for_navigation(self.page)
                        B.pause()

                    except PlaywrightTimeout:
                        self._log("Timeout — moving on", "warn")
                        try:
                            self.page.go_back()
                            B.wait_for_navigation(self.page)
                        except Exception:
                            self._search(query, location, date_filter)
                    except Exception as e:
                        self._log(f"Error: {e}", "error")

                # Next search-results page
                if not self._next_page():
                    break
                page_num += 1

        finally:
            self.close_browser()
