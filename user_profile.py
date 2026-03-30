"""
User profile — saved once, reused forever.

Stores personal info, immigration status, and job preferences.
The agent loads this automatically on every run — no re-typing.

Profile is saved to profile.json in the project folder.
"""

import json
import os

PROFILE_FILE = "profile.json"

# Immigration status → form answer mappings
IMMIGRATION_ANSWERS = {
    "USC": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "No",
        "us_citizen":         "Yes",
        "permanent_resident": "No",
        "visa_type":          "US Citizen",
        "skip_clearance":     False,
    },
    "GC": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "No",
        "us_citizen":         "No",
        "permanent_resident": "Yes",
        "visa_type":          "Permanent Resident",
        "skip_clearance":     False,
    },
    "H1B": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "H-1B",
        "skip_clearance":     True,
    },
    "F1-OPT": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "F-1 OPT",
        "skip_clearance":     True,
    },
    "F1-CPT": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "F-1 CPT",
        "skip_clearance":     True,
    },
    "L1": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "L-1",
        "skip_clearance":     True,
    },
    "TN": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "TN Visa",
        "skip_clearance":     True,
    },
    "EAD": {
        "authorized":         "Yes",
        "sponsor_now":        "No",
        "sponsor_future":     "Yes",
        "us_citizen":         "No",
        "permanent_resident": "No",
        "visa_type":          "EAD",
        "skip_clearance":     True,
    },
}

# Keywords that flag a security clearance requirement
CLEARANCE_KEYWORDS = [
    "security clearance", "clearance required", "active clearance",
    "top secret", "ts/sci", "secret clearance", "public trust",
    "must be us citizen", "us citizenship required", "citizenship required",
    "citizen only", "itar", "ear compliance", "dod clearance",
    "government clearance", "federal clearance",
]

# Keywords that flag US citizen / GC only requirement
CITIZENSHIP_ONLY_KEYWORDS = [
    "us citizens only", "must be a us citizen", "citizenship required",
    "green card or us citizen", "gc or us citizen",
    "no sponsorship", "cannot sponsor", "will not sponsor",
    "sponsorship not available", "not able to sponsor",
]


def load_profile() -> dict:
    if not os.path.exists(PROFILE_FILE):
        return {}
    with open(PROFILE_FILE, "r") as f:
        return json.load(f)


def save_profile(profile: dict):
    with open(PROFILE_FILE, "w") as f:
        json.dump(profile, f, indent=2)


def get_immigration_answers(profile: dict) -> dict:
    """Return the correct form answers based on immigration status."""
    status = profile.get("immigration", {}).get("status", "F1-OPT")
    return IMMIGRATION_ANSWERS.get(status, IMMIGRATION_ANSWERS["F1-OPT"])


def requires_clearance(jd: str) -> bool:
    """Return True if this job requires security clearance."""
    jd_lower = jd.lower()
    return any(kw in jd_lower for kw in CLEARANCE_KEYWORDS)


def requires_citizenship(jd: str) -> bool:
    """Return True if this job explicitly requires US citizenship / no sponsorship."""
    jd_lower = jd.lower()
    return any(kw in jd_lower for kw in CITIZENSHIP_ONLY_KEYWORDS)


def _ask(prompt: str, default: str = "") -> str:
    """Ask a question with an optional default value."""
    if default:
        answer = input(f"  {prompt} [{default}]: ").strip()
        return answer if answer else default
    else:
        return input(f"  {prompt}: ").strip()


def setup_profile() -> dict:
    """
    Interactive first-time profile setup.
    Only runs once — saved to profile.json for all future sessions.
    """
    existing = load_profile()
    p = existing.copy() if existing else {}

    def section(title):
        print(f"\n  {'─'*50}")
        print(f"  {title}")
        print(f"  {'─'*50}")

    print("\n" + "="*60)
    print("  PROFILE SETUP — saved once, used forever")
    print("="*60)

    # ── Personal info ──────────────────────────────────────────────────────
    section("Personal Information")
    personal = p.get("personal", {})
    personal["name"]     = _ask("Full name",     personal.get("name", ""))
    personal["email"]    = _ask("Email",          personal.get("email", ""))
    personal["phone"]    = _ask("Phone",          personal.get("phone", ""))
    personal["linkedin"] = _ask("LinkedIn URL",   personal.get("linkedin", ""))
    personal["github"]   = _ask("GitHub URL (press Enter to skip)", personal.get("github", ""))
    personal["city"]     = _ask("City",           personal.get("city", ""))
    personal["state"]    = _ask("State (e.g. MO)", personal.get("state", ""))
    personal["zip"]      = _ask("ZIP code",       personal.get("zip", ""))
    personal["country"]  = _ask("Country",        personal.get("country", "United States"))
    p["personal"] = personal

    # ── Immigration ────────────────────────────────────────────────────────
    section("Immigration / Work Authorization")
    print("  Status options:")
    statuses = list(IMMIGRATION_ANSWERS.keys())
    for i, s in enumerate(statuses, 1):
        print(f"    {i}. {s}")

    imm = p.get("immigration", {})
    current_status = imm.get("status", "F1-OPT")
    current_idx    = statuses.index(current_status) + 1 if current_status in statuses else 4

    choice = _ask(f"Immigration status (number)", str(current_idx))
    try:
        imm["status"] = statuses[int(choice) - 1]
    except (ValueError, IndexError):
        imm["status"] = current_status

    answers = IMMIGRATION_ANSWERS[imm["status"]]
    print(f"\n  Using: {imm['status']}")
    print(f"    Authorized to work in US:   {answers['authorized']}")
    print(f"    Requires sponsorship now:    {answers['sponsor_now']}")
    print(f"    Requires future sponsorship: {answers['sponsor_future']}")
    print(f"    Skip security clearance jobs: {answers['skip_clearance']}")
    p["immigration"] = imm

    # ── Education ─────────────────────────────────────────────────────────
    section("Education (highest degree)")
    edu = p.get("education", {})
    edu["degree"]          = _ask("Degree",          edu.get("degree", "Master of Science"))
    edu["field"]           = _ask("Field of study",  edu.get("field", "Information Technology Management"))
    edu["school"]          = _ask("University",       edu.get("school", "Indiana Wesleyan University"))
    edu["graduation_year"] = _ask("Graduation year",  edu.get("graduation_year", "2024"))
    p["education"] = edu

    # ── Salary ────────────────────────────────────────────────────────────
    section("Salary & Preferences")
    prefs = p.get("preferences", {})
    prefs["salary_min"]  = _ask("Minimum salary (number)",   str(prefs.get("salary_min", 90000)))
    prefs["salary_text"] = _ask("Salary range (text)",       prefs.get("salary_text", "95,000 - 110,000"))
    prefs["job_type"]    = _ask("Job type (Full-time/Contract/Both)", prefs.get("job_type", "Full-time"))
    p["preferences"] = prefs

    # ── Agent behavior ─────────────────────────────────────────────────────
    section("Agent Behavior")
    agent_cfg = p.get("agent", {})
    review = _ask("Review before each submission? (y/n)", "y" if agent_cfg.get("review_before_submit", True) else "n")
    agent_cfg["review_before_submit"] = review.lower() == "y"
    p["agent"] = agent_cfg

    # ── Save ───────────────────────────────────────────────────────────────
    save_profile(p)
    print("\n  Profile saved to profile.json")
    print("  The agent will use this automatically from now on.")
    return p


def get_or_setup_profile() -> dict:
    """Load profile if it exists, otherwise run setup."""
    profile = load_profile()
    if not profile or not profile.get("personal", {}).get("name"):
        print("\n  No profile found. Let's set it up (one time only).")
        profile = setup_profile()
    return profile
