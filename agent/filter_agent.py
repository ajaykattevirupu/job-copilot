"""
LinkedIn Filter Agent — sets all filter UI options after searching.

After a job search, LinkedIn shows filter buttons at the top:
  Date posted | Experience level | Job type | Remote | All filters

This agent clicks through them and sets your preferences automatically
before the main agent starts applying.
"""

import time


# LinkedIn filter label mappings
EXPERIENCE_LEVEL_MAP = {
    "internship":   "Internship",
    "entry":        "Entry level",
    "associate":    "Associate",
    "mid":          "Mid-Senior level",
    "senior":       "Mid-Senior level",
    "director":     "Director",
    "executive":    "Executive",
}

JOB_TYPE_MAP = {
    "full-time":  "Full-time",
    "part-time":  "Part-time",
    "contract":   "Contract",
    "temporary":  "Temporary",
    "internship": "Internship",
    "other":      "Other",
}

WORK_MODE_MAP = {
    "remote":  "Remote",
    "hybrid":  "Hybrid",
    "onsite":  "On-site",
    "on-site": "On-site",
}

DATE_FILTER_MAP = {
    "24h":   "Past 24 hours",
    "week":  "Past week",
    "month": "Past month",
    "any":   "Any time",
}


class LinkedInFilterAgent:

    def __init__(self, page):
        self.page = page

    def _click_if_exists(self, selector: str, timeout: int = 3000) -> bool:
        """Click element if it exists, return True if clicked."""
        try:
            el = self.page.wait_for_selector(selector, timeout=timeout)
            if el:
                el.click()
                time.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    def _check_option(self, label_text: str) -> bool:
        """Find and check a filter checkbox/button by its visible label text."""
        try:
            # try label element
            labels = self.page.query_selector_all("label span, li span, button span")
            for el in labels:
                if el.inner_text().strip().lower() == label_text.lower():
                    el.click()
                    time.sleep(0.3)
                    return True
        except Exception:
            pass
        return False

    def set_date_filter(self, date_filter: str = "week"):
        """Set 'Date posted' filter."""
        label = DATE_FILTER_MAP.get(date_filter, "Past week")
        try:
            # click the "Date posted" dropdown button
            date_btn = self.page.query_selector(
                'button[aria-label*="Date posted"], button:has-text("Date posted")'
            )
            if date_btn:
                date_btn.click()
                time.sleep(1)
                self._check_option(label)
                # click "Show results" or "Done"
                self._click_if_exists('button:has-text("Show results")')
                self._click_if_exists('button:has-text("Done")')
                time.sleep(1)
                print(f"  Filter set: Date posted → {label}")
        except Exception as e:
            print(f"  Could not set date filter: {e}")

    def set_experience_level(self, levels: list):
        """Set experience level filters (can select multiple)."""
        try:
            btn = self.page.query_selector(
                'button[aria-label*="Experience level"], button:has-text("Experience level")'
            )
            if btn:
                btn.click()
                time.sleep(1)
                checked = []
                for level in levels:
                    mapped = EXPERIENCE_LEVEL_MAP.get(level.lower(), level)
                    if self._check_option(mapped):
                        checked.append(mapped)
                self._click_if_exists('button:has-text("Show results")')
                self._click_if_exists('button:has-text("Done")')
                time.sleep(1)
                if checked:
                    print(f"  Filter set: Experience level → {', '.join(checked)}")
        except Exception as e:
            print(f"  Could not set experience level: {e}")

    def set_job_type(self, job_types: list):
        """Set job type filters (Full-time, Contract, etc.)."""
        try:
            btn = self.page.query_selector(
                'button[aria-label*="Job type"], button:has-text("Job type")'
            )
            if btn:
                btn.click()
                time.sleep(1)
                checked = []
                for jt in job_types:
                    mapped = JOB_TYPE_MAP.get(jt.lower(), jt)
                    if self._check_option(mapped):
                        checked.append(mapped)
                self._click_if_exists('button:has-text("Show results")')
                self._click_if_exists('button:has-text("Done")')
                time.sleep(1)
                if checked:
                    print(f"  Filter set: Job type → {', '.join(checked)}")
        except Exception as e:
            print(f"  Could not set job type: {e}")

    def set_work_mode(self, modes: list):
        """Set Remote/Hybrid/On-site filter."""
        try:
            btn = self.page.query_selector(
                'button[aria-label*="Remote"], button:has-text("Remote")'
            )
            if not btn:
                btn = self.page.query_selector('button:has-text("On-site")')
            if btn:
                btn.click()
                time.sleep(1)
                checked = []
                for mode in modes:
                    mapped = WORK_MODE_MAP.get(mode.lower(), mode)
                    if self._check_option(mapped):
                        checked.append(mapped)
                self._click_if_exists('button:has-text("Show results")')
                self._click_if_exists('button:has-text("Done")')
                time.sleep(1)
                if checked:
                    print(f"  Filter set: Work mode → {', '.join(checked)}")
        except Exception as e:
            print(f"  Could not set work mode: {e}")

    def open_all_filters(self):
        """Click the 'All filters' button to open the full filter panel."""
        try:
            btn = self.page.query_selector(
                'button[aria-label="All filters"], button:has-text("All filters")'
            )
            if btn:
                btn.click()
                time.sleep(1.5)
                return True
        except Exception:
            pass
        return False

    def apply_all_filters_panel(self):
        """Click 'Show X results' or 'Apply' in the full filter panel."""
        for selector in [
            'button:has-text("Show")',
            'button[aria-label*="Apply"]',
            'button:has-text("Apply")',
        ]:
            if self._click_if_exists(selector):
                time.sleep(2)
                return

    def setup_filters(self, preferences: dict):
        """
        Main entry — set all filters based on user preferences dict.

        preferences keys:
            date_filter:      "24h" | "week" | "month" | "any"
            experience_level: list of ["entry", "mid", "senior", "associate"]
            job_type:         list of ["Full-time", "Contract"]
            work_mode:        list of ["remote", "hybrid", "onsite"]
        """
        print("\n  Setting LinkedIn filters...")

        date_filter      = preferences.get("date_filter", "week")
        experience_level = preferences.get("experience_level", ["mid", "senior", "associate"])
        job_type_raw     = preferences.get("job_type", "Full-time")
        job_types        = [job_type_raw] if isinstance(job_type_raw, str) else job_type_raw
        work_modes       = preferences.get("work_mode", [])

        # set quick filters (top bar buttons)
        self.set_date_filter(date_filter)
        time.sleep(0.5)

        self.set_experience_level(experience_level)
        time.sleep(0.5)

        self.set_job_type(job_types)
        time.sleep(0.5)

        if work_modes:
            self.set_work_mode(work_modes)
            time.sleep(0.5)

        # open All Filters for anything remaining
        if self.open_all_filters():
            # easy apply toggle (make sure it's checked)
            easy_apply_toggle = self.page.query_selector(
                'label:has-text("Easy Apply"), input[aria-label*="Easy Apply"]'
            )
            if easy_apply_toggle:
                # check if already checked
                inp = self.page.query_selector('input#f_AL, input[name="f_AL"]')
                if inp and not inp.is_checked():
                    easy_apply_toggle.click()

            self.apply_all_filters_panel()

        print("  Filters applied. Starting job search...\n")
