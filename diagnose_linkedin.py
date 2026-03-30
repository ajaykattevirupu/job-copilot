"""
LinkedIn selector diagnostic.

Run this ONCE:
    py diagnose_linkedin.py

It opens your LinkedIn (using the saved session), goes to a Java Full Stack
job search, clicks the first job, and dumps every relevant selector to
diagnose_output.txt so we can update the agent's selectors precisely.
"""

import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright
from agent.browser import launch, wait_for_navigation, pause

OUTPUT = os.path.join(os.path.dirname(__file__), "diagnose_output.txt")
SEARCH = "https://www.linkedin.com/jobs/search/?keywords=Java+Full+Stack+Engineer&location=United+States&f_TPR=r86400"

def log(f, label, items):
    f.write(f"\n{'='*60}\n{label}\n{'='*60}\n")
    if not items:
        f.write("  (none found)\n")
    for item in items:
        f.write(f"  {item}\n")

def el_info(el):
    try:
        tag      = el.evaluate("e => e.tagName").lower()
        aria     = el.get_attribute("aria-label") or ""
        cls      = el.get_attribute("class") or ""
        eid      = el.get_attribute("id") or ""
        name     = el.get_attribute("name") or ""
        typ      = el.get_attribute("type") or ""
        text     = (el.inner_text() or "")[:60].replace("\n", " ")
        visible  = el.is_visible()
        return f"<{tag}> id={eid!r} class={cls[:40]!r} type={typ!r} name={name!r} aria={aria!r} text={text!r} visible={visible}"
    except Exception as e:
        return f"(error reading element: {e})"

with sync_playwright() as pw:
    ctx  = launch(pw)
    page = ctx.new_page()

    print("Opening LinkedIn job search…")
    page.goto(SEARCH)
    wait_for_navigation(page)
    pause(2, 3)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("LINKEDIN SELECTOR DIAGNOSTIC\n")
        f.write(f"URL: {page.url}\n")

        # ── 1. Job cards ─────────────────────────────────────────────
        card_sels = [
            ".job-card-container",
            ".jobs-search-results__list-item",
            "[data-job-id]",
            "[data-occludable-job-id]",
            "li.scaffold-layout__list-item",
        ]
        found_cards = []
        for sel in card_sels:
            els = page.query_selector_all(sel)
            found_cards.append(f"{sel!r:60} → {len(els)} elements")
        log(f, "JOB CARD SELECTORS", found_cards)

        # ── 2. Click the first visible job card ──────────────────────
        first_card = None
        for sel in card_sels:
            els = page.query_selector_all(sel)
            for el in els:
                if el.is_visible():
                    first_card = el
                    break
            if first_card:
                break

        if first_card:
            print("Clicking first job card…")
            first_card.click()
            pause(2, 3)
            f.write(f"\nClicked job card. URL now: {page.url}\n")

            # ── 3. Apply buttons ─────────────────────────────────────
            apply_sels = [
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply']",
                "button[aria-label*='Apply']",
                ".jobs-s-apply button",
                "[data-control-name='jobdetails_topcard_inapply']",
                "button[data-job-id]",
                ".jobs-apply-button--top-card",
            ]
            found_btns = []
            for sel in apply_sels:
                try:
                    els = page.query_selector_all(sel)
                    for el in els:
                        found_btns.append(f"{sel!r:55} → {el_info(el)}")
                except Exception:
                    pass
            log(f, "APPLY BUTTON SELECTORS", found_btns)

            # ── 4. All buttons in the job detail panel ───────────────
            all_btns = page.query_selector_all(
                ".jobs-details__main-content button, "
                ".jobs-unified-top-card button, "
                ".jobs-s-apply button"
            )
            log(f, "ALL BUTTONS IN JOB PANEL",
                [el_info(b) for b in all_btns])

            # ── 5. Job title / company selectors ─────────────────────
            detail_sels = {
                "title":   [
                    "h1.job-details-jobs-unified-top-card__job-title",
                    ".job-details-jobs-unified-top-card__job-title h1",
                    ".jobs-unified-top-card__job-title h1",
                    "h1.t-24",
                ],
                "company": [
                    ".job-details-jobs-unified-top-card__company-name a",
                    ".jobs-unified-top-card__company-name a",
                    "[data-tracking-control-name*='company'] a",
                ],
                "jd":      [
                    "#job-details",
                    ".jobs-description__content",
                    ".jobs-description-content__text",
                ],
            }
            for field, sels in detail_sels.items():
                results = []
                for sel in sels:
                    el = page.query_selector(sel)
                    if el:
                        results.append(f"{sel!r} → FOUND: {el.inner_text()[:80].replace(chr(10),' ')!r}")
                    else:
                        results.append(f"{sel!r} → not found")
                log(f, f"JOB DETAIL: {field.upper()}", results)

            # ── 6. Click Easy Apply if present and dump modal ────────
            apply_el = None
            for sel in ["button[aria-label*='Easy Apply']", "button.jobs-apply-button"]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    apply_el = el
                    f.write(f"\nFound apply button: {el_info(el)}\n")
                    break

            if apply_el:
                print("Clicking Easy Apply to inspect the modal…")
                tabs_before = len(ctx.pages)
                apply_el.click()
                pause(2, 3)
                tabs_after = len(ctx.pages)

                f.write(f"\nTabs before click: {tabs_before}, after: {tabs_after}\n")

                if tabs_after > tabs_before:
                    modal_page = ctx.pages[-1]
                    wait_for_navigation(modal_page)
                    f.write(f"New tab URL: {modal_page.url}\n")
                    target = modal_page
                else:
                    target = page
                    modal = page.query_selector(".jobs-easy-apply-modal, [data-test-modal]")
                    f.write(f"Modal element: {el_info(modal) if modal else 'NOT FOUND'}\n")

                # Dump all inputs in the modal / new tab
                inputs = target.query_selector_all("input, textarea, select, button")
                log(f, "MODAL / NEW TAB — ALL FORM ELEMENTS",
                    [el_info(i) for i in inputs[:40]])
            else:
                f.write("\nNo Easy Apply button found — cannot inspect modal.\n")
        else:
            f.write("\nNo job card found to click.\n")

    print(f"\nDone! Results saved to:\n  {OUTPUT}\n")
    input("Press Enter to close the browser…")
    ctx.close()
