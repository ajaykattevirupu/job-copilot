"""
External Portal Agent — fills company ATS forms when LinkedIn's Apply button
redirects to an external page (Workday, Greenhouse, Lever, iCIMS, etc.).

Uses human-like typing/clicking from browser.py.
Shows the approval modal via bridge before submitting.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import browser as B
from agent.browser import reading_countdown, check_human_takeover

PORTAL_PATTERNS = {
    "workday":         ["myworkdayjobs.com", "wd1.myworkdaysite.com", "wd3.myworkdaysite.com"],
    "greenhouse":      ["greenhouse.io", "boards.greenhouse.io"],
    "lever":           ["jobs.lever.co", "lever.co/"],
    "icims":           ["icims.com", "careers.icims.com"],
    "smartrecruiters": ["careers.smartrecruiters.com", "smartrecruiters.com/jobs"],
    "bamboohr":        ["bamboohr.com", "effortlesshiring.com"],
    "taleo":           ["taleo.net"],
    "jobvite":         ["jobs.jobvite.com"],
    "successfactors":  ["successfactors.com", "sapsf.com"],
}

# Buttons that advance to the next step
NEXT_SELECTORS = [
    '[data-automation-id="bottom-navigation-next-button"]',
    'button[aria-label="Continue to next step"]',
    'button[aria-label*="Next"]',
    'button[aria-label*="Continue"]',
    'button:has-text("Next step")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    # NOTE: artdeco-button--primary removed — it matches LinkedIn UI buttons falsely
]

SUBMIT_SELECTORS = [
    'button[aria-label="Submit application"]',
    'button[aria-label*="Submit"]',
    'button:has-text("Submit application")',
    'button:has-text("Submit")',
    'button[type="submit"]',
    'input[type="submit"]',
    '[data-automation-id*="submit"]',
    'button:has-text("Apply")',
    'button:has-text("Send application")',
]


def detect_portal(url: str) -> str:
    url_lower = url.lower()
    for portal, patterns in PORTAL_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return portal
    return "generic"


class PortalAgent:

    def __init__(self, page, openai_client, profile: dict, resume_text: str,
                 resume_pdf: str, resume_docx: str,
                 review_before_submit: bool = True, bridge=None,
                 cover_letter: str = ""):
        self.page                 = page
        self.client               = openai_client
        self.profile              = profile
        self.resume_text          = resume_text
        self.resume_pdf           = resume_pdf
        self.resume_docx          = resume_docx
        self.review_before_submit = review_before_submit
        self.bridge               = bridge
        self._current_job         = {}
        self._resume_uploaded     = False
        self.cover_letter         = cover_letter

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg, level="info"):
        if self.bridge:
            self.bridge.log(msg, level=level, job=self._current_job, tool="Portal")
        else:
            print(f"  [Portal] {msg}")

    # ── Profile helpers ───────────────────────────────────────────────────────

    def _p(self, key, default=""):
        return self.profile.get("personal", {}).get(key, default)

    def _first(self):
        return self._p("name").split()[0] if self._p("name") else ""

    def _last(self):
        parts = self._p("name").split()
        return parts[-1] if len(parts) > 1 else ""

    # ── Human-like field filling ───────────────────────────────────────────────

    def _type(self, selector: str, value: str):
        """Type into a field by selector, human-like."""
        if not value:
            return False
        try:
            el = self.page.query_selector(selector)
            if el and el.is_visible():
                B.type_into(el, value)
                return True
        except Exception:
            pass
        return False

    def _upload_resume(self, prefer_docx: bool = True):
        """Upload resume to any visible file input. No-ops after the first successful upload."""
        if self._resume_uploaded:
            return True
        for inp in self.page.query_selector_all('input[type="file"]'):
            try:
                if not inp.is_visible():
                    continue
                accept = (inp.get_attribute("accept") or "").lower()
                pdf_only = accept and ".pdf" in accept and ".doc" not in accept
                if prefer_docx and self.resume_docx and not pdf_only:
                    inp.set_input_files(self.resume_docx)
                    self._log("Uploaded DOCX resume")
                elif self.resume_pdf:
                    inp.set_input_files(self.resume_pdf)
                    self._log("Uploaded PDF resume")
                else:
                    continue
                B.short_pause()
                self._resume_uploaded = True
                return True
            except Exception:
                continue
        return False

    # ── AI question answering ──────────────────────────────────────────────────

    def _ai_answer(self, question: str, input_type: str = "text",
                   options: list = None, jd: str = "") -> str:
        # Use pre-generated cover letter for cover letter fields
        if self.cover_letter and any(
            kw in question.lower()
            for kw in ["cover letter", "cover_letter", "motivation", "why are you", "why do you want"]
        ):
            return self.cover_letter

        try:
            from tailor import _call_openai
            from prompts import build_answer_prompt
            opts = ("Options:\n" + "\n".join(f"- {o}" for o in options)) if options else ""
            prompt = build_answer_prompt(question, input_type, opts, self.profile)
            answer = _call_openai(self.client, prompt, max_tokens=150)
            return answer if answer and answer.lower() not in ["unknown", "n/a", ""] else ""
        except Exception:
            return ""

    def _label_for(self, el) -> str:
        try:
            eid = el.get_attribute("id")
            if eid:
                lbl = self.page.query_selector(f'label[for="{eid}"]')
                if lbl:
                    return lbl.inner_text().strip()
        except Exception:
            pass
        try:
            return (
                el.get_attribute("aria-label") or
                el.get_attribute("placeholder") or
                el.get_attribute("name") or ""
            ).strip()
        except Exception:
            return ""

    # ── Standard fields (works on all portals) ────────────────────────────────

    def _fill_standard(self):
        """Fill name / email / phone / location fields that appear on every portal."""
        pairs = [
            ('input[name*="firstName"], input[id*="firstName"], input[placeholder*="First"]', self._first()),
            ('input[name*="lastName"],  input[id*="lastName"],  input[placeholder*="Last"]',  self._last()),
            ('input[name*="fullName"],  input[placeholder*="Full name"]',                      self._p("name")),
            ('input[type="email"],      input[name*="email"],   input[id*="email"]',           self._p("email")),
            ('input[type="tel"],        input[name*="phone"],   input[id*="phone"]',           self._p("phone")),
            ('input[name*="city"],      input[id*="city"],      input[placeholder*="City"]',   self._p("city")),
            ('input[name*="state"],     input[id*="state"]',                                   self._p("state")),
            ('input[name*="zip"],       input[name*="postal"]',                                self._p("zip")),
            ('input[name*="linkedin"],  input[placeholder*="LinkedIn"]',                       self._p("linkedin")),
        ]
        for sel, val in pairs:
            if val:
                self._type(sel, val)

    # ── Fill all visible inputs on the current page ────────────────────────────

    def _fill_page_inputs(self, jd: str = ""):
        """
        Fill every unfilled visible input/textarea/select on the current page
        using AI for custom questions. Skips already-filled fields.
        """
        # Text inputs
        for inp in self.page.query_selector_all(
            'input[type="text"]:visible, input[type="number"]:visible, '
            'input[type="email"]:visible, input[type="tel"]:visible'
        ):
            try:
                if inp.input_value():
                    continue
                label = self._label_for(inp)
                if not label:
                    continue
                skip = ["name", "email", "phone", "city", "state", "zip", "postal"]
                if any(s in label.lower() for s in skip):
                    continue
                answer = self._ai_answer(label, inp.get_attribute("type") or "text", jd=jd)
                if answer:
                    B.type_into(inp, answer)
            except Exception:
                continue

        # Textareas
        for ta in self.page.query_selector_all("textarea:visible"):
            try:
                if ta.input_value():
                    continue
                label = self._label_for(ta) or "cover letter or additional info"
                # Use pre-generated cover letter for cover letter / motivation fields
                is_cl_field = any(kw in label.lower() for kw in
                                  ["cover letter", "cover_letter", "motivation", "why are you",
                                   "why do you want", "why this", "additional info"])
                if is_cl_field and self.cover_letter:
                    B.type_into(ta, self.cover_letter)
                    continue
                answer = self._ai_answer(label, "textarea", jd=jd)
                if answer:
                    B.type_into(ta, answer)
            except Exception:
                continue

        # Selects
        for sel_el in self.page.query_selector_all("select:visible"):
            try:
                label = self._label_for(sel_el)
                opts  = [
                    o.inner_text().strip()
                    for o in sel_el.query_selector_all("option")
                    if o.get_attribute("value")
                ]
                if not opts or not label:
                    continue
                answer = self._ai_answer(label, "select", options=opts, jd=jd)
                for opt in opts:
                    if answer.lower() in opt.lower() or opt.lower() in answer.lower():
                        sel_el.select_option(label=opt)
                        break
            except Exception:
                continue

        # Radio button groups
        for fieldset in self.page.query_selector_all("fieldset:visible"):
            try:
                legend = fieldset.query_selector("legend")
                question = legend.inner_text().strip() if legend else ""
                if not question:
                    continue
                answer = self._ai_answer(question, "yes/no radio")
                for radio in fieldset.query_selector_all('input[type="radio"]'):
                    lbl = self._label_for(radio)
                    if answer.lower() in lbl.lower():
                        B.click_el(self.page, radio)
                        break
            except Exception:
                continue

        B.short_pause()

    # ── Human-assist pause ───────────────────────────────────────────────────

    def _request_human_assist(self, reason: str) -> bool:
        """
        Notify the user that the bot is stuck and wait indefinitely for them
        to fill the form manually.  Returns True to continue, False to skip.
        """
        self._log(f"Bot stuck — {reason}", "warn")
        self._save_screenshot("needs_human_assist")

        job_title = self._current_job.get("title", "Application")

        if self.bridge:
            self.bridge.notify(
                "Job Copilot — Manual Input Needed",
                f"{job_title}: {reason[:80]}"
            )
            return self.bridge.request_approval(
                job={
                    **self._current_job,
                    "title": f"[Manual help needed] {job_title}",
                },
                tailored_resume=(
                    "The bot is stuck and needs your help.\n\n"
                    f"Reason: {reason}\n\n"
                    "Please fill in the remaining fields manually in the browser.\n\n"
                    "Click APPROVE when done so the bot can continue/submit,\n"
                    "or REJECT to skip this job."
                ),
            )
        else:
            print(f"\n  [Portal] Manual help needed: {reason}")
            print("  Please fill the form manually in the browser window.")
            return input("  Press Y when done to continue, N to skip: "
                         ).strip().lower() == "y"

    # ── Screenshot helper ─────────────────────────────────────────────────────

    def _save_screenshot(self, name: str = "blocked"):
        """Save a screenshot to logs/ for debugging."""
        try:
            logs_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
            )
            os.makedirs(logs_dir, exist_ok=True)
            ts   = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(logs_dir, f"{name}_{ts}.png")
            self.page.screenshot(path=path)
            self._log(f"Screenshot saved → logs/{name}_{ts}.png", "warn")
        except Exception as e:
            self._log(f"Screenshot failed: {e}", "warn")

    # ── Overlay / cookie banner dismissal ────────────────────────────────────

    def _dismiss_overlays(self):
        """Click away cookie banners, privacy modals, and GDPR notices."""
        dismiss_sels = [
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("Accept Cookies")',
            'button:has-text("Accept")',
            'button:has-text("I Accept")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            'button:has-text("Close")',
            '[aria-label="Close"]',
            '[aria-label="close"]',
            '[data-automation-id="closeButton"]',
            '#onetrust-accept-btn-handler',
            '.cookie-accept',
        ]
        for sel in dismiss_sels:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    B.click_el(self.page, btn)
                    B.short_pause()
                    return True
            except Exception:
                continue
        return False

    # ── Navigate multi-step forms ──────────────────────────────────────────────

    def _find_submit(self):
        submit_sels = SUBMIT_SELECTORS + [
            '[data-automation-id="bottom-navigation-next-button"]:has-text("Submit")',
            '[data-automation-id="wd-CommandButton"]:has-text("Submit")',
            'button[data-uxi-element-id*="submit"]',
        ]
        for sel in submit_sels:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    def _find_next(self):
        next_sels = [
            # Workday-specific automation IDs (most reliable)
            '[data-automation-id="bottom-navigation-next-button"]',
            '[data-automation-id="wd-CommandButton"]:has-text("Next")',
            '[data-automation-id="wd-CommandButton"]:has-text("Continue")',
            # Generic
            'button[aria-label="Continue to next step"]',
            'button[aria-label*="Next"]',
            'button[aria-label*="Continue"]',
            'button:has-text("Next step")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("Save and Continue")',
        ] + NEXT_SELECTORS
        seen = set()
        for sel in next_sels:
            if sel in seen:
                continue
            seen.add(sel)
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    label = (btn.get_attribute("aria-label") or btn.inner_text() or "").lower()
                    if "submit" not in label:
                        return btn
            except Exception:
                continue
        return None

    def _walk_form(self, jd: str = "", max_steps: int = 12) -> bool:
        """
        Walk through a multi-step form:
          dismiss overlays → fill page → click Next → repeat → submit.

        Step 0 always does a quick fill of common fields (name/email/phone/resume)
        before the full AI-powered pass.  When the bot cannot advance (no Next or
        Submit button, or the form is stuck in a loop), it asks the user for manual
        help and waits indefinitely rather than giving up immediately.

        Returns True if submitted.
        """
        if "linkedin.com" in self.page.url:
            self._log("Still on LinkedIn — external portal did not load properly", "warn")
            return False

        last_url = None
        for step in range(max_steps):
            if self.bridge and not self.bridge.is_running:
                return False

            self._dismiss_overlays()
            self._log(f"Filling form — step {step + 1}…")

            # ── Step 0: always attempt the fast common-field fill first ──────
            # This guarantees name/email/phone/resume are populated before we
            # spend time on AI-powered custom questions.
            if step == 0:
                self._upload_resume()
                self._fill_standard()

            # Full AI-powered pass for custom questions / selects / radios
            self._fill_page_inputs(jd)

            # Also catch any file inputs that weren't present on load
            self._upload_resume()

            submit_btn = self._find_submit()
            if submit_btn:
                # Reading pause — let the user review before bot submits
                job_label = (
                    f"{self._current_job.get('title', '')} "
                    f"@ {self._current_job.get('company', '')}"
                ).strip(" @")
                _cd = reading_countdown(job_label, action="Submit", bridge=self.bridge)
                if _cd in ("skip", "pause") or check_human_takeover(self.page):
                    self._log("User took control — submission cancelled, tab left open", "skip")
                    return False

                if self.review_before_submit:
                    if self.bridge:
                        approved = self.bridge.request_approval(
                            self._current_job, self.resume_text
                        )
                    else:
                        approved = input("  Submit? (y/n): ").strip().lower() == "y"
                    if not approved:
                        self._log("Submission cancelled", "skip")
                        return False

                submit_btn = self._find_submit()
                if not submit_btn:
                    self._log("Submit button gone after approval wait", "warn")
                    return False

                B.click_el(self.page, submit_btn)
                B.pause(1.5, 2.5)
                self._log("Submitted via external portal", "success")
                return True

            next_btn = self._find_next()
            if next_btn:
                current_url = self.page.url
                B.click_el(self.page, next_btn)
                B.wait_for_navigation(self.page)
                B.pause()
                if self.page.url == current_url == last_url:
                    # Form didn't advance — validation error or JS-gated step.
                    # Ask the user to fix it manually rather than looping forever.
                    self._log("Form not advancing — requesting human assist", "warn")
                    if not self._request_human_assist(
                        "The form is not advancing. It may have validation errors "
                        "or required fields the bot couldn't fill. Please correct "
                        "them and click Next, then approve to let the bot continue."
                    ):
                        return False
                    # After the user acts, give the page a moment to settle then retry
                    B.pause(1.5, 2.5)
                    last_url = None  # reset loop-detection
                    continue
                last_url = current_url
            else:
                # No Next or Submit visible — pause and ask for human help.
                # The bot has already filled everything it could; now it needs
                # the user to handle the current screen (e.g. EEO, custom quiz,
                # or an unusual layout the selectors don't cover).
                self._log("No Next or Submit button found — requesting human assist", "warn")
                if not self._request_human_assist(
                    "The bot cannot find a Next or Submit button on this screen. "
                    "This may be a custom questionnaire, EEO form, or unusual layout. "
                    "Please complete this screen manually, then approve to continue."
                ):
                    return False
                # Give the page time to update after user interaction
                B.pause(2, 4)
                # Re-check: if the user already clicked Next themselves and the
                # page has changed, the next loop iteration picks it up naturally.

        return False

    # ── Workday-specific helpers ───────────────────────────────────────────────

    def _workday_click_apply(self) -> bool:
        """Click the main Apply button on the Workday job detail page."""
        for sel in [
            '[data-automation-id="applyBtn"]',
            '[data-automation-id="Apply"]',
            'a:has-text("Apply")',
            'button:has-text("Apply")',
        ]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    B.click_el(self.page, btn)
                    B.wait_for_navigation(self.page)
                    B.pause(1.5, 2.5)
                    return True
            except Exception:
                continue
        return False

    def _handle_autofill_prompt(self) -> bool:
        """
        Generic handler for 'How would you like to apply?' screens.
        ALWAYS prefers 'Autofill with Resume' — uploads resume and waits for
        the ATS to parse it and pre-fill fields. Falls back to 'Apply Manually'
        only if no autofill option is present.

        Works on Workday, generic ATS portals, and any page that offers these
        choices before the application form.
        """
        # ── Priority 1: Autofill with Resume ─────────────────────────────
        autofill_sels = [
            # Workday-specific automation IDs
            '[data-automation-id="autofillWithResume"]',
            # Text-based — match buttons/links containing any of these phrases
            'button:has-text("Autofill with Resume")',
            'button:has-text("Autofill with resume")',
            'button:has-text("Use My Resume")',
            'button:has-text("Upload Resume")',
            'button:has-text("Upload my resume")',
            'a:has-text("Autofill with Resume")',
            'a:has-text("Autofill with resume")',
            'a:has-text("Use My Resume")',
            'a:has-text("Upload Resume")',
        ]
        for sel in autofill_sels:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    self._log("Choosing 'Autofill with Resume' on intro screen")
                    B.click_el(self.page, btn)
                    B.pause(1.5, 2.0)
                    # After click, an <input type="file"> may appear — upload immediately
                    upload_file = self.resume_docx or self.resume_pdf
                    if upload_file:
                        deadline = time.time() + 8
                        while time.time() < deadline:
                            fi = self.page.query_selector('input[type="file"]')
                            if fi:
                                try:
                                    fi.set_input_files(upload_file)
                                    self._log(f"Resume uploaded for autofill: {os.path.basename(upload_file)}")
                                    self._resume_uploaded = True
                                except Exception:
                                    pass
                                break
                            time.sleep(0.4)
                    # Wait for the ATS to parse the resume and autofill fields
                    B.pause(3, 5)
                    B.wait_for_navigation(self.page)
                    B.pause(1.5, 2.5)
                    return True
            except Exception:
                continue

        # ── Priority 2: Apply Manually (fallback) ─────────────────────────
        manual_sels = [
            '[data-automation-id="applyManually"]',
            'button:has-text("Apply Manually")',
            'a:has-text("Apply Manually")',
            '[data-automation-id="createAccountLink"]:has-text("Apply Manually")',
        ]
        for sel in manual_sels:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    self._log("Choosing 'Apply Manually' on intro screen (no autofill option found)")
                    B.click_el(self.page, btn)
                    B.wait_for_navigation(self.page)
                    B.pause(1.5, 2.5)
                    return True
            except Exception:
                continue

        return False

    def _workday_handle_how_to_apply(self) -> bool:
        """Workday 'How would you like to apply?' — delegates to generic handler."""
        return self._handle_autofill_prompt()

    def _workday_handle_login_wall(self) -> bool:
        """
        Detect and bypass the Workday sign-in / create-account wall.
        Tries to fill known credentials; if unavailable, notifies user.
        Returns True if we got past it.
        """
        url = self.page.url.lower()
        is_login_page = (
            "/signin" in url or "/login" in url or
            self.page.query_selector('[data-automation-id="username"]') is not None or
            self.page.query_selector('input[type="email"][id*="email"]') is not None
        )
        if not is_login_page:
            return True  # not a login wall

        self._log("Workday login wall detected — attempting to bypass", "warn")

        # Try clicking "Apply Manually" / "Continue as Guest" if present
        for sel in [
            'button:has-text("Apply Manually")',
            'a:has-text("Apply Manually")',
            'button:has-text("Continue as guest")',
            'button:has-text("Skip sign in")',
            '[data-automation-id="skipLogin"]',
            '[data-automation-id="applyManually"]',
        ]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    self._log("Found guest/manual option — clicking")
                    B.click_el(self.page, btn)
                    B.wait_for_navigation(self.page)
                    B.pause(1.5, 2.5)
                    return True
            except Exception:
                continue

        # Try filling credentials if stored in profile
        wd_email    = self.profile.get("workday", {}).get("email",    self._p("email"))
        wd_password = self.profile.get("workday", {}).get("password", "")
        if wd_email and wd_password:
            self._log("Filling Workday credentials from profile")
            for sel in ['[data-automation-id="username"]', 'input[type="email"]']:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        B.type_into(el, wd_email)
                        break
                except Exception:
                    pass
            for sel in ['[data-automation-id="password"]', 'input[type="password"]']:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        B.type_into(el, wd_password)
                        break
                except Exception:
                    pass
            for sel in ['[data-automation-id="signIn"]', 'button:has-text("Sign In")', 'button[type="submit"]']:
                try:
                    btn = self.page.query_selector(sel)
                    if btn and btn.is_visible():
                        B.click_el(self.page, btn)
                        B.wait_for_navigation(self.page)
                        B.pause(2, 3)
                        return "/signin" not in self.page.url.lower()
                except Exception:
                    pass

        # Cannot bypass — notify user and save screenshot
        self._log("Cannot bypass Workday login wall — manual login required", "warn")
        self._save_screenshot("workday_login_wall")
        if self.bridge:
            self.bridge.notify(
                "Job Copilot — Manual Login Needed",
                f"Workday login wall at {self.page.url[:60]}"
            )
        return False

    # ── Portal-specific entry points ───────────────────────────────────────────

    def _handle_greenhouse(self, jd: str) -> bool:
        self._log("Filling Greenhouse form…")
        B.pause()
        self._upload_resume(prefer_docx=True)
        self._fill_standard()
        # Greenhouse-specific IDs
        for sel, val in [
            ("input#job_application_first_name", self._first()),
            ("input#job_application_last_name",  self._last()),
            ("input#job_application_email",       self._p("email")),
            ("input#job_application_phone",       self._p("phone")),
        ]:
            if val:
                self._type(sel, val)
        self._fill_page_inputs(jd)
        return self._walk_form(jd)

    def _handle_lever(self, jd: str) -> bool:
        self._log("Filling Lever form…")
        B.pause()
        self._upload_resume(prefer_docx=True)
        for sel, val in [
            ('input[name="name"]',             self._p("name")),
            ('input[name="email"]',            self._p("email")),
            ('input[name="phone"]',            self._p("phone")),
            ('input[name="urls[LinkedIn]"]',   self._p("linkedin")),
        ]:
            if val:
                self._type(sel, val)
        self._fill_page_inputs(jd)
        return self._walk_form(jd)

    def _handle_workday(self, jd: str) -> bool:
        self._log("Filling Workday form (multi-step)…")
        B.pause(2, 3)
        self._dismiss_overlays()

        # Step 1: If we're still on the job detail page, click Apply
        if not self.page.query_selector('[data-automation-id="bottom-navigation-next-button"]'):
            self._workday_click_apply()
            self._dismiss_overlays()

        # Step 2: Handle "How would you like to apply?" intro screen
        self._workday_handle_how_to_apply()
        self._dismiss_overlays()

        # Step 3: Handle login / register wall
        if not self._workday_handle_login_wall():
            return False

        # Step 4: Wait for the first form step to fully render
        B.pause(1.5, 2.5)
        self._dismiss_overlays()

        # Step 5: Upload resume and fill standard fields, then walk the form
        self._upload_resume()
        self._fill_standard()
        return self._walk_form(jd, max_steps=15)

    def _handle_icims(self, jd: str) -> bool:
        self._log("Filling iCIMS form…")
        B.pause()
        self._dismiss_overlays()
        self._handle_autofill_prompt()
        self._dismiss_overlays()
        self._upload_resume(prefer_docx=True)
        self._fill_standard()
        self._fill_page_inputs(jd)
        return self._walk_form(jd)

    def _handle_generic(self, jd: str) -> bool:
        self._log("Filling form (generic portal)…")
        B.pause()
        self._dismiss_overlays()
        self._handle_autofill_prompt()
        self._dismiss_overlays()
        self._upload_resume(prefer_docx=True)
        self._fill_standard()
        self._fill_page_inputs(jd)
        return self._walk_form(jd)

    # ── Main entry ────────────────────────────────────────────────────────────

    def apply(self, url: str, job_title: str, company: str, jd: str = "",
              job_meta: dict = None) -> bool:
        """
        Detect the portal, fill the form, show approval modal, and submit.
        Returns True if successfully submitted.
        """
        self._current_job = job_meta or {"title": job_title, "company": company}
        portal = detect_portal(url)
        self._log(f"Portal: {portal} — {url[:60]}…")

        # Navigate to the portal URL if not already there
        if self.page.url != url:
            self.page.goto(url)
            B.wait_for_navigation(self.page)
            B.pause(1.5, 2.5)

        # Re-read actual URL after any redirects (LinkedIn → external ATS)
        actual_url = self.page.url
        if actual_url != url:
            portal = detect_portal(actual_url)
            self._log(f"Redirected → {portal} — {actual_url[:60]}…")

        if portal == "greenhouse":
            return self._handle_greenhouse(jd)
        elif portal == "lever":
            return self._handle_lever(jd)
        elif portal == "workday":
            return self._handle_workday(jd)
        elif portal == "icims":
            return self._handle_icims(jd)
        else:
            return self._handle_generic(jd)
