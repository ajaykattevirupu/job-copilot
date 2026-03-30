"""
Job Copilot — Web Dashboard

Run this to start the browser dashboard:
    python run.py

Then open: http://localhost:8000
"""

import uvicorn

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  Job Copilot")
    print("  Open http://localhost:8000 in your browser")
    print("=" * 50 + "\n")
    uvicorn.run("webapp.app:app", host="0.0.0.0", port=8000, reload=False)
