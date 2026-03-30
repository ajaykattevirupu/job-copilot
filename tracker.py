"""
Application tracker — saves every application to a JSON file.
Simple, no database needed.
"""

import json
import os
from datetime import datetime

TRACKER_FILE = "applications.json"


def _load() -> list:
    if not os.path.exists(TRACKER_FILE):
        return []
    with open(TRACKER_FILE, "r") as f:
        return json.load(f)


def _save(applications: list):
    with open(TRACKER_FILE, "w") as f:
        json.dump(applications, f, indent=2)


def add_application(company: str, role: str, source: str, notes: str = "") -> dict:
    """Log a new job application."""
    applications = _load()
    app = {
        "id": len(applications) + 1,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": company,
        "role": role,
        "source": source,         # LinkedIn, Indeed, company portal, recruiter email
        "status": "Applied",
        "notes": notes,
        "history": [
            {"status": "Applied", "date": datetime.now().strftime("%Y-%m-%d %H:%M")}
        ],
    }
    applications.append(app)
    _save(applications)
    return app


def update_status(app_id: int, new_status: str, notes: str = ""):
    """Update the status of an application."""
    applications = _load()
    for app in applications:
        if app["id"] == app_id:
            app["status"] = new_status
            if notes:
                app["notes"] = notes
            app["history"].append({
                "status": new_status,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            _save(applications)
            return app
    return None


def get_all() -> list:
    return _load()


def is_already_applied(company: str, role: str) -> bool:
    """
    Check if we already applied to this company+role combo.
    Fuzzy match — catches 'SPS Commerce' vs 'SPS Commerce Inc.' etc.
    """
    applications = _load()
    company_lower = company.lower().strip()
    role_lower    = role.lower().strip()

    for app in applications:
        stored_company = app.get("company", "").lower().strip()
        stored_role    = app.get("role", "").lower().strip()

        # exact match
        if stored_company == company_lower and stored_role == role_lower:
            return True

        # fuzzy — company name contains or is contained in stored
        company_match = (
            company_lower in stored_company or
            stored_company in company_lower or
            # first 8 chars match (handles suffixes like Inc, LLC)
            (len(company_lower) >= 8 and company_lower[:8] == stored_company[:8])
        )
        role_match = (
            role_lower in stored_role or
            stored_role in role_lower
        )

        if company_match and role_match:
            return True

    return False


def get_stats() -> dict:
    applications = _load()
    if not applications:
        return {}

    status_counts = {}
    for app in applications:
        s = app["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "total": len(applications),
        "by_status": status_counts,
        "today": sum(
            1 for a in applications
            if a["date"].startswith(datetime.now().strftime("%Y-%m-%d"))
        ),
    }


def print_dashboard():
    """Print a clean summary of all applications."""
    applications = _load()
    stats = get_stats()

    if not applications:
        print("\n  No applications tracked yet.\n")
        return

    print("\n" + "="*65)
    print(f"  APPLICATIONS DASHBOARD")
    print("="*65)
    print(f"  Total: {stats['total']}  |  Today: {stats['today']}")
    print()

    # status breakdown
    for status, count in stats["by_status"].items():
        bar = "█" * count
        print(f"  {status:<20} {bar} {count}")

    print()
    print(f"  {'ID':<4} {'Date':<12} {'Company':<20} {'Role':<25} {'Status'}")
    print(f"  {'-'*4} {'-'*12} {'-'*20} {'-'*25} {'-'*15}")

    # show most recent 20
    for app in reversed(applications[-20:]):
        print(
            f"  {app['id']:<4} {app['date'][:10]:<12} "
            f"{app['company'][:19]:<20} {app['role'][:24]:<25} {app['status']}"
        )
    print("="*65 + "\n")
