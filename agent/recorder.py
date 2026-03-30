"""
Learn-from-demonstration recorder.

First run  → agent doesn't know the selectors → shows a "Watching" banner
             in the browser → user does the action manually → clicks Save
             → URL + inputs are snapshotted and saved as a macro JSON file.

Every future run → macro is loaded, URL template is replayed automatically
                   (job keywords / location are substituted in the URL).
"""

import json
import os
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

MACRO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "browser_profile", "macros",
)
os.makedirs(MACRO_DIR, exist_ok=True)

# ── Watch banner injected into the browser page ──────────────────────────────

WATCH_BANNER_JS = r"""
(function() {
    if (document.getElementById('__jc_banner')) return;

    const bar = document.createElement('div');
    bar.id = '__jc_banner';
    bar.style.cssText = [
        'position:fixed','top:0','left:0','right:0','z-index:2147483647',
        'background:#1f6feb','color:#fff','padding:10px 20px',
        'font:13px/1.5 system-ui,sans-serif',
        'display:flex','align-items:center','gap:12px','box-shadow:0 2px 8px #0005',
    ].join(';');

    bar.innerHTML = `
        <span>&#128065; <b>Job Copilot is watching.</b>
        Type your search, set your filters — then click ✓&nbsp;Save when done.</span>
        <button id="__jc_save"
            style="margin-left:auto;padding:5px 16px;border-radius:6px;
                   background:#fff;color:#1f6feb;border:none;
                   font-weight:600;cursor:pointer;font-size:13px">
            ✓ Save this setup
        </button>`;

    document.body.style.paddingTop = '44px';
    document.body.prepend(bar);

    document.getElementById('__jc_save').addEventListener('click', () => {
        window.__jc_saved = true;
        bar.style.background = '#2ea043';
        bar.querySelector('span').textContent =
            '✓ Saved! Agent will replay this automatically next time.';
        setTimeout(() => {
            bar.remove();
            document.body.style.paddingTop = '';
        }, 2500);
    });
})();
"""


# ── Macro helpers ─────────────────────────────────────────────────────────────

def macro_path(name: str) -> str:
    return os.path.join(MACRO_DIR, f"{name}.json")


def has_macro(name: str) -> bool:
    return os.path.exists(macro_path(name))


def save_macro(name: str, data: dict):
    with open(macro_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_macro(name: str) -> dict:
    try:
        with open(macro_path(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def delete_macro(name: str):
    """Call this to force re-learning on next run."""
    p = macro_path(name)
    if os.path.exists(p):
        os.remove(p)


# ── Watch mode ────────────────────────────────────────────────────────────────

def watch_and_learn(page, name: str, bridge=None, timeout_s: int = 180) -> dict:
    """
    Show the watch banner, wait for the user to complete the action
    and click ✓ Save, then snapshot the URL + visible input values.

    Returns the saved macro dict.
    """
    def _log(msg, level="info"):
        if bridge:
            bridge.log(msg, level=level, tool="Recorder")
        else:
            print(f"  [Recorder] {msg}")

    _log(
        f"WATCH MODE — Complete '{name}' in the browser window, "
        f"then click the green ✓ Save button at the top.",
        "warn",
    )

    try:
        page.evaluate(WATCH_BANNER_JS)
    except Exception:
        pass

    # Poll until user clicks Save or timeout
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1)
        try:
            if page.evaluate("() => !!window.__jc_saved"):
                break
        except Exception:
            pass
    else:
        _log("Watch mode timed out — saving current state anyway", "warn")

    # Snapshot URL
    url = page.url

    # Snapshot visible text inputs (search box values, etc.)
    inputs = {}
    try:
        for inp in page.query_selector_all(
            "input[aria-label], input[placeholder], input[name]"
        ):
            try:
                label = (
                    inp.get_attribute("aria-label")
                    or inp.get_attribute("placeholder")
                    or inp.get_attribute("name")
                    or ""
                )
                val = inp.input_value()
                if label and val:
                    inputs[label.strip()] = val.strip()
            except Exception:
                pass
    except Exception:
        pass

    macro = {"url": url, "inputs": inputs}
    save_macro(name, macro)
    _log(f"Macro '{name}' saved → will replay automatically next time.", "success")
    return macro


# ── Replay helpers ────────────────────────────────────────────────────────────

def replay_search_url(macro: dict, query: str, location: str) -> str:
    """
    Take a saved LinkedIn search URL and substitute the new keywords + location.
    All other params (filters, f_AL, f_TPR, f_E, f_JT, etc.) are preserved.
    """
    url = macro.get("url", "")
    if not url:
        return ""

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Replace keywords and location, keep everything else
    params["keywords"] = [query]
    params["location"] = [location]

    # Flatten (parse_qs returns lists)
    flat = {k: v[0] for k, v in params.items()}
    new_query = urlencode(flat)
    return urlunparse(parsed._replace(query=new_query))
