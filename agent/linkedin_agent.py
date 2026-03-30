"""
LinkedIn Easy Apply Agent — stealth browser, human-like, persistent session.

What makes this work:
  - Persistent browser profile: stays logged in, no re-auth every run
  - navigator.webdriver hidden: LinkedIn can't detect Playwright
  - Human typing with random delays: looks like a real person
  - Human mouse movement before every click
  - Waits for elements properly instead of blind time.sleep()
"""

import os
import sys
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from tailor import check_relevance, tailor_resume, analyze_skills_gap, generate_cover_letter, _call_openai
from tracker import add_application, is_already_applied
from agent.pdf_generator import generate_pdf
from agent.docx_generator import generate_docx
from agent.filter_agent import LinkedInFilterAgent
from agent.portal_agent import PortalAgent, detect_portal
from agent.recorder import has_macro, load_macro, watch_and_learn, replay_search_url
from agent.browser import (
    launch, type_text, type_into, click, click_el,
    scroll_down, scroll_panel, wait_for_any, wait_for_navigation,
    pause, short_pause, micro_pause,
    check_human_takeover, reset_human_takeover, reading_countdown,
)
from user_profile import (
    get_immigration_answers, requires_clearance, requires_citizenship,
)
from prompts import LINKEDIN_MESSAGE_REPLY_PROMPT, build_answer_prompt

DATE_FILTERS = {
    "24h":   "r86400",
    "week":  "r604800",
    "month": "r2592000",
    "any":   "",
}

LINKEDIN_LOGIN = "https://www.linkedin.com/login"
LINKEDIN_JOBS  = "https://www.linkedin.com/jobs/search/"


class LinkedInAgent:

    def __init__(self, email: str, password: str, openai_client,
                 resume_text: str, profile: dict, bridge=None):
        self.email       = email
        self.password    = password
        self.client      = openai_client
        self.resume_text = resume_text
        self.profile     = profile
        self.bridge      = bridge
        self.ctx         = None   # persistent browser context
        self.page        = None
        self.playwright  = None
        self.applied     = 0
        self.skipped     = 0
        self.review_before_submit = profile.get("agent", {}).get("review_before_submit", True)
        self._current_job          = {}
        self._current_tailored     = ""
        self._current_cover_letter = ""
        self._current_ats_score    = 0

    # ── Logging ────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "info", job: dict = None):
        if self.bridge:
            self.bridge.log(msg, level=level, job=job or self._current_job, tool="LinkedIn")
        else:
            icons = {"info":"  ","success":"  ✓ ","skip":"  — ","warn":"  ⚠ ","error":"  ✗ "}
            print(f"{icons.get(level,'  ')}{msg}")

    # ── Browser ─────────────────────────────────────────────────────

    def start_browser(self):
        """
        Launch stealth Chromium with persistent profile.
        If already logged into LinkedIn, we go straight to job search.
        """
        self.playwright = sync_playwright().start()
        self.ctx  = launch(self.playwright)
        # Wait for Chrome to finish restoring the persistent session before
        # reading ctx.pages — otherwise we only see about:blank
        time.sleep(2)
        all_pages = [p for p in self.ctx.pages if not p.is_closed()]
        real_pages = [p for p in all_pages if p.url not in ("about:blank", "")]
        chosen = real_pages[0] if real_pages else (all_pages[0] if all_pages else None)
        if chosen:
            self.page = chosen
            for extra in all_pages:
                if extra is not chosen:
                    try: extra.close()
                    except Exception: pass
        else:
            self.page = self.ctx.new_page()
        self._log("Browser ready (stealth mode, persistent session)")

    def close_browser(self):
        try:
            if self.ctx:  self.ctx.close()
        except Exception: pass
        try:
            if self.playwright: self.playwright.stop()
        except Exception: pass

    # ── Login ────────────────────────────────────────────────────────

    def login(self):
        """
        Log into LinkedIn with human-like typing.
        If session cookie exists (persistent profile), may already be logged in.
        """
        self.page.goto(LINKEDIN_LOGIN)
        wait_for_navigation(self.page)

        # Already logged in?
        if "feed" in self.page.url or "mynetwork" in self.page.url:
            self._log("Already logged in — session restored from profile")
            # Briefly linger on the feed like a real user before navigating
            pause(2.0, 3.5)
            return

        self._log("Logging in…")
        type_text(self.page, "#username", self.email)
        type_text(self.page, "#password", self.password)
        pause(0.5, 1.2)

        click(self.page, 'button[type="submit"]')
        wait_for_navigation(self.page, timeout=20000)
        pause(1, 2)

        # Handle 2FA / CAPTCHA
        if any(x in self.page.url for x in ["checkpoint", "challenge", "captcha", "login"]):
            self._log("⚠  LinkedIn needs verification — complete it in the browser", "warn")
            if self.bridge:
                self.bridge.log(
                    "ACTION REQUIRED: Complete the LinkedIn verification/CAPTCHA in the browser window. "
                    "The agent will wait up to 3 minutes.",
                    level="warn", tool="LinkedIn"
                )
            # Wait up to 3 min for user to complete
            for _ in range(36):
                time.sleep(5)
                if "feed" in self.page.url or "mynetwork" in self.page.url:
                    break

        if "feed" in self.page.url or "mynetwork" in self.page.url:
            self._log("Logged in successfully")
        else:
            self._log(f"Login may have failed — current URL: {self.page.url}", "warn")

    # ── Job search ────────────────────────────────────────────────────

    def search_jobs(self, query: str, location: str,
                    remote_only: bool = False, date_filter: str = "week",
                    job_types: list = None, work_modes: list = None,
                    experience_levels: list = None, easy_apply_only: bool = False,
                    sort_by: str = "DD"):
        from urllib.parse import quote
        self._log(f"Searching: {query} · {location} · {date_filter}")

        MACRO = "linkedin_search"

        # ── Build filter URL params from dashboard selections ──────────────────
        # LinkedIn URL param reference:
        #   f_TPR  — date posted  (r86400=24h, r604800=week, r2592000=month)
        #   f_JT   — job type     (F=Full-time, C=Contract, P=Part-time, T=Temporary)
        #   f_WT   — work mode    (2=Remote, 3=Hybrid, 1=On-site)
        #   f_E    — exp level    (2=Entry, 3=Associate, 4=Mid-Senior, 5=Director)
        #   f_AL   — Easy Apply only (true)
        tpr = DATE_FILTERS.get(date_filter, "")
        parts = [
            f"keywords={quote(query)}",
            f"location={quote(location)}",
        ]
        if tpr:
            parts.append(f"f_TPR={tpr}")
        # LinkedIn requires %2C (URL-encoded comma) for multi-value params
        if job_types:
            parts.append(f"f_JT={'%2C'.join(job_types)}")
        if work_modes:
            parts.append(f"f_WT={'%2C'.join(work_modes)}")
        elif remote_only:
            parts.append("f_WT=2")
        if experience_levels:
            parts.append(f"f_E={'%2C'.join(experience_levels)}")
        if easy_apply_only:
            parts.append("f_AL=true")
        # sort_by already a named param — use directly
        parts.append(f"sortBy={sort_by}")

        search_url = f"{LINKEDIN_JOBS}?{'&'.join(parts)}"
        filter_summary = ", ".join(filter(None, [
            date_filter,
            " / ".join(job_types) if job_types else "",
            " / ".join(work_modes) if work_modes else "",
            f"levels {experience_levels}" if experience_levels else "",
            "Easy Apply only" if easy_apply_only else "",
        ]))
        self._log(f"Filters: {filter_summary or 'defaults'}")
        self._log(f"URL → {search_url}")

        # ── Navigate to filtered search URL ────────────────────────────────────
        # All filters are baked into the URL params — no UI clicking needed.
        self.page.goto(search_url)
        wait_for_navigation(self.page)
        pause(2.0, 3.0)
        self._wait_for_cards()

    def _wait_for_cards(self):
        """Block until job cards appear (up to 15s)."""
        self._log("Waiting for job listings…")
        try:
            self.page.wait_for_selector(
                ".job-card-container, [data-occludable-job-id], "
                ".jobs-search-results__list-item",
                timeout=15000,
            )
        except Exception:
            self._log("Job cards slow to load — continuing anyway", "warn")

    # ── Left-panel scroll helpers ──────────────────────────────────────────────

    def _left_panel_coords(self) -> tuple:
        """
        Return the centre (x, y) of LinkedIn's left job-list panel.
        Tries to locate the actual container element; falls back to a
        safe fixed coordinate on the left third of a 1366×768 viewport.
        """
        for sel in [
            ".jobs-search-results-list",
            ".scaffold-layout__list",
            ".jobs-search__results-list",
        ]:
            try:
                el = self.page.query_selector(sel)
                if el:
                    box = el.bounding_box()
                    if box:
                        return (
                            box["x"] + box["width"]  / 2,
                            box["y"] + box["height"] / 2,
                        )
            except Exception:
                continue
        return (300, 400)   # safe default: left third of 1366px viewport

    def _scroll_to_card(self, card) -> None:
        """
        Smoothly scroll the left-panel list so `card` is in the viewport.
        Called before clicking each card so LinkedIn's lazy-loader triggers.
        """
        try:
            box = card.bounding_box()
            if not box:
                return
            px, py = self._left_panel_coords()
            # Scroll by the card's distance from the safe viewport centre (350px)
            offset = box["y"] - 350
            if abs(offset) > 60:
                scroll_panel(self.page, px, py,
                             total_px=int(offset), reading=False)
                short_pause()
        except Exception:
            pass

    def _scroll_for_next_card(self, seen: set) -> bool:
        """
        Hover over the left panel and scroll DOWN in small increments,
        waiting after each step for LinkedIn to lazy-load new card elements.

        Returns True if at least one unseen card appeared within 5 seconds.
        Returns False if no new cards loaded (end of page reached).
        """
        px, py = self._left_panel_coords()

        def _unseen_count():
            try:
                return sum(
                    1 for c in self.get_job_cards()
                    if (
                        c.get_attribute("data-job-id") or
                        c.get_attribute("data-occludable-job-id") or ""
                    ) not in seen
                )
            except Exception:
                return 0

        # Scroll in three steps of ~300px each, waiting between steps
        for _ in range(3):
            before = _unseen_count()
            scroll_panel(self.page, px, py, total_px=320)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if _unseen_count() > before:
                    return True
                time.sleep(0.3)

        return _unseen_count() > 0   # last-chance check

    def _click_next_page(self) -> bool:
        """
        Find and click LinkedIn's Next Page pagination button.
        Waits for the new page's job cards to render before returning.
        Returns True if navigation succeeded.
        """
        self._log("End of page — looking for Next Page…")
        next_sels = [
            'button[aria-label="View next page"]',
            'button[aria-label*="next page" i]',
            'button.jobs-search-pagination__button--next',
            'li.artdeco-pagination__indicator--number.selected + li button',
            '[data-test-pagination-page-btn="next"]',
        ]
        for sel in next_sels:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    scroll_into_view(self.page, btn)
                    short_pause()
                    click_el(self.page, btn)
                    wait_for_navigation(self.page)
                    pause(2.5, 3.5)
                    self._wait_for_cards()
                    self._log("Moved to next page")
                    return True
            except Exception:
                continue
        return False

    def _click_filter_option(self, page, option_text: str) -> bool:
        """Click a visible dropdown option by its text."""
        for sel in [
            f'li[role="option"]:has-text("{option_text}")',
            f'div[role="option"]:has-text("{option_text}")',
            f'span:has-text("{option_text}")',
            f'label:has-text("{option_text}")',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    click_el(page, el)
                    return True
            except Exception:
                continue
        return False

    def _apply_ui_filters(self, sort_by="DD", date_filter="week",
                          job_types=None, work_modes=None,
                          experience_levels=None, easy_apply_only=False):
        """
        Click LinkedIn's actual filter UI after URL navigation.
        URL params set the initial query; UI clicks confirm selection visually.
        """
        p = self.page
        job_types        = job_types or []
        work_modes       = work_modes or []
        experience_levels = experience_levels or []

        # ── Sort by ────────────────────────────────────────────────────────────
        sort_label = "Most recent" if sort_by == "DD" else "Most relevant"
        try:
            sort_btn = None
            for sel in [
                'button.jobs-search-results-list__list-options--sort-button',
                'button[aria-label*="Sort by"]',
                'button:has-text("Most relevant")',
                'button:has-text("Most recent")',
                'button:has-text("Sort by")',
            ]:
                try:
                    b = p.query_selector(sel)
                    if b and b.is_visible():
                        sort_btn = b
                        break
                except Exception:
                    continue
            if sort_btn:
                click_el(p, sort_btn)
                pause(0.6, 1.0)
                self._click_filter_option(p, sort_label)
                pause(1.0, 1.5)
                self._log(f"Sort → {sort_label}")
        except Exception:
            pass

        # ── Date posted ────────────────────────────────────────────────────────
        DATE_LABELS = {"24h": "Past 24 hours", "week": "Past week",
                       "month": "Past month", "any": "Any time"}
        date_label = DATE_LABELS.get(date_filter, "Past week")
        try:
            date_btn = None
            for sel in [
                'button[aria-label*="Date posted"]',
                'button:has-text("Date posted")',
                'button:has-text("Past week")',
                'button:has-text("Past 24 hours")',
                'button:has-text("Past month")',
                'button:has-text("Any time")',
            ]:
                try:
                    b = p.query_selector(sel)
                    if b and b.is_visible():
                        date_btn = b
                        break
                except Exception:
                    continue
            if date_btn:
                click_el(p, date_btn)
                pause(0.6, 1.0)
                self._click_filter_option(p, date_label)
                # Click "Show results" if modal
                for sel in ['button:has-text("Show results")', 'button:has-text("Apply")']:
                    try:
                        b = p.query_selector(sel)
                        if b and b.is_visible():
                            click_el(p, b)
                            break
                    except Exception:
                        continue
                pause(1.0, 1.5)
                self._log(f"Date → {date_label}")
        except Exception:
            pass

        # ── Job type ───────────────────────────────────────────────────────────
        JT_LABELS = {"F": "Full-time", "C": "Contract", "P": "Part-time",
                     "T": "Temporary", "I": "Internship"}
        if job_types:
            try:
                jt_btn = None
                for sel in ['button[aria-label*="Job type"]', 'button:has-text("Job type")']:
                    try:
                        b = p.query_selector(sel)
                        if b and b.is_visible():
                            jt_btn = b
                            break
                    except Exception:
                        continue
                if jt_btn:
                    click_el(p, jt_btn)
                    pause(0.6, 1.0)
                    for jt in job_types:
                        self._click_filter_option(p, JT_LABELS.get(jt, jt))
                        pause(0.3, 0.5)
                    for sel in ['button:has-text("Show results")', 'button:has-text("Apply")']:
                        try:
                            b = p.query_selector(sel)
                            if b and b.is_visible():
                                click_el(p, b)
                                break
                        except Exception:
                            continue
                    pause(1.0, 1.5)
                    self._log(f"Job type → {', '.join(JT_LABELS.get(j,j) for j in job_types)}")
            except Exception:
                pass

        # ── Work mode (Remote / Hybrid / On-site) ─────────────────────────────
        WT_LABELS = {"2": "Remote", "3": "Hybrid", "1": "On-site"}
        if work_modes:
            try:
                wt_btn = None
                for sel in ['button[aria-label*="Remote"]', 'button:has-text("Remote")']:
                    try:
                        b = p.query_selector(sel)
                        if b and b.is_visible():
                            wt_btn = b
                            break
                    except Exception:
                        continue
                if wt_btn:
                    click_el(p, wt_btn)
                    pause(0.6, 1.0)
                    for wm in work_modes:
                        self._click_filter_option(p, WT_LABELS.get(wm, wm))
                        pause(0.3, 0.5)
                    for sel in ['button:has-text("Show results")', 'button:has-text("Apply")']:
                        try:
                            b = p.query_selector(sel)
                            if b and b.is_visible():
                                click_el(p, b)
                                break
                        except Exception:
                            continue
                    pause(1.0, 1.5)
                    self._log(f"Work mode → {', '.join(WT_LABELS.get(w,w) for w in work_modes)}")
            except Exception:
                pass

        # ── Easy Apply toggle ─────────────────────────────────────────────────
        if easy_apply_only:
            try:
                for sel in ['button[aria-label*="Easy Apply"]', 'button:has-text("Easy Apply")']:
                    b = p.query_selector(sel)
                    if b and b.is_visible():
                        click_el(p, b)
                        pause(1.0, 1.5)
                        self._log("Filter: Easy Apply only")
                        break
            except Exception:
                pass

        # Wait for results to refresh after filter clicks
        pause(1.5, 2.0)
        self._wait_for_cards()

    # ── Job cards ──────────────────────────────────────────────────────

    def get_job_cards(self):
        return self.page.query_selector_all(
            ".job-card-container, .jobs-search-results__list-item, "
            "[data-job-id], [data-occludable-job-id]"
        )

    def get_job_details(self) -> dict:
        d = {"title": "Unknown Role", "company": "Unknown Company",
             "jd": "", "location": ""}
        title_sels = [
            ".job-details-jobs-unified-top-card__job-title h1",
            ".jobs-unified-top-card__job-title h1",
            "h1.t-24.t-bold.inline",
            "h1.jobs-unified-top-card__job-title",
        ]
        company_sels = [
            ".job-details-jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__subtitle-primary-grouping a",
        ]
        jd_sels = [
            "#job-details",
            ".jobs-description__content .jobs-description-content__text",
            ".jobs-description-content",
            ".job-view-layout",
        ]
        loc_sels = [
            ".job-details-jobs-unified-top-card__primary-description-without-tagline",
            ".jobs-unified-top-card__bullet",
        ]
        for key, sels in [("title", title_sels), ("company", company_sels),
                           ("jd", jd_sels), ("location", loc_sels)]:
            for sel in sels:
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        d[key] = el.inner_text().strip()
                        if d[key]:
                            break
                except Exception:
                    continue
        return d

    def _scroll_jd(self):
        """
        Scroll the job description panel up and down like a human reading it.
        Helps avoid bot detection and ensures the full JD is loaded.
        """
        jd_selectors = [
            "#job-details",
            ".jobs-description-content",
            ".jobs-description__content",
            ".job-view-layout",
        ]
        scroll_script = """
            (function(sel, delta) {
                var el = document.querySelector(sel);
                if (el) { el.scrollTop += delta; return true; }
                window.scrollBy(0, delta);
                return false;
            })(arguments[0], arguments[1])
        """
        sel = jd_selectors[0]
        for s in jd_selectors:
            try:
                if self.page.query_selector(s):
                    sel = s
                    break
            except Exception:
                continue

        # Hover over the JD panel (right side of LinkedIn's split layout) and
        # scroll down with reading-pace pauses, then scroll back up.
        JD_X, JD_Y = 950, 400   # approximate centre of the JD pane at 1366×768
        scroll_panel(self.page, JD_X, JD_Y, total_px=900, reading=True)
        pause(0.5, 1.0)   # "finished reading" pause
        scroll_panel(self.page, JD_X, JD_Y, total_px=-700, reading=False)
        pause(0.3, 0.6)

    # ── Form helpers ───────────────────────────────────────────────────

    def _get_label(self, el) -> str:
        try:
            eid = el.get_attribute("id")
            if eid:
                lbl = self.page.query_selector(f'label[for="{eid}"]')
                if lbl: return lbl.inner_text().strip()
        except Exception: pass
        try:
            aria = el.get_attribute("aria-label")
            if aria: return aria.strip()
        except Exception: pass
        return ""

    def _ai_answer(self, question: str, input_type: str = "text") -> str:
        """Ask GPT to answer a form question, fall back to empty."""
        try:
            prompt = build_answer_prompt(question, input_type, "", self.profile)
            answer = _call_openai(self.client, prompt, max_tokens=150)
            if answer.strip() == "ASK_USER":
                self._log(f"Form question (AI best-effort): {question}", "warn")
                return ""
            return answer
        except Exception:
            return ""

    def _ai_pick_option(self, question: str, options: list) -> str:
        opts_txt = "Options:\n" + "\n".join(f"- {o}" for o in options)
        prompt = build_answer_prompt(question, "select", opts_txt, self.profile)
        try:
            answer = _call_openai(self.client, prompt, max_tokens=60).strip()
            for opt in options:
                if answer.lower() in opt.lower():
                    return opt
            return options[-1] if options else ""
        except Exception:
            return options[-1] if options else ""

    # ── Fill one Easy Apply page ───────────────────────────────────────

    def _fill_page(self, upload_resume: str) -> str:
        """Fill all form fields on the current Easy Apply modal step."""
        # Bail immediately if stop was requested
        if self.bridge and not self.bridge.is_running:
            return "done"
        short_pause()
        personal = self.profile.get("personal", {})
        _warned_questions: set = set()  # deduplicate per-step warnings

        # ── Resume upload ──────────────────────────────────────────────
        fi = self.page.query_selector('input[type="file"]')
        if fi and upload_resume and os.path.exists(upload_resume):
            try:
                fi.set_input_files(upload_resume)
                short_pause()
                self._log(f"Uploaded resume: {os.path.basename(upload_resume)}")
            except Exception as _ue:
                self._log(f"Resume upload error: {_ue}", "warn")

        # ── Phone number (LinkedIn has country-code dropdown + number field) ──
        phone = personal.get("phone", "")
        if phone:
            for sel in [
                'input[id*="phoneNumber"]',
                'input[name*="phoneNumber"]',
                'input[aria-label*="Phone"]',
                'input[aria-label*="phone"]',
                'input[name*="phone"]',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible() and not el.input_value():
                        type_into(el, phone)
                        break
                except Exception:
                    continue

        # ── All text / number inputs inside the modal ──────────────────
        modal = (
            self.page.query_selector(".jobs-easy-apply-modal")
            or self.page.query_selector('[data-test-modal]')
            or self.page   # fallback to full page
        )

        for inp in modal.query_selector_all('input[type="text"], input[type="number"]'):
            try:
                if not inp.is_visible():
                    continue
                if inp.input_value():
                    continue
                label = self._get_label(inp)
                if not label:
                    continue
                # skip fields already handled or that LinkedIn pre-fills
                skip_kws = ["phone", "name", "first", "last", "city", "state",
                            "zip", "postal", "country", "address"]
                if any(k in label.lower() for k in skip_kws):
                    continue
                inp_type = inp.get_attribute("type") or "text"
                answer   = self._ai_answer(label, inp_type)
                if not answer and inp_type == "number":
                    # Default unknown numeric experience fields to 0
                    answer = "0"
                    if label not in _warned_questions:
                        _warned_questions.add(label)
                        self._log(f"Numeric field defaulted to 0: {label}", "warn")
                if answer:
                    type_into(inp, answer)
                    micro_pause()
            except Exception:
                continue

        # ── Textareas ──────────────────────────────────────────────────
        for ta in modal.query_selector_all("textarea"):
            try:
                if not ta.is_visible():
                    continue
                if ta.input_value():
                    continue
                label = self._get_label(ta) or "additional information"
                answer = self._ai_answer(label, "textarea")
                if answer:
                    type_into(ta, answer)
                    micro_pause()
            except Exception:
                continue

        # ── Selects / dropdowns ────────────────────────────────────────
        for sel_el in modal.query_selector_all("select"):
            try:
                if not sel_el.is_visible():
                    continue
                label = self._get_label(sel_el)
                opts  = [
                    o.inner_text().strip()
                    for o in sel_el.query_selector_all("option")
                    if o.get_attribute("value")
                ]
                if not opts:
                    continue
                pick = self._ai_pick_option(label, opts)
                sel_el.select_option(label=pick)
                short_pause()
            except Exception:
                continue

        # ── Radio button groups ────────────────────────────────────────
        for group in modal.query_selector_all("fieldset"):
            try:
                if not group.is_visible():
                    continue
                legend = group.query_selector("legend")
                question = legend.inner_text().strip() if legend else ""
                if not question:
                    continue
                answer = self._ai_answer(question, "yes/no radio")
                for radio in group.query_selector_all('input[type="radio"]'):
                    rl = self._get_label(radio)
                    if rl and answer.lower() in rl.lower():
                        click_el(self.page, radio)
                        break
            except Exception:
                continue

        short_pause()

        # ── Detect next button (give it up to 10s) ────────────────────
        _, el = wait_for_any(self.page, [
            'button[aria-label="Submit application"]',
            'button[aria-label="Review your application"]',
            'button[aria-label="Continue to next step"]',
            'button[aria-label*="Submit"]',
            'button[aria-label*="Review"]',
            'button[aria-label*="Continue"]',
            # LinkedIn sometimes uses "Next" instead of "Continue to next step"
            'button[aria-label="Next"]',
            'button[aria-label*="Next"]',
            # Footer action buttons inside the modal
            '.jobs-easy-apply-modal footer button[type="button"]',
            '.jobs-easy-apply-modal .artdeco-button--primary',
        ], timeout=10000)

        if el:
            label = (el.get_attribute("aria-label") or "").lower()
            text  = (el.inner_text() or "").lower().strip()
            self._log(f"Modal button: aria-label='{label}' text='{text}'")
            if "submit"   in label or "submit"   in text: return "submit"
            if "review"   in label or "review"   in text: return "review"
            if "continue" in label or "continue" in text: return "next"
            if "next"     in label or "next"     in text: return "next"
            # Primary modal button that doesn't match above — treat as Next
            return "next"

        self._log("No modal nav button found — timeout", "warn")
        return "done"

    # ── Handle full Easy Apply flow ────────────────────────────────────

    def handle_easy_apply(self, resume_pdf: str, resume_docx: str,
                          job_title: str = "", company: str = "") -> bool:
        # Prefer DOCX for upload; fall back to PDF only if DOCX unavailable
        upload_resume = resume_docx or resume_pdf
        # Wait for modal to fully render before starting
        pause(1.5, 2.5)
        for _page_num in range(15):
            # Stop immediately if user requested it
            if self.bridge and not self.bridge.is_running:
                return False

            action = self._fill_page(upload_resume)

            if action == "submit":
                if self.review_before_submit:
                    if self.bridge:
                        approved = self.bridge.request_approval(
                            self._current_job, self._current_tailored,
                            cover_letter=self._current_cover_letter,
                            ats_score=self._current_ats_score,
                        )
                    else:
                        approved = input(f"\n  Submit {job_title} @ {company}? (y/n): ").lower() == "y"

                    if not approved:
                        self._log("Submission cancelled", "skip")
                        try:
                            dismiss = self.page.query_selector('button[aria-label="Dismiss"]')
                            if dismiss: click_el(self.page, dismiss)
                        except Exception:
                            pass
                        return False

                # Re-query after approval wait — DOM reference may be stale
                submit_btn = None
                for _ssel in [
                    'button[aria-label="Submit application"]',
                    'button[aria-label*="Submit"]',
                    '.jobs-easy-apply-modal button[aria-label*="Submit"]',
                ]:
                    submit_btn = self.page.query_selector(_ssel)
                    if submit_btn and submit_btn.is_visible():
                        break
                if not submit_btn:
                    self._log("Submit button gone — LinkedIn modal timed out", "warn")
                    return False

                click_el(self.page, submit_btn)
                pause(1.5, 2.5)
                self._log(f"Submitted — {job_title} @ {company}", "success")
                return True

            elif action == "review":
                btn = self.page.query_selector('button[aria-label="Review your application"]')
                if btn: click_el(self.page, btn)

            elif action == "next":
                btn = None
                for _sel in [
                    'button[aria-label="Continue to next step"]',
                    'button[aria-label*="Continue"]',
                    'button[aria-label="Next"]',
                    'button[aria-label*="Next"]',
                    '.jobs-easy-apply-modal .artdeco-button--primary',
                    '.jobs-easy-apply-modal footer button[type="button"]',
                ]:
                    btn = self.page.query_selector(_sel)
                    if btn and btn.is_visible():
                        break
                if btn:
                    click_el(self.page, btn)
                else:
                    break  # No continue button found — stuck, bail out

            else:
                break

        return False

    # ── LinkedIn messages ──────────────────────────────────────────────

    def check_messages(self) -> list:
        self.page.goto("https://www.linkedin.com/messaging/")
        wait_for_navigation(self.page)
        pause(1.5, 2.5)
        messages = []
        try:
            for convo in self.page.query_selector_all(".msg-conversation-listitem")[:10]:
                if not convo.query_selector(".notification-badge"): continue
                click_el(self.page, convo)
                pause(0.8, 1.5)
                msg_el    = self.page.query_selector(".msg-s-message-list__event:last-child .msg-s-event-listitem__body")
                sender_el = self.page.query_selector(".msg-entity-lockup__entity-title")
                if msg_el and sender_el:
                    messages.append({
                        "sender":  sender_el.inner_text().strip(),
                        "message": msg_el.inner_text().strip(),
                    })
        except Exception as e:
            self._log(f"Message check error: {e}", "warn")
        return messages

    def draft_reply(self, message: str) -> str:
        prompt = LINKEDIN_MESSAGE_REPLY_PROMPT.format(message=message)
        return _call_openai(self.client, prompt, max_tokens=150)

    def send_linkedin_message(self, text: str) -> bool:
        """Type and send a reply in the currently-open LinkedIn conversation."""
        try:
            compose = self.page.query_selector(
                '.msg-form__contenteditable[contenteditable="true"]'
            )
            if not compose:
                return False
            click_el(self.page, compose)
            type_into(compose, text)
            short_pause()
            send_btn = self.page.query_selector('button.msg-form__send-button')
            if send_btn:
                click_el(self.page, send_btn)
                pause(0.8, 1.2)
                return True
        except Exception as e:
            self._log(f"Send message error: {e}", "warn")
        return False

    # ── Main run loop ──────────────────────────────────────────────────

    def run(self, query: str = "Java Full Stack Engineer",
            location: str = "United States",
            max_applications: int = 30,
            remote_only: bool = False,
            date_filter: str = "week",
            job_types: list = None,
            work_modes: list = None,
            experience_levels: list = None,
            easy_apply_only: bool = False,
            sort_by: str = "DD",
            keep_alive: bool = False):

        imm            = get_immigration_answers(self.profile)
        skip_clearance = imm.get("skip_clearance", True)
        skills_to_learn = []

        self.start_browser()
        try:
            self.login()
            # Filters are baked into the search URL — no UI clicking needed
            self.search_jobs(
                query, location, remote_only, date_filter,
                job_types=job_types,
                work_modes=work_modes,
                experience_levels=experience_levels,
                easy_apply_only=easy_apply_only,
                sort_by=sort_by,
            )

            seen = set()

            while self.applied < max_applications:
                # Respect stop request
                if self.bridge and not self.bridge.is_running:
                    self._log("Stopped by user", "warn")
                    break

                cards = self.get_job_cards()
                if not cards:
                    self._log("No job cards found", "warn")
                    break

                for card in cards:
                    if self.applied >= max_applications:
                        break
                    if self.bridge and not self.bridge.is_running:
                        break
                    try:
                        job_id = (
                            card.get_attribute("data-job-id") or
                            card.get_attribute("data-occludable-job-id")
                        )
                        # Fast-skip by job_id if known
                        if job_id and job_id in seen:
                            continue

                        # Scroll the left panel to bring this card into view,
                        # then click it to load the job description on the right.
                        self._scroll_to_card(card)
                        click_el(self.page, card)
                        pause(1.5, 2.5)

                        d       = self.get_job_details()
                        title   = d["title"]
                        company = d["company"]
                        jd      = d["jd"]
                        if not jd: continue

                        # Deduplicate — always track both job_id AND title|company
                        title_key = f"{title}|{company}"
                        job_key   = job_id or title_key
                        if job_key in seen or title_key in seen:
                            continue
                        seen.add(job_key)
                        seen.add(title_key)
                        if job_id:
                            seen.add(job_id)

                        job_ref = {"title": title, "company": company}
                        self._log(f"{title} @ {company}", job=job_ref)

                        # Human-like: scroll through JD while "reading" it
                        self._scroll_jd()

                        # Already applied?
                        if is_already_applied(company, title):
                            self._log("Already applied — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Immigration filters
                        if skip_clearance and requires_clearance(jd):
                            self._log("Requires clearance — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        if skip_clearance and requires_citizenship(jd):
                            self._log("US citizens / GC only — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # ── Step 1: Scan JD ───────────────────────────────────
                        if self.bridge:
                            self.bridge.tailor_progress([
                                {"label": "Scanning job description…", "active": True,  "done": False},
                                {"label": "Scoring relevance",         "active": False, "done": False},
                                {"label": "Tailoring resume",          "active": False, "done": False},
                                {"label": "Generating PDF / DOCX",     "active": False, "done": False},
                            ], job_ref)

                        rel = check_relevance(self.client, jd)

                        # ── Step 2: Relevance result ───────────────────────────
                        score_label = f"Score {rel['score']}/10 — {rel['decision']}: {rel['reason'][:80]}"
                        if self.bridge:
                            self.bridge.tailor_progress([
                                {"label": "Scanned job description",   "active": False, "done": True},
                                {"label": score_label,                 "active": False, "done": True},
                                {"label": "Tailoring resume",          "active": False, "done": False},
                                {"label": "Generating PDF / DOCX",     "active": False, "done": False},
                            ], job_ref)
                        self._log(f"[{rel['score']}/10] {rel['decision']} — {rel['reason']}", job=job_ref)

                        # ── Effort-based filtering ─────────────────────────────
                        # Low  (≥4): apply to APPLY, APPLY_LEARN, MAYBE
                        # Med  (≥6): apply to APPLY, APPLY_LEARN only
                        # High (≥8): apply to APPLY only
                        effort    = getattr(self.bridge, "effort", "med") if self.bridge else "med"
                        min_score = {"low": 4, "med": 6, "high": 8}.get(effort, 6)
                        decision  = rel["decision"]

                        # Always skip SKIP decisions
                        if decision == "SKIP":
                            self._log("AI decided SKIP — moving on", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # MAYBE: only proceed on Low effort
                        if decision == "MAYBE" and effort != "low":
                            self._log(f"MAYBE at {effort} effort — skipping", "skip", job_ref)
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        # Score below threshold
                        if rel["score"] < min_score:
                            self._log(
                                f"Score {rel['score']}/10 below {effort} threshold ({min_score}) — skipping",
                                "skip", job_ref
                            )
                            self.skipped += 1
                            if self.bridge: self.bridge.inc_skipped()
                            continue

                        if rel["decision"] == "APPLY_LEARN":
                            missing = rel.get("learn", "")
                            if missing and missing.lower() != "none":
                                self._log(f"APPLY_LEARN — will study: {missing}", job=job_ref)
                                skills_to_learn.append({"job": f"{title} @ {company}", "learn": missing})

                        # ── Step 3: Tailor resume ──────────────────────────────
                        if self.bridge:
                            self.bridge.tailor_progress([
                                {"label": "Scanned job description",   "active": False, "done": True},
                                {"label": score_label,                 "active": False, "done": True},
                                {"label": "Tailoring resume…",         "active": True,  "done": False},
                                {"label": "Generating PDF / DOCX",     "active": False, "done": False},
                            ], job_ref)

                        tailored = tailor_resume(self.client, self.resume_text, jd)
                        self._current_job = {
                            "title": title, "company": company,
                            "location": d.get("location", ""),
                            "jd": jd[:3000], "score": rel.get("score", 0),
                        }
                        self._current_tailored = tailored

                        # ── Step 4: Generate docs ──────────────────────────────
                        if self.bridge:
                            self.bridge.tailor_progress([
                                {"label": "Scanned job description",   "active": False, "done": True},
                                {"label": score_label,                 "active": False, "done": True},
                                {"label": "Resume tailored",           "active": False, "done": True},
                                {"label": "Generating DOCX…",          "active": True,  "done": False},
                            ], job_ref)

                        # Save to output/resumes/ — same filename as user's uploaded resume
                        _dir    = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "output", "resumes"
                        )
                        _name_file = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "resume_name.txt"
                        )
                        if os.path.exists(_name_file):
                            with open(_name_file, "r", encoding="utf-8") as _nf:
                                _stem = _nf.read().strip() or "resume"
                        else:
                            _stem = "resume"
                        os.makedirs(_dir, exist_ok=True)
                        _txt_path = os.path.join(_dir, f"{_stem}.txt")
                        with open(_txt_path, "w", encoding="utf-8") as _f:
                            _f.write(tailored)
                        # Always generate DOCX (primary); PDF only as backup
                        resume_docx = generate_docx(tailored, os.path.join(_dir, f"{_stem}.docx"))
                        resume_pdf  = None   # Not generated by default

                        # Skills gap analysis + ATS score — saved as sidecar JSON
                        try:
                            import json as _json
                            skills = analyze_skills_gap(self.client, jd, tailored)
                            self._current_ats_score = skills.get("score", 0)
                            with open(os.path.join(_dir, f"{_stem}.json"), "w", encoding="utf-8") as _jf:
                                _json.dump(skills, _jf)
                        except Exception:
                            skills = {}
                            self._current_ats_score = 0

                        # Cover letter
                        try:
                            self._current_cover_letter = generate_cover_letter(
                                self.client, tailored, jd, title, company
                            )
                        except Exception:
                            self._current_cover_letter = ""

                        self._log(f"Resume saved → output/resumes/{_stem}.docx", job=job_ref)

                        # ── Step 5: All done — show resume preview ─────────────
                        if self.bridge:
                            self.bridge.tailor_progress([
                                {"label": "Scanned job description",   "active": False, "done": True},
                                {"label": score_label,                 "active": False, "done": True},
                                {"label": "Resume tailored",           "active": False, "done": True},
                                {"label": "DOCX resume saved",          "active": False, "done": True},
                            ], job_ref, resume_preview=tailored,
                               ats_score=self._current_ats_score)

                        # Pass resume_pdf=None — we only generate DOCX by default
                        # (handle_easy_apply uses docx for upload; pdf stays None)


                        # LinkedIn has two apply button types:
                        #   Easy Apply  → button, opens modal on same page
                        #   Apply ↗     → button or <a>, opens new tab (external ATS)
                        apply_btn   = None
                        is_easy     = False
                        for asel, easy in [
                            ('button[aria-label*="Easy Apply"]',        True),
                            ('.jobs-apply-button[aria-label*="Easy"]',   True),
                            ('button[aria-label*="Apply to"]',           False),
                            ('a[aria-label*="Apply to"]',                False),
                            ('button.jobs-apply-button',                 False),
                            ('a.jobs-apply-button',                      False),
                            ('button[aria-label*="Apply"]',              False),
                            ('.jobs-s-apply button',                     False),
                            ('.jobs-s-apply a',                          False),
                        ]:
                            try:
                                btn = self.page.query_selector(asel)
                                if btn and btn.is_visible():
                                    apply_btn = btn
                                    is_easy   = easy
                                    self._log(
                                        f"{'Easy Apply' if easy else 'Apply (external)'} "
                                        f"button found [{asel}]",
                                        job=job_ref
                                    )
                                    break
                            except Exception:
                                continue

                        if not apply_btn:
                            self._log("No Apply button visible — skipping", "skip", job_ref)
                            continue

                        # Plan mode: show intent, don't actually click
                        permission = getattr(self.bridge, "permission", "ask") if self.bridge else "ask"
                        if permission == "plan":
                            self._log(
                                f"[Plan] Would {'Easy Apply' if is_easy else 'Apply (external)'} → "
                                f"{title} @ {company}",
                                "info", job_ref
                            )
                            continue

                        # ── Human-takeover check ────────────────────────────────
                        # Reset detector before we read the JD so any scroll/click
                        # the user does while reading is picked up.
                        reset_human_takeover(self.page)

                        # ── Reading pause + human-takeover guard ────────────────
                        # Give the user READING_PAUSE seconds to look at the job.
                        # If they interact with the page OR press a key → leave the
                        # tab open and move on without applying.
                        _action_label = "Easy Apply" if is_easy else "Apply (external)"
                        _countdown = reading_countdown(
                            f"{title} @ {company}",
                            action=_action_label,
                            bridge=self.bridge,
                        )
                        if _countdown in ("skip", "pause"):
                            self._log(
                                f"User took control — skipping apply, tab left open",
                                "skip", job_ref,
                            )
                            continue   # don't close the tab; move to next card

                        if check_human_takeover(self.page):
                            self._log(
                                "Human interaction detected — leaving tab open, skipping apply",
                                "skip", job_ref,
                            )
                            continue

                        # ── Step 1: Click Apply — capture new tab immediately ──
                        new_tab   = None
                        active_page = None          # set in steps 2/3 below
                        pre_click_url = self.page.url
                        try:
                            with self.page.expect_popup(timeout=6000) as popup_info:
                                click_el(self.page, apply_btn)
                            new_tab = popup_info.value
                        except PWTimeout:
                            pass  # No popup — Easy Apply modal OR same-tab nav

                        # ── Step 2: Check CURRENT PAGE for modal interstitial ──
                        # LinkedIn sometimes shows "You are leaving LinkedIn" as an
                        # overlay. Note: :has-text() is NOT supported by query_selector
                        # in modern Playwright — use aria-label / data-* selectors only.
                        if new_tab is None:
                            pause(1.5, 2.0)

                            # Check if the current page already navigated away (same-tab external)
                            if self.page.url != pre_click_url and "linkedin.com" not in self.page.url:
                                self._log(f"Same-tab external nav → {self.page.url[:70]}", job=job_ref)
                                # Treat current page as the external portal
                                active_page = self.page
                                # We'll handle this below — skip remaining interstitial logic
                            else:
                                # Look for "Leaving LinkedIn" interstitial buttons
                                _interstitial_found = False
                                _cont_sels = [
                                    'button[data-tracking-control-name*="external"]',
                                    'button[data-tracking-control-name*="apply"]',
                                    # aria-label based
                                    'button[aria-label*="Continue"]',
                                    'button[aria-label*="Apply"]',
                                    # Generic modal action buttons
                                    '.artdeco-modal__actionbar button',
                                    '[data-test-modal-id] button',
                                ]
                                for _sel in _cont_sels:
                                    try:
                                        _btns = self.page.query_selector_all(_sel)
                                        for _btn in _btns:
                                            _btn_text = (_btn.inner_text() or "").lower().strip()
                                            if not _btn.is_visible():
                                                continue
                                            if any(w in _btn_text for w in ("continue", "apply", "proceed")):
                                                self._log(f"Interstitial — clicking '{_btn_text}'", job=job_ref)
                                                try:
                                                    with self.page.expect_popup(timeout=12000) as popup_info:
                                                        click_el(self.page, _btn)
                                                    new_tab = popup_info.value
                                                except PWTimeout:
                                                    pause(3.0, 4.0)
                                                    # Check for same-tab nav after interstitial click
                                                    if "linkedin.com" not in self.page.url:
                                                        active_page = self.page
                                                _interstitial_found = True
                                                break
                                        if _interstitial_found:
                                            break
                                    except Exception:
                                        continue

                        # ── Step 3: Handle interstitial INSIDE the new tab ─────
                        # LinkedIn opens a new tab at linkedin.com/jobs/view/externalApply/...
                        # That page shows "You are leaving LinkedIn" — need to click Continue there.
                        # active_page may already be set to self.page if same-tab nav detected in Step 2
                        if active_page is None:
                            active_page = self.page
                        if new_tab:
                            new_tab.bring_to_front()
                            pause(2.0, 3.0)  # Let the new tab load

                            if "linkedin.com" in new_tab.url:
                                self._log("Interstitial inside new tab — clicking Continue", job=job_ref)
                                # Iterate all buttons/links and click the one that says continue/apply
                                for _el in (new_tab.query_selector_all('button, a') or []):
                                    try:
                                        _t = (_el.inner_text() or "").lower().strip()
                                        if _el.is_visible() and any(w in _t for w in ("continue", "apply", "proceed")):
                                            click_el(new_tab, _el)
                                            break
                                    except Exception:
                                        continue
                                # Also try data-tracking attribute
                                for _sel in ['button[data-tracking-control-name*="apply"]',
                                             'a[data-tracking-control-name*="apply"]']:
                                    try:
                                        _btn = new_tab.query_selector(_sel)
                                        if _btn and _btn.is_visible():
                                            click_el(new_tab, _btn)
                                            break
                                    except Exception:
                                        continue

                            # Wait up to 20s for redirect chain to leave LinkedIn
                            for _ in range(40):
                                _url = new_tab.url
                                if _url not in ("about:blank", "") and "linkedin.com" not in _url:
                                    break
                                time.sleep(0.5)
                            try:
                                wait_for_navigation(new_tab)
                            except Exception:
                                pass
                            pause(2.0, 3.0)
                            active_page = new_tab
                            self._log(f"External tab → {new_tab.url[:70]}", job=job_ref)
                        elif "linkedin.com" not in self.page.url:
                            active_page = self.page
                            self._log(f"Same-tab external → {self.page.url[:70]}", job=job_ref)

                        # ── Decide: Easy Apply modal or external portal? ───────
                        current_url = active_page.url
                        has_modal   = (
                            active_page.query_selector(".jobs-easy-apply-modal") is not None or
                            active_page.query_selector('[data-test-modal-id="easy-apply-modal"]') is not None
                        )
                        is_external = (
                            "linkedin.com" not in current_url and current_url not in ("about:blank", "")
                        ) or (
                            not has_modal and not is_easy and current_url not in ("about:blank", "")
                        )

                        self._log(
                            f"→ {'External ATS' if is_external else 'Easy Apply modal'}",
                            job=job_ref
                        )

                        if is_external:
                            if "linkedin.com" in current_url or current_url in ("about:blank", ""):
                                self._log("External portal did not load — skipping", "warn", job_ref)
                                if new_tab and not new_tab.is_closed():
                                    new_tab.close()
                                break  # re-fetch job cards
                            portal = PortalAgent(
                                page=active_page, openai_client=self.client,
                                profile=self.profile, resume_text=tailored,
                                resume_pdf=resume_pdf, resume_docx=resume_docx,
                                review_before_submit=self.review_before_submit,
                                bridge=self.bridge,
                            )
                            success = portal.apply(
                                current_url, title, company, jd,
                                job_meta=self._current_job
                            )
                        else:
                            orig_page = self.page
                            self.page = active_page
                            success   = self.handle_easy_apply(
                                resume_pdf, resume_docx, title, company
                            )  # resume_docx preferred; resume_pdf=None by default
                            self.page = orig_page

                        # Close the new tab — Playwright automatically refocuses self.page
                        if new_tab and not new_tab.is_closed():
                            new_tab.close()

                        if success:
                            self.applied += 1
                            source = "External Portal" if is_external else "LinkedIn Easy Apply"
                            add_application(
                                company, title, source,
                                notes=f"learn: {rel.get('learn','')}" if rel["decision"] == "APPLY_LEARN" else ""
                            )
                            self._log(f"Applied ({self.applied}/{max_applications})", "success", self._current_job)
                            if self.bridge: self.bridge.inc_applied()
                        else:
                            self._log("Could not complete application", "warn", job_ref)
                            try:
                                dismiss = self.page.query_selector('button[aria-label="Dismiss"]')
                                if dismiss: click_el(self.page, dismiss)
                            except Exception:
                                pass

                        # Navigation occurred — card list is stale. Break to re-fetch.
                        break

                    except PWTimeout:
                        self._log("Timeout — moving on", "warn")
                        break  # re-fetch cards after timeout too
                    except Exception as e:
                        err = str(e)
                        if "context was destroyed" in err or "Target page" in err or "closed" in err:
                            # Page navigated — silently re-fetch cards
                            break
                        self._log(f"Error: {e}", "error")

                # After processing one card (the for-loop always breaks after 1),
                # scroll the left panel to reveal the next card and trigger
                # LinkedIn's lazy-loading before the next while iteration.
                if self.bridge and not self.bridge.is_running:
                    break
                if not self._scroll_for_next_card(seen):
                    # No new cards on this page — try pagination
                    if self._click_next_page():
                        seen.clear()   # fresh page, reset card deduplication
                    else:
                        self._log("No more jobs to load", "info")
                        break

            # Save learning plan
            if skills_to_learn:
                self._save_learning_plan(skills_to_learn)

            # Check messages
            self._log("Checking LinkedIn messages for recruiter outreach…")
            for msg in self.check_messages():
                reply = self.draft_reply(msg["message"])
                self._log(f"Drafting reply to {msg['sender']}…")

                if self.bridge:
                    ok = self.bridge.request_approval(
                        job={
                            "title":   f"Reply to {msg['sender']}",
                            "company": "LinkedIn Message",
                            "jd":      msg["message"][:3000],
                            "score":   0,
                        },
                        tailored_resume=reply,
                        modal_type="email",
                    )
                else:
                    print(f"\n  Draft reply to {msg['sender']}:\n\n{reply}\n")
                    ok = input("  Send this reply? (y/n): ").strip().lower() == "y"

                if ok:
                    if self.send_linkedin_message(reply):
                        self._log(f"Reply sent to {msg['sender']}", "success")
                        if self.bridge: self.bridge.inc_emails()
                    else:
                        self._log(f"Could not send reply to {msg['sender']} — saving to file", "warn")
                        os.makedirs("output", exist_ok=True)
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        with open(f"output/linkedin_reply_{ts}.txt", "w") as f:
                            f.write(f"To: {msg['sender']}\n\n{reply}")
                else:
                    self._log("Reply skipped", "skip")

        finally:
            if not self.bridge:
                input("  Press Enter to close the browser…")
            if not keep_alive:
                self.close_browser()

    def post_update(self, text: str) -> bool:
        """
        Post a status update on LinkedIn on behalf of the user.
        Requires explicit permission (post_enabled on bridge).
        """
        if self.bridge and not getattr(self.bridge, "post_enabled", False):
            self._log("LinkedIn posting not enabled — toggle 'Post' in the mode bar", "warn")
            return False

        self._log(f"Posting to LinkedIn: {text[:80]}…")
        try:
            self.page.goto("https://www.linkedin.com/feed/")
            wait_for_navigation(self.page)
            pause(1.5, 2.5)

            # Click "Start a post"
            start_btn = None
            for sel in [
                'button[aria-label*="Start a post"]',
                '.share-box-feed-entry__trigger',
                'button.share-creation-state__mini-container',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        start_btn = el
                        break
                except Exception:
                    continue

            if not start_btn:
                self._log("Could not find 'Start a post' button", "warn")
                return False

            click_el(self.page, start_btn)
            pause(1.0, 1.5)

            # Type post content
            editor = None
            for sel in [
                '.ql-editor',
                '[contenteditable="true"]',
                '.share-creation-state__mini-container [role="textbox"]',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        editor = el
                        break
                except Exception:
                    continue

            if not editor:
                self._log("Could not find post editor", "warn")
                return False

            click_el(self.page, editor)
            pause(0.5, 1.0)
            type_into(editor, text)
            pause(1.0, 1.5)

            # Request approval before posting
            if self.bridge:
                approved = self.bridge.request_approval(
                    {"title": "LinkedIn Post", "company": "", "jd": text},
                    text, modal_type="post"
                )
                if not approved:
                    self._log("Post cancelled", "skip")
                    # Dismiss the post dialog
                    dismiss = self.page.query_selector('button[aria-label="Dismiss"]')
                    if dismiss: click_el(self.page, dismiss)
                    return False

            # Click Post button
            post_btn = None
            for sel in [
                'button[aria-label="Post"]',
                '.share-actions__primary-action',
                'button.artdeco-button--primary',
            ]:
                try:
                    el = self.page.query_selector(sel)
                    if el and el.is_visible():
                        post_btn = el
                        break
                except Exception:
                    continue

            if post_btn:
                click_el(self.page, post_btn)
                pause(2.0, 3.0)
                self._log("Posted to LinkedIn successfully", "success")
                return True
            else:
                self._log("Could not find Post button", "warn")
                return False

        except Exception as e:
            self._log(f"Post error: {e}", "error")
            return False

    def _save_learning_plan(self, skills: list):
        os.makedirs("output", exist_ok=True)
        all_skills = {}
        for entry in skills:
            for skill in entry["learn"].split(","):
                skill = skill.strip()
                if skill and skill.lower() != "none":
                    all_skills.setdefault(skill, []).append(entry["job"])
        with open("output/learning_plan.txt", "w") as f:
            f.write("SKILLS TO LEARN\n" + "="*50 + "\n\n")
            for skill, jobs in sorted(all_skills.items(), key=lambda x: -len(x[1])):
                f.write(f"  {skill} ({len(jobs)} job(s))\n")
                for job in jobs[:3]:
                    f.write(f"    - {job}\n")
        self._log(f"Learning plan saved → output/learning_plan.txt", "success")
