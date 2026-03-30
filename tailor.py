"""
Core AI logic — calls OpenAI to tailor resumes, draft emails, analyze fit.
"""

from openai import OpenAI
from prompts import (
    TAILOR_RESUME_PROMPT,
    DRAFT_EMAIL_PROMPT,
    ANALYZE_FIT_PROMPT,
    EXTRACT_JD_INFO_PROMPT,
    RELEVANCE_CHECK_PROMPT,
    SKILLS_GAP_PROMPT,
)


def get_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)


def _call_openai(client: OpenAI, prompt: str, max_tokens: int = 2000) -> str:
    """Single OpenAI API call — returns the text response."""
    response = client.chat.completions.create(
        model="gpt-4o-mini",  # fast + cheap for daily use (~30x cheaper than gpt-4o)
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def tailor_resume(client: OpenAI, resume: str, jd: str) -> str:
    """Rewrite the resume to match the job description."""
    prompt = TAILOR_RESUME_PROMPT.format(resume=resume, jd=jd)
    return _call_openai(client, prompt, max_tokens=3000)


def generate_cover_letter(client: OpenAI, resume: str, jd: str,
                          job_title: str = "", company: str = "") -> str:
    """Write a 3-paragraph tailored cover letter for the given job."""
    from prompts import COVER_LETTER_PROMPT
    prompt = COVER_LETTER_PROMPT.format(
        job_title=job_title, company=company, jd=jd[:3000], resume=resume[:3000]
    )
    return _call_openai(client, prompt, max_tokens=400)


def draft_email(client: OpenAI, resume: str, recruiter_message: str) -> str:
    """Draft a reply to a recruiter or vendor message."""
    prompt = DRAFT_EMAIL_PROMPT.format(resume=resume, recruiter_message=recruiter_message)
    return _call_openai(client, prompt, max_tokens=500)


def analyze_fit(client: OpenAI, resume: str, jd: str) -> str:
    """Score how well the candidate fits the JD."""
    prompt = ANALYZE_FIT_PROMPT.format(resume=resume, jd=jd)
    return _call_openai(client, prompt, max_tokens=800)


def extract_jd_info(client: OpenAI, jd: str) -> str:
    """Pull out key info from a JD before deciding to apply."""
    prompt = EXTRACT_JD_INFO_PROMPT.format(jd=jd)
    return _call_openai(client, prompt, max_tokens=600)


def analyze_skills_gap(client: OpenAI, jd: str, resume: str) -> dict:
    """
    Compare tailored resume against JD — returns strong skills and missing skills.
    Saves cleanly as a sidecar .json next to the resume file.
    """
    prompt = SKILLS_GAP_PROMPT.format(jd=jd, resume=resume)
    raw = _call_openai(client, prompt, max_tokens=200)
    result = {"strong": [], "missing": [], "tip": "", "score": 0, "raw": raw}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                result["score"] = int(line.split(":", 1)[1].strip().split("/")[0])
            except (ValueError, IndexError):
                pass
        elif line.startswith("STRONG:"):
            result["strong"] = [s.strip() for s in line.split(":", 1)[1].split(",") if s.strip()]
        elif line.startswith("MISSING:"):
            val = line.split(":", 1)[1].strip()
            result["missing"] = [] if val.lower() == "none" else [s.strip() for s in val.split(",") if s.strip()]
        elif line.startswith("TIP:"):
            result["tip"] = line.split(":", 1)[1].strip()
    return result


def check_relevance(client: OpenAI, jd: str) -> dict:
    """
    Fast relevance gate — call this BEFORE tailoring.
    Returns dict with keys: decision, score, reason, missing, raw
    """
    prompt = RELEVANCE_CHECK_PROMPT.format(jd=jd)
    raw = _call_openai(client, prompt, max_tokens=150)

    result = {"decision": "MAYBE", "score": 5, "reason": "", "missing": "", "learn": "", "raw": raw}

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("DECISION:"):
            result["decision"] = line.split(":", 1)[1].strip().upper()
        elif line.startswith("SCORE:"):
            try:
                result["score"] = int(line.split(":", 1)[1].strip().split("/")[0])
            except ValueError:
                pass
        elif line.startswith("REASON:"):
            result["reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("MISSING:"):
            result["missing"] = line.split(":", 1)[1].strip()
        elif line.startswith("LEARN:"):
            result["learn"] = line.split(":", 1)[1].strip()

    return result
