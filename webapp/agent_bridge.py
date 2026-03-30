"""
Agent Bridge — thread-safe communication between the agent thread and WebSocket.

Agent thread  ←→  AgentBridge  ←→  WebSocket  ←→  Browser
"""

import threading
import asyncio
import json
from datetime import datetime
from typing import Optional, Callable


class AgentBridge:

    def __init__(self):
        self._approval_event  = threading.Event()
        self._approval_result = False
        self._send_callback: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.is_running   = False
        self.stats        = {"applied": 0, "skipped": 0, "pending": 0, "emails": 0}
        self.current_job  = {}
        self.activity_log = []

        # Mode state (updated by UI messages)
        self.permission  = "ask"   # "ask" | "auto" | "plan"
        self.effort      = "med"   # "low" | "med" | "high"
        self.continuous  = False   # 24/7 mode
        self.post_enabled = False  # LinkedIn posting allowed

    # ── WebSocket registration ─────────────────────────────────────────────

    def register_ws(self, send_callback: Callable, loop: asyncio.AbstractEventLoop):
        self._send_callback = send_callback
        self._loop = loop

    def unregister_ws(self):
        self._send_callback = None
        self._loop = None

    # ── Sending messages to browser ────────────────────────────────────────

    def _send(self, message: dict):
        if not self._send_callback or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_callback(json.dumps(message)),
                self._loop,
            )
        except Exception:
            pass

    def log(self, message: str, level: str = "info",
            job: dict = None, tool: str = None):
        """
        Send a log entry to the live feed in the UI.
        level: "info" | "success" | "skip" | "warn" | "error"
        tool:  displayed as the block header (e.g. "LinkedIn", "Indeed", "Gmail")
        """
        _defaults = {
            "info":    "Info",
            "success": "Applied",
            "skip":    "Skipped",
            "warn":    "Warning",
            "error":   "Error",
        }
        entry = {
            "type":    "log",
            "tool":    tool or _defaults.get(level, "Info"),
            "time":    datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "level":   level,
            "job":     job or {},
        }
        self.activity_log.append(entry)
        if len(self.activity_log) > 200:
            self.activity_log.pop(0)
        self._send(entry)

    def tailor_progress(self, steps: list, job: dict = None, resume_preview: str = "",
                        entry_id: str = "", ats_score: int = 0):
        """
        Stream step-by-step resume tailoring progress to the feed.
        steps: list of {"label": str, "done": bool, "active": bool}
        entry_id: stable ID per job so the UI updates the same card in-place.
        """
        j = job or {}
        if not entry_id:
            import re
            raw = f"{j.get('company','')}-{j.get('title','')}"
            entry_id = re.sub(r"[^a-z0-9]", "-", raw.lower())[:40]
        msg = {
            "type":           "tailor_progress",
            "entry_id":       entry_id,
            "steps":          steps,
            "job":            j,
            "resume_preview": resume_preview,
            "ats_score":      ats_score,
            "time":           datetime.now().strftime("%H:%M:%S"),
        }
        self._send(msg)

    def update_stats(self):
        self._send({"type": "stats", **self.stats})

    def update_status(self, status: str):
        self._send({"type": "status", "status": status})

    # ── Convenience increment helpers ──────────────────────────────────────

    def inc_applied(self):
        self.stats["applied"] += 1
        self.update_stats()

    def inc_skipped(self):
        self.stats["skipped"] += 1
        self.update_stats()

    def inc_emails(self):
        self.stats["emails"] += 1
        self.update_stats()

    # ── Approval flow ──────────────────────────────────────────────────────

    def request_approval(self, job: dict, tailored_resume: str,
                         modal_type: str = "apply",
                         cover_letter: str = "", ats_score: int = 0) -> bool:
        """
        Called by the agent thread when ready to submit.
        Behaviour depends on permission mode:
          ask  → show approval modal, block until user clicks
          auto → approve immediately (no modal)
          plan → skip always (plan mode never submits)
        """
        # ── Auto mode: apply without asking ───────────────────────────
        if self.permission == "auto":
            self.log(
                f"Auto-applying — {job.get('title')} @ {job.get('company')}",
                level="success", job=job,
            )
            return True

        # ── Plan mode: never submit ────────────────────────────────────
        if self.permission == "plan":
            self.log(
                f"[Plan] Would apply to {job.get('title')} @ {job.get('company')}",
                level="info", job=job,
            )
            return False

        # ── Ask mode: show modal, block ────────────────────────────────
        self._approval_event.clear()
        self._approval_result = False
        self.stats["pending"] = 1
        self.update_stats()

        self._send({
            "type":         "approval_required",
            "modal_type":   modal_type,
            "job":          job,
            "resume":       tailored_resume[:3000],
            "cover_letter": cover_letter[:2000],
            "ats_score":    ats_score,
            "time":         datetime.now().strftime("%H:%M:%S"),
        })

        self.log(
            f"Awaiting approval — {job.get('title')} @ {job.get('company')}",
            level="warn", job=job,
        )

        # OS desktop notification so user knows to check the browser
        self.notify(
            title="Job Copilot — Approval Needed",
            message=f"{job.get('title', 'Job')} @ {job.get('company', '')}",
        )

        self._approval_event.wait(timeout=90)    # 90 sec timeout — LinkedIn modal times out at ~2 min
        self.stats["pending"] = 0
        self.update_stats()

        if self._approval_result:
            self.log("Approved — submitting", level="success", job=job)
        else:
            self.log("Skipped by user", level="skip", job=job)

        return self._approval_result

    def receive_approval(self, approved: bool):
        self._approval_result = approved
        self._approval_event.set()

    # ── Desktop notification ────────────────────────────────────────────────

    def notify(self, title: str = "Job Copilot", message: str = "Action required"):
        """Show a non-intrusive OS desktop notification via plyer."""
        try:
            from plyer import notification
            notification.notify(
                title=title,
                message=message,
                app_name="Job Copilot",
                timeout=8,
            )
        except Exception:
            pass  # plyer not installed or OS doesn't support notifications

    # ── Agent lifecycle ────────────────────────────────────────────────────

    def agent_started(self, config: dict):
        self.is_running   = True
        self.stats        = {"applied": 0, "skipped": 0, "pending": 0, "emails": 0}
        self.activity_log = []
        self._send({"type": "agent_started", "config": config})
        self.log("Agent started", level="info", tool="System")

    def agent_stopped(self):
        self.is_running = False
        self._send({"type": "agent_stopped", "stats": self.stats})
        self.log(
            f"Done — Applied: {self.stats['applied']}  "
            f"Skipped: {self.stats['skipped']}  "
            f"Emails: {self.stats['emails']}",
            level="success",
            tool="System",
        )


# Global singleton shared by app.py and all agent threads
bridge = AgentBridge()
