"""
FastAPI server — serves the dashboard and handles WebSocket connections.

Run with:
    python -m webapp.app
    or
    python run.py

Then open: http://localhost:8000
"""

import sys
import os
import re
import asyncio
import threading
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

from webapp.agent_bridge import bridge

app = FastAPI(title="xHR")

# serve static files (CSS, JS)
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── WebSocket connection manager ───────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


RESUME_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resume.txt")

@app.post("/api/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    try:
        import io
        content = await file.read()
        name = (file.filename or "").lower()

        if name.endswith(".txt"):
            text = content.decode("utf-8", errors="ignore")

        elif name.endswith(".docx"):
            from docx import Document
            doc = Document(io.BytesIO(content))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        elif name.endswith(".pdf"):
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(content))
                text = "\n".join(
                    page.extract_text() or "" for page in reader.pages
                ).strip()
            except ImportError:
                try:
                    import pdfplumber
                    with pdfplumber.open(io.BytesIO(content)) as pdf:
                        text = "\n".join(
                            p.extract_text() or "" for p in pdf.pages
                        ).strip()
                except ImportError:
                    return JSONResponse(
                        {"ok": False, "error": "PDF parsing requires pypdf or pdfplumber. "
                         "Run: pip install pypdf  or use a .txt/.docx file instead."},
                        status_code=400
                    )
        else:
            text = content.decode("utf-8", errors="ignore")

        text = text.strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Could not extract text from file."}, status_code=400)

        os.makedirs(os.path.dirname(RESUME_PATH), exist_ok=True)
        with open(RESUME_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        # Save original filename stem so tailored resumes use the same name
        original_stem = os.path.splitext(file.filename)[0] if file.filename else "Resume"
        resume_name_path = os.path.join(os.path.dirname(RESUME_PATH), "resume_name.txt")
        with open(resume_name_path, "w", encoding="utf-8") as f:
            f.write(original_stem)
        return {"ok": True, "chars": len(text), "filename": file.filename}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


RESUMES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "resumes")

@app.get("/api/resumes")
async def list_resumes():
    """List all AI-tailored resumes saved to output/resumes/."""
    try:
        if not os.path.exists(RESUMES_DIR):
            return []
        stems = {}
        for fname in sorted(os.listdir(RESUMES_DIR), reverse=True):
            stem, ext = os.path.splitext(fname)
            if ext not in (".txt", ".pdf", ".docx", ".json"):
                continue
            if stem not in stems:
                stems[stem] = {"stem": stem, "has_txt": False, "has_pdf": False, "has_docx": False}
            if ext == ".txt":  stems[stem]["has_txt"]  = True
            if ext == ".pdf":  stems[stem]["has_pdf"]  = True
            if ext == ".docx": stems[stem]["has_docx"] = True
        # Stem format: {Name}_{Role}_{Company}_{YYYY}_{MMDD}
        # Last 2 underscore-parts are year + date; everything before is label
        result = []
        for s in stems.values():
            parts = s["stem"].rsplit("_", 2)
            if len(parts) == 3:
                label = parts[0].replace("_", " ").strip()
                date  = f"{parts[1]}-{parts[2]}"   # e.g. "2026-0324"
            else:
                label = s["stem"].replace("_", " ").strip()
                date  = ""
            result.append({**s, "label": label, "date": date})
        # Sort newest first (stems are already reverse-sorted by filename)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/resumes/{stem}")
async def delete_resume(stem: str):
    """Delete all files associated with a resume stem."""
    try:
        safe = os.path.basename(stem)
        deleted = []
        for ext in (".txt", ".docx", ".pdf", ".json"):
            path = os.path.join(RESUMES_DIR, safe + ext)
            if os.path.exists(path):
                os.remove(path)
                deleted.append(ext)
        if not deleted:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/resumes/text/{stem}")
async def get_resume_text(stem: str):
    """Return the plain-text content of a saved resume plus skills analysis."""
    try:
        safe = os.path.basename(stem)
        path = os.path.join(RESUMES_DIR, safe + ".txt")
        if not os.path.exists(path):
            return JSONResponse({"error": "Not found"}, status_code=404)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        # Load sidecar skills JSON if it exists
        skills = {}
        json_path = os.path.join(RESUMES_DIR, safe + ".json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as jf:
                    skills = json.load(jf)
            except Exception:
                pass
        return {"stem": safe, "text": text, "skills": skills}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/resumes/text/{stem}")
async def save_resume_text(stem: str, body: dict):
    """Save edited resume text back to the .txt file."""
    try:
        safe = os.path.basename(stem)
        path = os.path.join(RESUMES_DIR, safe + ".txt")
        if not os.path.exists(path):
            return JSONResponse({"error": "Not found"}, status_code=404)
        text = body.get("text", "")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/resumes/download/{filename}")
async def download_resume(filename: str):
    """Serve a PDF or DOCX resume file for download."""
    safe = os.path.basename(filename)
    path = os.path.join(RESUMES_DIR, safe)
    if not os.path.exists(path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(path, filename=safe)


@app.get("/api/applications")
async def get_applications():
    try:
        from tracker import get_all
        return get_all()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/profile")
async def get_profile():
    try:
        import json
        profile_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profile.json")
        if os.path.exists(profile_path):
            with open(profile_path, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    loop = asyncio.get_event_loop()

    # register bridge so the agent thread can send messages here
    bridge.register_ws(manager.broadcast, loop)

    # send current state to newly connected client
    await websocket.send_text(json.dumps({
        "type":      "init",
        "is_running": bridge.is_running,
        "stats":      bridge.stats,
        "log":        bridge.activity_log[-50:],   # last 50 entries
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            await handle_message(data)

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        bridge.unregister_ws()


async def handle_message(data: dict):
    """Handle messages from the browser."""
    msg_type = data.get("type")

    if msg_type == "start_agent":
        config = data.get("config", {})
        # Apply mode settings from config
        bridge.permission  = config.get("permission",   "ask")
        bridge.effort      = config.get("effort",       "med")
        bridge.continuous  = config.get("continuous",   False)
        bridge.post_enabled = config.get("post_enabled", False)
        thread = threading.Thread(
            target=run_agent_thread,
            args=(config,),
            daemon=True,
        )
        thread.start()

    elif msg_type == "stop_agent":
        bridge.is_running = False
        bridge.log("Stop requested by user", level="warn")

    elif msg_type == "set_mode":
        key = data.get("key")
        val = data.get("value")
        if key == "permission":
            bridge.permission = val
            bridge.log(f"Mode → {val}", level="info", tool="System")
        elif key == "effort":
            bridge.effort = val
            bridge.log(f"Effort → {val}", level="info", tool="System")
        elif key == "post":
            bridge.post_enabled = bool(val)
            bridge.log(f"LinkedIn posting {'enabled' if val else 'disabled'}", level="info", tool="System")

    elif msg_type == "chat":
        msg = data.get("message", "").strip()
        if not msg:
            return
        bridge.log(f"User: {msg}", level="info", tool="Chat")
        # Handle "post to linkedin" even mid-run
        if re.search(r"post.*(linkedin|update|status)|linkedin.*post", msg, re.I):
            post_text = re.sub(
                r"post.*?(?:to linkedin|linkedin|about|:)\s*", "", msg, flags=re.I
            ).strip() or (
                "I'm actively applying to Java Full Stack roles. "
                "Open to exciting opportunities! #OpenToWork #Java #FullStack"
            )
            thread = threading.Thread(
                target=_run_linkedin_post, args=(post_text,), daemon=True
            )
            thread.start()

    elif msg_type == "approval":
        approved = data.get("approved", False)
        bridge.receive_approval(approved)

    elif msg_type == "ping":
        pass  # keep-alive


# ── LinkedIn post helper ────────────────────────────────────────────────────────

def _run_linkedin_post(text: str):
    """Run a LinkedIn post in a background thread (reuses any open browser session)."""
    import traceback
    try:
        from playwright.sync_api import sync_playwright
        from agent.linkedin_agent import LinkedInAgent
        from tailor import get_client

        api_key = os.environ.get("OPENAI_API_KEY", "")
        client  = get_client(api_key)

        bridge.log(f"Starting LinkedIn post…", level="info", tool="LinkedIn")
        pw  = sync_playwright().start()
        from agent.browser import launch
        ctx  = launch(pw)
        page = ctx.new_page()

        agent = LinkedInAgent(
            email="", password="", openai_client=client,
            resume_text="", profile={}, bridge=bridge,
        )
        agent.ctx  = ctx
        agent.page = page
        agent.playwright = pw

        agent.post_update(text)
        ctx.close()
        pw.stop()
    except Exception as e:
        bridge.log(f"Post error: {e}", level="error", tool="LinkedIn")
        bridge.log(traceback.format_exc(), level="error", tool="LinkedIn")


# ── Agent thread ───────────────────────────────────────────────────────────────

def run_agent_thread(config: dict):
    """
    Orchestrates all selected platforms sequentially in a background thread.
    LinkedIn → Indeed → Dice → Gmail
    All output flows through the bridge to the browser.
    """
    import traceback

    try:
        from user_profile import get_or_setup_profile
        from tailor import get_client

        profile  = get_or_setup_profile()
        api_key  = config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        client   = get_client(api_key)
        platforms = config.get("platforms", {})

        resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resume.txt")
        with open(resume_path, "r", encoding="utf-8") as f:
            resume = f.read().strip()

        job_config = {
            "query":             config.get("query", "Java Full Stack Engineer"),
            "location":          config.get("location", "United States"),
            "max_applications":  int(config.get("max_applications", 30)),
            "date_filter":       config.get("date_filter", "week"),
            "job_types":         config.get("job_types", []),
            "work_modes":        config.get("work_modes", []),
            "experience_levels": config.get("experience_levels", []),
            "easy_apply_only":   bool(config.get("easy_apply_only", False)),
            "sort_by":           config.get("sort_by", "DD"),
        }

        bridge.agent_started(job_config)

        # ── LinkedIn ──────────────────────────────────────────────
        linkedin_agent = None
        if platforms.get("linkedin", True):
            bridge.log("Starting LinkedIn…", tool="LinkedIn")
            try:
                from agent.linkedin_agent import LinkedInAgent
                linkedin_agent = LinkedInAgent(
                    email         = config.get("linkedin_email", ""),
                    password      = config.get("linkedin_password", ""),
                    openai_client = client,
                    resume_text   = resume,
                    profile       = profile,
                    bridge        = bridge,
                )
                # Keep browser alive if Gmail will reuse the same context
                linkedin_agent.run(**job_config, keep_alive=bool(platforms.get("gmail")))
            except Exception as e:
                bridge.log(f"LinkedIn error: {e}", level="error", tool="LinkedIn")
                bridge.log(traceback.format_exc(), level="error", tool="LinkedIn")

        # ── Indeed ────────────────────────────────────────────────
        if platforms.get("indeed"):
            bridge.log("Starting Indeed…", tool="Indeed")
            try:
                from agent.indeed_agent import IndeedAgent
                indeed = IndeedAgent(client, resume, profile, bridge=bridge)
                indeed.run(**job_config)
            except Exception as e:
                bridge.log(f"Indeed error: {e}", level="error", tool="Indeed")
                bridge.log(traceback.format_exc(), level="error", tool="Indeed")

        # ── Dice ──────────────────────────────────────────────────
        if platforms.get("dice"):
            bridge.log("Starting Dice…", tool="Dice")
            try:
                from agent.dice_agent import DiceAgent
                dice = DiceAgent(
                    openai_client = client,
                    resume_text   = resume,
                    profile       = profile,
                    email         = config.get("dice_email", ""),
                    password      = config.get("dice_password", ""),
                    bridge        = bridge,
                )
                dice.run(**job_config)
            except Exception as e:
                bridge.log(f"Dice error: {e}", level="error", tool="Dice")
                import traceback
                bridge.log(traceback.format_exc(), level="error", tool="Dice")

        # ── Gmail replies ─────────────────────────────────────────
        if platforms.get("gmail"):
            bridge.log("Starting Gmail reply agent…", tool="Gmail")
            try:
                from agent.gmail_agent import GmailAgent
                # Reuse LinkedIn's browser context if available (avoids same-profile conflict)
                shared_ctx = linkedin_agent.ctx if linkedin_agent and linkedin_agent.ctx else None
                gmail = GmailAgent(client, profile, resume, bridge=bridge, ctx=shared_ctx)
                gmail.run(max_replies=20)
            except Exception as e:
                bridge.log(f"Gmail error: {e}", level="error", tool="Gmail")
                bridge.log(traceback.format_exc(), level="error", tool="Gmail")
            finally:
                # Now safe to close the LinkedIn browser
                if linkedin_agent:
                    try:
                        linkedin_agent.close_browser()
                    except Exception:
                        pass
                    linkedin_agent = None

    except Exception as e:
        bridge.log(f"Fatal error: {e}", level="error", tool="System")
        bridge.log(traceback.format_exc(), level="error", tool="System")
    finally:
        bridge.agent_stopped()

# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  xHR running at http://localhost:8000\n")
    uvicorn.run("webapp.app:app", host="0.0.0.0", port=8000, reload=True)
