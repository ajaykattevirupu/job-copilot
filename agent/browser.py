"""
Human-like browser controller — stealth edition.

Anti-detection layers:
  1. Persistent Chrome profile  → stays logged in, looks like a real user
  2. Expanded stealth JS        → hides 15+ Playwright/automation fingerprints
  3. playwright-stealth patch   → extra evasions (optional, gracefully skipped)
  4. Bezier-curve mouse moves   → cursor arcs naturally to every target
  5. Scroll-before-interact     → smooth wheel scroll until element is on-screen
  6. Human keystroke timing     → variable 60-200ms delays + occasional hesitations
  7. Random inter-action pauses → 0.5–2s gaps between major actions
"""

import os
import sys
import time
import math
import random
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import Page

# ── Reading-pause config ─────────────────────────────────────────────────────────
# Seconds the bot waits before clicking Apply / Submit.
# During this window a keypress pauses the bot and hands control to the user.
READING_PAUSE = 8

# ── Scroll-abort flag ────────────────────────────────────────────────────────────
# Set instantly from the background CDP thread when the user fires a trusted
# wheel / mousedown / keydown event (via page.expose_binding → __botAbort).
# Checked before every micro-step in scroll and mouse-move loops so the bot
# yields in < 50 ms — no CDP round-trip required.
_abort = threading.Event()


def _is_aborted() -> bool:
    """True if the user has taken the scroll wheel / mouse since last reset."""
    return _abort.is_set()


def _reset_abort():
    """Call at the top of every new scroll/move sequence to start fresh."""
    _abort.clear()


def _bot_abort_callback(source, *_args):
    """
    Invoked by the browser's JS __botAbort() call (via expose_binding).
    Runs on Playwright's background CDP thread — threading.Event is safe here.
    """
    _abort.set()

# ── Profile ─────────────────────────────────────────────────────────────────────

PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "browser_profile",
)

# ── User-Agent (latest stable Chrome) ───────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Stealth JS — injected into every page before any scripts run ─────────────────
# Covers the most common fingerprint checks used by Akamai, PerimeterX, Workday, etc.

STEALTH_JS = """
(function () {
    // 1. Hide webdriver flag — #1 check on every bot-detection service
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Realistic plugin list (empty = headless Chromium giveaway)
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            function FakePlugin(name, desc, filename) {
                return Object.create(Plugin.prototype, {
                    name:        { value: name },
                    description: { value: desc },
                    filename:    { value: filename },
                    length:      { value: 0 },
                });
            }
            const plugins = [
                FakePlugin('Chrome PDF Plugin',     'Portable Document Format', 'internal-pdf-viewer'),
                FakePlugin('Chrome PDF Viewer',     '',                         'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
                FakePlugin('Native Client',         '',                         'internal-nacl-plugin'),
            ];
            plugins.length = 3;
            return plugins;
        }
    });

    // 3. Languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // 4. Hardware — realistic values
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
    Object.defineProperty(navigator, 'platform',            { get: () => 'Win32' });
    Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0 });

    // 5. Chrome runtime object — absent in pure Playwright contexts
    if (!window.chrome) {
        window.chrome = {
            runtime: {
                id: undefined,
                connect: function() {},
                sendMessage: function() {},
                onMessage: { addListener: function() {}, removeListener: function() {} },
            },
            loadTimes: function() { return {}; },
            csi:        function() { return {}; },
            app:        {},
        };
    }

    // 6. Permissions — real Chrome resolves 'notifications' from actual permission state
    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission, onchange: null })
            : _origQuery(params);

    // 7. Screen properties — match a typical 1920×1080 display
    Object.defineProperty(screen, 'width',       { get: () => 1920 });
    Object.defineProperty(screen, 'height',      { get: () => 1080 });
    Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
    Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
    Object.defineProperty(screen, 'colorDepth',  { get: () => 24  });
    Object.defineProperty(screen, 'pixelDepth',  { get: () => 24  });

    // 8. Hide Playwright-specific window properties
    const _cleanProps = ['__playwright', '__pw_manual', '__PW_inspect',
                         '_playwrightWorkerState', '__selenium_evaluate'];
    _cleanProps.forEach(p => { try { delete window[p]; } catch(_) {} });

    // 9. Prevent iframe/iframe-contentWindow detection
    const _origIframe = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
    if (_origIframe) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const w = _origIframe.call(this);
                if (w) {
                    try {
                        Object.defineProperty(w.navigator, 'webdriver', { get: () => undefined });
                    } catch(_) {}
                }
                return w;
            }
        });
    }

    // 10. Track mouse position for Bezier continuity (set by Python side)
    window._mouseX = window.innerWidth  / 2;
    window._mouseY = window.innerHeight / 2;
    document.addEventListener('mousemove', e => {
        window._mouseX = e.clientX;
        window._mouseY = e.clientY;
    }, { passive: true });

    // 11. Human-takeover detector + instant Python abort signal.
    //     isTrusted=true  → real physical device event (user)
    //     isTrusted=false → Playwright-synthesised event (bot)
    //     Sets window._humanTookControl for the session-level takeover check,
    //     AND calls window.__botAbort() to immediately wake the Python abort
    //     flag so ongoing scroll/mouse loops stop within one micro-step.
    window._humanTookControl = false;
    ['mousedown', 'wheel', 'keydown'].forEach(function(type) {
        window.addEventListener(type, function(e) {
            if (e.isTrusted) {
                window._humanTookControl = true;
                try { window.__botAbort(); } catch(_) {}
            }
        }, { passive: true, capture: true });
    });
})();
"""


# ── Browser launch ───────────────────────────────────────────────────────────────

def launch(playwright, headless: bool = False):
    """
    Launch a stealth Chrome browser with a persistent project profile.
    Log in once on first launch — stays logged in after that.
    Returns a BrowserContext.
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)

    try:
        ctx = playwright.chromium.launch_persistent_context(
            user_data_dir = PROFILE_DIR,
            channel       = "chrome",
            headless      = headless,
            viewport      = {"width": 1366, "height": 768},
            user_agent    = USER_AGENT,
            locale        = "en-US",
            timezone_id   = "America/Chicago",
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-service-autorun",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--disable-focus-on-load",
            ],
            ignore_default_args = ["--enable-automation"],
        )
    except Exception as e:
        if "user data directory is already in use" in str(e).lower() or "target closed" in str(e).lower():
            raise RuntimeError(
                "The agent's browser profile is already in use.\n"
                "Another agent process may still be running — stop it and try again."
            ) from e
        raise

    ctx.add_init_script(STEALTH_JS)

    # Expose the Python abort callback so every page in this context can call
    # window.__botAbort() from JavaScript to immediately set _abort.
    try:
        ctx.expose_binding("__botAbort", _bot_abort_callback)
    except Exception:
        pass  # already exposed (e.g. context reuse)

    # Optional: playwright-stealth extra patches (install with: pip install playwright-stealth)
    try:
        from playwright_stealth import stealth_sync
        for page in ctx.pages:
            stealth_sync(page)
        ctx.on("page", lambda p: stealth_sync(p))
    except ImportError:
        pass  # gracefully skip if not installed

    return ctx


# ── Randomness helpers ────────────────────────────────────────────────────────────

def _rnd(a: float, b: float) -> float:
    return random.uniform(a, b)

def pause(min_s: float = 0.8, max_s: float = 2.0):
    """Random pause — simulates a human reading the page."""
    time.sleep(_rnd(min_s, max_s))

def short_pause():
    time.sleep(_rnd(0.3, 0.8))

def micro_pause():
    time.sleep(_rnd(0.04, 0.15))

def reading_pause():
    """Longer pause — simulates reading content before acting."""
    time.sleep(_rnd(0.8, 2.0))


# ── Bezier mouse movement ─────────────────────────────────────────────────────────

def _bezier_path(sx: float, sy: float, tx: float, ty: float,
                 num_points: int = None) -> list:
    """
    Generate (x, y) points along a quadratic Bezier curve from (sx,sy) to (tx,ty).
    The control point is offset randomly so the path curves naturally.
    """
    if num_points is None:
        # More points for longer distances → smooth arc
        dist = math.hypot(tx - sx, ty - sy)
        num_points = max(12, min(40, int(dist / 15)))

    # Random control point — shifts the arc left/right and up/down
    cx = (sx + tx) / 2 + _rnd(-120, 120)
    cy = (sy + ty) / 2 + _rnd(-80,  80)

    pts = []
    for i in range(num_points + 1):
        t  = i / num_points
        mt = 1 - t
        x  = mt * mt * sx + 2 * mt * t * cx + t * t * tx
        y  = mt * mt * sy + 2 * mt * t * cy + t * t * ty
        pts.append((x, y))
    return pts


def _get_mouse_pos(page: Page) -> tuple:
    """Read the tracked mouse position from the page (set by our STEALTH_JS listener)."""
    try:
        pos = page.evaluate("() => ({ x: window._mouseX, y: window._mouseY })")
        return float(pos["x"]), float(pos["y"])
    except Exception:
        return 683.0, 384.0   # fallback: centre of 1366×768


def human_move(page: Page, tx: float, ty: float):
    """
    Move the cursor from its current position to (tx, ty) along a Bezier arc.
    Each intermediate step uses page.mouse.move() so the motion is visible
    and tracked by bot-detection scripts.
    """
    sx, sy = _get_mouse_pos(page)

    # Skip movement if already near target
    if math.hypot(tx - sx, ty - sy) < 5:
        return

    pts = _bezier_path(sx, sy, tx, ty)
    for x, y in pts:
        if _is_aborted():
            break
        page.mouse.move(x, y)
        time.sleep(_rnd(0.004, 0.012))   # ~6ms per step → smooth 60fps-ish motion

    # Update tracker
    try:
        page.evaluate(f"() => {{ window._mouseX = {tx}; window._mouseY = {ty}; }}")
    except Exception:
        pass


# ── Smooth scrolling ──────────────────────────────────────────────────────────────

def scroll_into_view(page: Page, el) -> bool:
    """
    Smoothly scroll the page until `el` is visible on screen.
    Uses mouse wheel in small human-sized increments rather than
    teleporting via scrollIntoView().
    Aborts immediately if the user touches the scroll wheel.
    """
    _reset_abort()
    try:
        box = el.bounding_box()
        if not box:
            return False

        vp        = page.viewport_size or {"width": 1366, "height": 768}
        vh        = vp["height"]
        el_center = box["y"] + box["height"] / 2
        target_y  = vh * 0.45   # aim to place element just above screen centre

        offset    = el_center - target_y
        if abs(offset) < 50:
            return True   # already on screen

        steps    = random.randint(6, 12)
        per_step = offset / steps
        for _ in range(steps):
            if _is_aborted():
                break
            page.mouse.wheel(0, per_step + _rnd(-15, 15))
            time.sleep(_rnd(0.06, 0.18))

        short_pause()
        return True
    except Exception:
        return False


def scroll_down(page: Page, total_px: int = 600):
    """Scroll the main viewport down total_px in smooth human-sized wheel steps.
    Aborts immediately if the user touches the scroll wheel."""
    _reset_abort()
    steps = random.randint(5, 10)
    per_step = total_px / steps
    for _ in range(steps):
        if _is_aborted():
            break
        page.mouse.wheel(0, per_step + _rnd(-25, 25))
        time.sleep(_rnd(0.07, 0.22))


def scroll_panel(page: Page, hover_x: float, hover_y: float,
                 total_px: int = 400, reading: bool = False):
    """
    Scroll an inner panel (e.g. job description pane, results sidebar) by
    moving the cursor over it first, then sending mouse wheel events.

    hover_x / hover_y — coordinates inside the panel to hover over.
    reading           — if True, insert random pauses between steps to
                        simulate a human reading while scrolling.
    Aborts immediately if the user touches the scroll wheel.
    """
    _reset_abort()
    human_move(page, hover_x, hover_y)
    micro_pause()
    steps = random.randint(4, 8)
    per_step = total_px / steps
    for _ in range(steps):
        if _is_aborted():
            break
        page.mouse.wheel(0, per_step + _rnd(-20, 20))
        if reading:
            time.sleep(_rnd(0.25, 0.7))   # pause to "read" each chunk
        else:
            time.sleep(_rnd(0.06, 0.18))


# ── Human clicking ────────────────────────────────────────────────────────────────

def click(page: Page, selector: str, timeout: int = 10000) -> bool:
    """
    Wait for selector → scroll into view → Bezier-arc to element → click.
    """
    try:
        el = page.wait_for_selector(selector, timeout=timeout, state="visible")
        if not el:
            return False
        return click_el(page, el)
    except Exception:
        return False


def click_el(page: Page, el) -> bool:
    """
    Scroll element into view, move mouse along a Bezier curve, hover,
    then click with a slight randomised offset inside the element.
    """
    try:
        scroll_into_view(page, el)
        micro_pause()

        box = el.bounding_box()
        if not box:
            el.click()
            short_pause()
            return True

        # Target: random point inside the element (not always dead-centre)
        tx = box["x"] + box["width"]  * _rnd(0.25, 0.75) + _rnd(-2, 2)
        ty = box["y"] + box["height"] * _rnd(0.25, 0.75) + _rnd(-2, 2)

        human_move(page, tx, ty)
        time.sleep(_rnd(0.08, 0.25))   # hover pause before clicking
        page.mouse.click(tx, ty)
        time.sleep(_rnd(0.3, 0.9))
        return True
    except Exception:
        try:
            el.click()
            short_pause()
            return True
        except Exception:
            return False


# ── Human typing ──────────────────────────────────────────────────────────────────

def type_text(page: Page, selector: str, text: str, clear: bool = True):
    """
    Click a field (with Bezier move) then type with human keystroke timing.
    """
    try:
        el = page.wait_for_selector(selector, timeout=8000, state="visible")
        if not el:
            return
        click_el(page, el)
        short_pause()
        if clear:
            page.keyboard.press("Control+a")
            micro_pause()
            page.keyboard.press("Backspace")
            short_pause()
        _human_type_keys(page, text)
    except Exception:
        try:
            page.fill(selector, text)
        except Exception:
            pass


def type_into(el, text: str):
    """
    Type text into an element with human keystroke timing.
    Tries to get the page reference for keyboard events; falls back to el.type().
    """
    try:
        # Click the element (Bezier move handled if we have the page)
        el.click()
        short_pause()
        el.press("Control+a")
        micro_pause()
        el.press("Backspace")
        short_pause()
        for i, ch in enumerate(text):
            el.type(ch)
            # Base keystroke delay: 60–200ms
            time.sleep(_rnd(0.06, 0.20))
            # Occasional longer pause — "thinking between words"
            if ch == " " and random.random() < 0.25:
                time.sleep(_rnd(0.15, 0.45))
            # Burst-then-pause pattern every 8–18 chars
            if i > 0 and i % random.randint(8, 18) == 0:
                time.sleep(_rnd(0.2, 0.6))
        short_pause()
    except Exception:
        try:
            el.fill(text)
        except Exception:
            pass


def _human_type_keys(page: Page, text: str):
    """Type text via page.keyboard with variable inter-key delays."""
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        time.sleep(_rnd(0.06, 0.20))
        if ch == " " and random.random() < 0.25:
            time.sleep(_rnd(0.15, 0.45))
        if i > 0 and i % random.randint(8, 18) == 0:
            time.sleep(_rnd(0.2, 0.6))
    short_pause()


# ── Scroll helpers ────────────────────────────────────────────────────────────────

def scroll_to_element(page: Page, selector: str):
    """Smooth-scroll until a selector's element is on screen."""
    try:
        el = page.query_selector(selector)
        if el:
            scroll_into_view(page, el)
    except Exception:
        pass


# ── Wait utilities ────────────────────────────────────────────────────────────────

def wait_for_any(page: Page, selectors: list, timeout: int = 10000):
    """
    Poll for the first selector that appears.
    Returns (selector, element) or (None, None).
    """
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    return sel, el
            except Exception:
                pass
        time.sleep(0.3)
    return None, None


def wait_for_navigation(page: Page, timeout: int = 15000):
    """
    Wait for page load. Uses 'load' not 'networkidle' — LinkedIn/Indeed
    make constant XHR requests so networkidle never fires.
    """
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
    short_pause()


# ── Human-takeover helpers ────────────────────────────────────────────────────────

def check_human_takeover(page: Page) -> bool:
    """
    Returns True if a real user (isTrusted event) has interacted with the page
    since the last reset_human_takeover() call.
    """
    try:
        return bool(page.evaluate("() => window._humanTookControl === true"))
    except Exception:
        return False


def reset_human_takeover(page: Page):
    """Clear the human-takeover flag so the next job starts fresh."""
    try:
        page.evaluate("() => { window._humanTookControl = false; }")
    except Exception:
        pass


# ── Reading-pause countdown ───────────────────────────────────────────────────────

def reading_countdown(title: str, action: str = "apply", bridge=None) -> str:
    """
    Print a terminal countdown (READING_PAUSE seconds) before a major action.
    During the countdown the user can press any key to pause the agent.

    Returns:
        "proceed" — countdown elapsed, bot should continue
        "skip"    — user typed 'skip', bot should skip this job
        "pause"   — user pressed a key (not skip), bot should leave tab open
                    and move on without closing it
    """
    sep = "─" * 52
    print(f"\n  ┌{sep}", flush=True)
    print(f"  │  Job : {title[:60]}", flush=True)
    print(f"  │  Bot will '{action}' in {READING_PAUSE}s.", flush=True)
    print(f"  │  Press any key to pause and take control.", flush=True)
    print(f"  └{sep}", flush=True)

    if bridge:
        bridge.log(
            f"Reading pause — will '{action}' [{title[:50]}] in {READING_PAUSE}s",
            level="warn",
        )

    for remaining in range(READING_PAUSE, 0, -1):
        print(f"  {remaining}s ...  ", end="\r", flush=True)
        key_hit = _wait_for_keypress(1.0)
        if key_hit:
            print(f"\n  ── Paused on '{title[:50]}' ──", flush=True)
            print(f"  Enter 'skip' to skip this job, or press Enter to proceed:", flush=True)
            try:
                cmd = input("  > ").strip().lower()
            except Exception:
                cmd = ""
            if cmd in ("skip", "s"):
                print("  Skipping.", flush=True)
                return "skip"
            print("  Resuming bot — tab left open.", flush=True)
            return "pause"

    print(f"  Proceeding with '{action}'...          ", flush=True)
    return "proceed"


def _wait_for_keypress(timeout_secs: float) -> bool:
    """
    Non-blocking keypress check over timeout_secs seconds.
    Returns True if a key was pressed.
    Works on Windows (msvcrt) and Unix (select).
    """
    deadline = time.time() + timeout_secs
    if sys.platform == "win32":
        import msvcrt
        while time.time() < deadline:
            if msvcrt.kbhit():
                msvcrt.getch()   # consume the byte
                return True
            time.sleep(0.05)
        return False
    else:
        import select
        remaining = deadline - time.time()
        r, _, _ = select.select([sys.stdin], [], [], max(0, remaining))
        if r:
            sys.stdin.readline()
            return True
        return False
