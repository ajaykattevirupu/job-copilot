"""
job-copilot — your personal AI job application assistant
---------------------------------------------------------
Usage:
    python main.py

Make sure resume.txt has your master resume before running.
Set ANTHROPIC_API_KEY in your environment or paste it on first run.
"""

import os
import sys
from datetime import datetime
from tailor import get_client, tailor_resume, draft_email, analyze_fit, extract_jd_info, check_relevance
from tracker import add_application, update_status, print_dashboard, get_all


# ─── Helpers ────────────────────────────────────────────────────────────────

def clear():
    os.system("cls" if os.name == "nt" else "clear")


def load_resume() -> str:
    if not os.path.exists("resume.txt"):
        print("\n  ERROR: resume.txt not found.")
        print("  Create a file called resume.txt in this folder with your master resume.\n")
        sys.exit(1)
    with open("resume.txt", "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print("\n  ERROR: resume.txt is empty. Paste your resume into it first.\n")
        sys.exit(1)
    return content


def paste_multiline(prompt_text: str) -> str:
    """Let user paste multiple lines. Type END on a new line to finish."""
    print(f"\n{prompt_text}")
    print("(Paste your text, then type END on a new line and press Enter)\n")
    lines = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def save_output(filename: str, content: str) -> str:
    """Save output to the output/ folder."""
    os.makedirs("output", exist_ok=True)
    path = os.path.join("output", filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def get_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("\n  OpenAI API key not found in environment.")
        key = input("  Paste your API key: ").strip()
    return key


# ─── Menu Actions ────────────────────────────────────────────────────────────

def action_tailor(client, resume: str):
    """Tailor resume to a job description."""
    jd = paste_multiline("Paste the Job Description:")
    if not jd:
        print("  No JD provided. Cancelled.")
        return

    # ── Relevance gate ────────────────────────────────────
    print("\n  Checking relevance...")
    rel = check_relevance(client, jd)

    score_bar = "█" * rel["score"] + "░" * (10 - rel["score"])
    print(f"\n  [{score_bar}] {rel['score']}/10  →  {rel['decision']}")
    print(f"  {rel['reason']}")
    if rel["missing"] and rel["missing"].lower() != "none":
        print(f"  Missing: {rel['missing']}")

    if rel["decision"] == "SKIP":
        print("\n  SKIPPED — not relevant to your stack. Move on.\n")
        return

    if rel["decision"] == "APPLY_LEARN":
        learn = rel.get("learn", "")
        if learn and learn.lower() != "none":
            print(f"\n  Missing skills (learnable): {learn}")
            print("  These will be added to your learning plan.")
        proceed = input("\n  Good match with gaps — apply? (y/n): ").strip().lower()
        if proceed != "y":
            return

    if rel["decision"] == "MAYBE":
        proceed = input("\n  Weak match. Apply anyway? (y/n): ").strip().lower()
        if proceed != "y":
            return
    # ─────────────────────────────────────────────────────

    print("\n  Analyzing fit...")
    fit = analyze_fit(client, resume, jd)
    print("\n" + "─"*60)
    print(fit)
    print("─"*60)

    proceed = input("\n  Continue tailoring resume? (y/n): ").strip().lower()
    if proceed != "y":
        return

    print("\n  Tailoring your resume...")
    tailored = tailor_resume(client, resume, jd)

    # save to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"resume_tailored_{timestamp}.txt"
    path = save_output(filename, tailored)

    print(f"\n  Saved to: {path}")
    print("\n" + "─"*60)
    print(tailored)
    print("─"*60)

    # log the application
    log_it = input("\n  Log this as an application? (y/n): ").strip().lower()
    if log_it == "y":
        company = input("  Company name: ").strip()
        role = input("  Role title: ").strip()
        source = input("  Source (LinkedIn/Indeed/Portal/Other): ").strip()
        app = add_application(company, role, source)
        print(f"\n  Logged! Application #{app['id']} — {company} / {role}")


def action_draft_email(client, resume: str):
    """Draft a reply to a recruiter message."""
    msg = paste_multiline("Paste the recruiter message or job description:")
    if not msg:
        print("  Nothing pasted. Cancelled.")
        return

    # ── Relevance gate ────────────────────────────────────
    print("\n  Checking relevance...")
    rel = check_relevance(client, msg)

    score_bar = "█" * rel["score"] + "░" * (10 - rel["score"])
    print(f"\n  [{score_bar}] {rel['score']}/10  →  {rel['decision']}")
    print(f"  {rel['reason']}")
    if rel["missing"] and rel["missing"].lower() != "none":
        print(f"  Missing: {rel['missing']}")

    if rel["decision"] == "SKIP":
        print("\n  SKIPPED — not relevant to your stack. Don't reply.\n")
        return

    if rel["decision"] == "APPLY_LEARN":
        learn = rel.get("learn", "")
        if learn and learn.lower() != "none":
            print(f"\n  Missing skills (learnable): {learn}")
        proceed = input("\n  Good match with gaps — reply? (y/n): ").strip().lower()
        if proceed != "y":
            return

    if rel["decision"] == "MAYBE":
        proceed = input("\n  Weak match. Reply anyway? (y/n): ").strip().lower()
        if proceed != "y":
            return
    # ─────────────────────────────────────────────────────

    print("\n  Drafting your reply...")
    reply = draft_email(client, resume, msg)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = save_output(f"email_reply_{timestamp}.txt", reply)

    print(f"\n  Saved to: {path}")
    print("\n" + "─"*60)
    print(reply)
    print("─"*60)

    # log it
    log_it = input("\n  Log this as an application? (y/n): ").strip().lower()
    if log_it == "y":
        company = input("  Company/Recruiter name: ").strip()
        role = input("  Role title: ").strip()
        app = add_application(company, role, source="Recruiter Email")
        print(f"\n  Logged! Application #{app['id']} — {company} / {role}")


def action_analyze_jd(client, resume: str):
    """Quickly analyze a JD before deciding to apply."""
    jd = paste_multiline("Paste the Job Description:")
    if not jd:
        return

    print("\n  Extracting key info...")
    info = extract_jd_info(client, jd)
    print("\n" + "─"*60)
    print(info)
    print("─"*60)

    print("\n  Calculating your fit score...")
    fit = analyze_fit(client, resume, jd)
    print("\n" + "─"*60)
    print(fit)
    print("─"*60)


def action_update_status():
    """Update the status of an existing application."""
    print_dashboard()
    apps = get_all()
    if not apps:
        return

    try:
        app_id = int(input("  Enter application ID to update: ").strip())
    except ValueError:
        print("  Invalid ID.")
        return

    print("\n  Status options:")
    statuses = [
        "Applied", "Phone Screen", "Technical Round", "Final Round",
        "Offer", "Rejected", "Ghosted", "Withdrawn"
    ]
    for i, s in enumerate(statuses, 1):
        print(f"    {i}. {s}")

    try:
        choice = int(input("\n  Choose status (number): ").strip())
        new_status = statuses[choice - 1]
    except (ValueError, IndexError):
        print("  Invalid choice.")
        return

    notes = input("  Notes (optional, press Enter to skip): ").strip()
    app = update_status(app_id, new_status, notes)
    if app:
        print(f"\n  Updated! #{app_id} → {new_status}")
    else:
        print(f"\n  Application #{app_id} not found.")


# ─── Agent Actions ────────────────────────────────────────────────────────────

def action_linkedin_agent(client, resume: str):
    """Launch the LinkedIn Easy Apply agent."""
    from agent.linkedin_agent import LinkedInAgent
    from user_profile import get_or_setup_profile, setup_profile

    # load or create profile
    profile = get_or_setup_profile()
    personal = profile.get("personal", {})

    print("\n  LinkedIn Agent Setup")
    print("  ─────────────────────────────────────────")
    print(f"  Profile loaded: {personal.get('name')} | {personal.get('email')}")
    print(f"  Immigration: {profile.get('immigration', {}).get('status', 'unknown')}")
    imm_answers = profile.get("immigration", {})
    skip_clr = any([
        "F1" in imm_answers.get("status", ""),
        "H1B" in imm_answers.get("status", ""),
        "L1" in imm_answers.get("status", ""),
    ])
    if skip_clr:
        print("  Security clearance jobs: SKIPPED automatically")

    re_setup = input("\n  Update profile before starting? (y/n): ").strip().lower()
    if re_setup == "y":
        profile = setup_profile()

    li_email    = input("\n  LinkedIn email: ").strip()
    li_password = input("  LinkedIn password: ").strip()

    prefs    = profile.get("preferences", {})
    query    = input(f"  Job title [{prefs.get('job_titles', ['Java Full Stack Engineer'])[0]}]: ").strip()
    query    = query or prefs.get("job_titles", ["Java Full Stack Engineer"])[0]
    location = input(f"  Location [{prefs.get('location', 'United States')}]: ").strip()
    location = location or prefs.get("location", "United States")

    max_apps    = input("  Max applications (default 30): ").strip()
    max_apps    = int(max_apps) if max_apps.isdigit() else 30

    print("  Date filter: 1=Last 24h  2=Last week (default)  3=Last month  4=Any time")
    date_choice = input("  Choose (1-4): ").strip()
    date_filter = {"1": "24h", "2": "week", "3": "month", "4": "any"}.get(date_choice, "week")

    review = input("  Review before each submission? (y/n, default y): ").strip().lower()
    profile.setdefault("agent", {})["review_before_submit"] = (review != "n")

    print(f"\n  Starting: {max_apps} jobs | {query} | {location} | posted: {date_filter}")
    print("  Skips: already applied | clearance jobs | citizenship-only | wrong stack")
    print("  APPLY_LEARN: applies and saves learning plan for skill gaps\n")

    agent = LinkedInAgent(li_email, li_password, client, resume, profile)
    agent.run(query=query, location=location, max_applications=max_apps, date_filter=date_filter)


def action_email_monitor(client, resume: str):
    """Start the Gmail recruiter monitor."""
    from agent.email_monitor import EmailMonitor

    print("\n  Gmail Monitor Setup")
    print("  ─────────────────────────────────────────")
    print("  You need a Gmail App Password (not your regular password).")
    print("  Get one at: Google Account → Security → App Passwords\n")
    gmail     = input("  Gmail address: ").strip()
    app_pass  = input("  Gmail App Password: ").strip()
    interval  = input("  Check every N seconds (default: 60): ").strip()
    interval  = int(interval) if interval.isdigit() else 60

    monitor = EmailMonitor(gmail, app_pass, client, resume)
    monitor.run(interval_seconds=interval)


# ─── Main Loop ───────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  JOB COPILOT — AI Job Application Agent")
    print("="*60)

    api_key = get_api_key()
    client  = get_client(api_key)
    resume  = load_resume()

    print(f"\n  Resume loaded ({len(resume.split())} words)")
    print("  Ready.\n")

    while True:
        print("\n" + "─"*60)
        print("  What do you want to do?")
        print()
        print("  ── Manual tools ──────────────────────────")
        print("  1. Tailor resume to a Job Description")
        print("  2. Draft reply to a recruiter/vendor email")
        print("  3. Analyze a JD (fit score before applying)")
        print()
        print("  ── Agents (fully automatic) ───────────────")
        print("  4. LinkedIn Easy Apply Agent  (auto-applies jobs)")
        print("  5. Gmail Monitor              (auto-drafts recruiter replies)")
        print()
        print("  ── Tracker ───────────────────────────────")
        print("  6. View application dashboard")
        print("  7. Update application status")
        print("  8. Exit")
        print()

        choice = input("  Choose (1-8): ").strip()

        if choice == "1":
            action_tailor(client, resume)
        elif choice == "2":
            action_draft_email(client, resume)
        elif choice == "3":
            action_analyze_jd(client, resume)
        elif choice == "4":
            action_linkedin_agent(client, resume)
        elif choice == "5":
            action_email_monitor(client, resume)
        elif choice == "6":
            print_dashboard()
        elif choice == "7":
            action_update_status()
        elif choice == "8":
            print("\n  Good luck with the applications!\n")
            break
        else:
            print("  Invalid choice. Pick 1-8.")


if __name__ == "__main__":
    main()
