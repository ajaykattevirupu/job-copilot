"""
Convert plain-text resume to a clean PDF for uploading to job applications.
Uses fpdf2 — lightweight, no external dependencies.
"""

from fpdf import FPDF
import os
from datetime import datetime


# Helvetica (built-in) only supports Latin-1. Map common Unicode to ASCII.
_UNICODE_MAP = str.maketrans({
    '\u2022': '-',   # bullet •
    '\u2023': '-',   # triangular bullet ‣
    '\u25aa': '-',   # small black square ▪
    '\u25cf': '-',   # black circle ●
    '\u2013': '-',   # en dash –
    '\u2014': '-',   # em dash —
    '\u2018': "'",   # left single quote '
    '\u2019': "'",   # right single quote '
    '\u201c': '"',   # left double quote "
    '\u201d': '"',   # right double quote "
    '\u2026': '...', # ellipsis …
    '\u2192': '->',  # right arrow →
    '\u2190': '<-',  # left arrow ←
    '\u00b7': '-',   # middle dot ·
    '\u00a0': ' ',   # non-breaking space
    '\u2019': "'",
})

def _clean(text: str) -> str:
    """Normalize Unicode characters to Helvetica-safe Latin-1."""
    text = text.translate(_UNICODE_MAP)
    # Catch anything else outside Latin-1
    return text.encode('latin-1', errors='replace').decode('latin-1')


class ResumePDF(FPDF):
    def header(self):
        pass  # no default header

    def footer(self):
        pass  # no default footer


def generate_pdf(resume_text: str, output_path: str = None) -> str:
    """
    Convert a plain-text resume to PDF.
    Returns the path to the saved PDF.
    """
    if output_path is None:
        os.makedirs("output", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join("output", f"resume_{timestamp}.pdf")

    pdf = ResumePDF()
    pdf.add_page()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=15)

    lines = resume_text.split("\n")

    for line in lines:
        line = _clean(line.rstrip())

        # Name line (first non-empty line) — large bold
        if line and pdf.get_y() < 20:
            pdf.set_font("Helvetica", "B", 16)
            pdf.cell(0, 8, line, ln=True, align="C")
            continue

        # Section headers — bold, slightly larger
        is_header = (
            line.isupper()
            or line.startswith("PROFESSIONAL")
            or line.startswith("TECHNICAL")
            or line.startswith("EDUCATION")
            or line.startswith("PROJECTS")
            or line.startswith("CERTIFICATIONS")
            or line.startswith("EXPERIENCE")
        )

        if is_header and line.strip():
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_fill_color(230, 230, 230)
            pdf.cell(0, 6, line, ln=True, fill=True)
            pdf.ln(1)
            continue

        # Contact line (email/phone/linkedin)
        if any(x in line for x in ["@", "linkedin", "github", "|", "http"]):
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 5, line, ln=True, align="C")
            continue

        # Bullet points
        if line.strip().startswith("•") or line.strip().startswith("-"):
            pdf.set_font("Helvetica", "", 9)
            # indent bullet points
            pdf.set_x(20)
            # handle long lines with multi_cell
            pdf.multi_cell(0, 5, line.strip(), ln=True)
            continue

        # Job title / company lines (non-empty, not bullet)
        if line.strip() and not line.strip().startswith("•"):
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(0, 5, line, ln=True)
            continue

        # Empty line — small gap
        if not line.strip():
            pdf.ln(2)

    pdf.output(output_path)
    return output_path
