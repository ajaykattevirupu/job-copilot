/* ── State ──────────────────────────────────────────────────── */
let ws = null, timer = null, secsLeft = 90, agentRunning = false;
let cmdHistory = [], histIdx = -1;
let _lastConfig = null;

/* ── Mode bar logic ─────────────────────────────────────────── */
function initModes() {
  // Single-select mode pills (permission + effort)
  ['mg-perm', 'mg-effort'].forEach(gid => {
    const grp = document.getElementById(gid);
    if (!grp) return;
    const key = 'jc_mode_' + gid;
    const saved = ls(key);
    if (saved) {
      grp.querySelectorAll('.mode-pill').forEach(p => p.classList.toggle('active', p.dataset.val === saved));
    }
    grp.querySelectorAll('.mode-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        grp.querySelectorAll('.mode-pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        lsSet(key, pill.dataset.val);
        send({ type: 'set_mode', key: gid === 'mg-perm' ? 'permission' : 'effort', value: pill.dataset.val });
      });
    });
  });

  // Toggle buttons (Post)
  ['post'].forEach(id => {
    const cb  = document.getElementById('mc-' + id);
    const lbl = document.getElementById('mt-' + id);
    if (!cb || !lbl) return;
    cb.checked = ls('jc_mt_' + id) === 'true';
    lbl.classList.toggle('active', cb.checked);
    cb.addEventListener('change', () => {
      lsSet('jc_mt_' + id, cb.checked);
      lbl.classList.toggle('active', cb.checked);
      send({ type: 'set_mode', key: id, value: cb.checked });
    });
  });
}

function getMode()   { return document.querySelector('#mg-perm   .mode-pill.active')?.dataset.val  || 'ask'; }
function getEffort() { return document.querySelector('#mg-effort .mode-pill.active')?.dataset.val  || 'med'; }
function isPostEnabled()  { return document.getElementById('mc-post')?.checked || false; }

/* ── Filter chip logic ──────────────────────────────────────── */
function initChips() {
  document.querySelectorAll('.chips').forEach(group => {
    const single = group.dataset.single === 'true';
    const key    = 'jc_chips_' + group.id;
    // Restore saved state
    const saved = ls(key);
    if (saved) {
      const vals = saved.split(',');
      group.querySelectorAll('.chip').forEach(c => {
        c.classList.toggle('active', vals.includes(c.dataset.val));
      });
    }
    group.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        if (single) {
          group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
          chip.classList.add('active');
        } else {
          chip.classList.toggle('active');
        }
        // Save state
        const active = [...group.querySelectorAll('.chip.active')].map(c => c.dataset.val);
        lsSet(key, active.join(','));
      });
    });
  });

  // Easy Apply toggle persistence
  const ea = document.getElementById('fc-easyapply');
  ea.checked = ls('jc_easy_apply') === 'true';
  ea.addEventListener('change', () => lsSet('jc_easy_apply', ea.checked));
}

function getFilters() {
  const chips = id => [...document.querySelectorAll(`#${id} .chip.active`)].map(c => c.dataset.val);
  const jobTypes = chips('fc-jobtype');
  return {
    sort_by:           chips('fc-sort')[0]    || 'DD',
    date_filter:       chips('fc-date')[0]    || 'week',
    job_types:         jobTypes,              // [] means "any type"
    work_modes:        chips('fc-mode'),
    experience_levels: chips('fc-level'),
    easy_apply_only:   document.getElementById('fc-easyapply').checked,
  };
}

/* ── WebSocket ──────────────────────────────────────────────── */
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen    = ()  => setStatus("Ready", "idle");
  ws.onclose   = ()  => { setStatus("Reconnecting…", "idle"); setTimeout(connect, 2000); };
  ws.onerror   = (e) => console.error("ws", e);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}
function send(obj) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

/* ── Message handler ────────────────────────────────────────── */
function handle(msg) {
  switch (msg.type) {
    case "init":
      agentRunning = msg.is_running;
      updateStats(msg.stats || {});
      (msg.log || []).forEach(addEntry);
      syncUI();
      break;
    case "log":
      if (msg.level === "resume") addResumeEntry(msg);
      else addEntry(msg);
      break;
    case "tailor_progress": addTailorEntry(msg); break;
    case "stats":   updateStats(msg);         break;
    case "status":  setStatus(msg.status, agentRunning ? "running" : "idle"); break;
    case "agent_started":
      agentRunning = true; syncUI(); setStatus("Running", "running"); break;
    case "agent_stopped":
      agentRunning = false; updateStats(msg.stats || {}); syncUI();
      setStatus("Done", "idle"); closeModal(); break;
    case "approval_required": showModal(msg); break;
  }
}

/* ── Resume tailoring feed entries ──────────────────────────── */
function addTailorEntry(msg) {
  const feed  = document.getElementById("feed");
  const job   = msg.job || {};
  const ctx   = job.title ? `${job.title} — ${job.company||""}` : "";
  const steps = msg.steps || [];
  const elId  = "tailor-" + (msg.entry_id || Date.now());

  const stepsHtml = steps.map(s => `
    <div class="tailor-step ${s.done ? 'done' : s.active ? 'active' : ''}">
      <span class="step-dot"></span>
      <span>${x(s.label)}</span>
    </div>`).join("");

  const ats   = msg.ats_score || 0;
  const atsCls = ats >= 75 ? "ats-good" : ats >= 50 ? "ats-ok" : "ats-low";
  const inner = `
    <div class="e-header">
      <span class="e-tag">AI</span>
      ${ctx ? `<span class="e-ctx" style="flex:1">${x(ctx)}</span>` : ""}
      ${ats > 0 ? `<span class="ats-badge ${atsCls}" style="margin-right:4px">ATS ${ats}%</span>` : ""}
      <span class="e-ts">${msg.time||""}</span>
    </div>
    <div class="tailor-steps">${stepsHtml}</div>
    ${msg.resume_preview ? `<pre class="resume-preview">${x(msg.resume_preview)}</pre>` : ""}
  `;

  let el = document.getElementById(elId);
  if (el) {
    el.innerHTML = inner;
  } else {
    el = document.createElement("div");
    el.className = "entry resume";
    el.id = elId;
    el.innerHTML = inner;
    feed.appendChild(el);
  }
  feed.scrollTop = feed.scrollHeight;
  return el;
}

function addResumeEntry(msg) {
  const feed = document.getElementById("feed");
  const job  = msg.job || {};
  const ctx  = job.title ? `${job.title} — ${job.company||""}` : "";
  const el = document.createElement("div");
  el.className = "entry resume";
  el.innerHTML = `
    <div class="e-header">
      <span class="e-tag">Resume</span>
      ${ctx ? `<span class="e-ctx" style="flex:1">${x(ctx)}</span>` : ""}
      <span class="e-ts">${msg.time||""}</span>
    </div>
    <pre class="resume-preview">${x(msg.message||"")}</pre>
  `;
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

/* ── Feed entries ───────────────────────────────────────────── */
function addEntry(e) {
  const feed = document.getElementById("feed");
  const job  = e.job || {};
  const ctx  = job.title
    ? `${job.title}${job.company ? " — " + job.company : ""}`
    : "";

  const el = document.createElement("div");
  el.className = `entry ${e.level || "info"}`;
  el.innerHTML = `
    <span class="e-tag">${x(e.tool || "")}</span>
    <span class="e-body">${ctx ? `<div class="e-ctx">${x(ctx)}</div>` : ""}${x(e.message || "")}</span>
    <span class="e-ts">${e.time || ""}</span>
  `;
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

/* ── Stats ──────────────────────────────────────────────────── */
function updateStats(s) {
  set("stat-applied", s.applied ?? 0);
  set("stat-skipped", s.skipped ?? 0);
  set("stat-emails",  s.emails  ?? 0);
  set("stat-pending", s.pending ?? 0);
  if ((s.pending ?? 0) > 0) setStatus("Awaiting approval", "pending");
}

/* ── UI sync ────────────────────────────────────────────────── */
function syncUI() {
  const input  = document.getElementById("cmd-input");
  const btn    = document.getElementById("btn-run");
  const stop   = document.getElementById("btn-stop");
  const prompt = document.getElementById("cmd-prompt");

  // Input is ALWAYS enabled — user can message agent while it runs
  input.disabled = false;

  if (agentRunning) {
    btn.innerHTML  = "Send ↵";
    btn.disabled   = false;
    prompt.style.color = "var(--green)";
    input.placeholder  = "Message agent — type stop, skip, or anything…";
  } else {
    btn.innerHTML  = "Run ▶";
    btn.disabled   = false;
    prompt.style.color = "";
    input.placeholder  = "apply java full stack jobs posted last 24 hours on linkedin";
  }
  stop.style.display = agentRunning ? "block" : "none";
  input.focus();
}

function setStatus(text, cls) {
  set("status-text", text);
  document.getElementById("status-dot").className = `status-dot ${cls}`;
}

/* ── Command parser ─────────────────────────────────────────── */
function parseCommand(text) {
  const t = text.trim().toLowerCase();

  // ── Date filter — command wins, otherwise use chip ───────────
  let date_filter = getFilters().date_filter;
  if (/24.?h|today|last.?24|past.?day|24 hours?/.test(t))   date_filter = "24h";
  else if (/month|30.?day/.test(t))                          date_filter = "month";
  else if (/any|all.?time/.test(t))                          date_filter = "any";
  else if (/week/.test(t))                                   date_filter = "week";

  // ── Platforms ─────────────────────────────────────────────────
  const hasMention = /linkedin|indeed|dice|gmail|email/.test(t);
  const platforms = {
    linkedin: !hasMention || /linkedin/.test(t),
    indeed:   /indeed/.test(t),
    dice:     /dice/.test(t),
    gmail:    /gmail|check.*email|email.*repl/.test(t),
  };
  // Override sidebar checkboxes if command names platforms
  if (hasMention) {
    document.getElementById("pl-linkedin").checked = platforms.linkedin;
    document.getElementById("pl-indeed").checked   = platforms.indeed;
    document.getElementById("pl-dice").checked     = platforms.dice;
    document.getElementById("pl-gmail").checked    = platforms.gmail;
  } else {
    // Use current sidebar toggles
    platforms.linkedin = document.getElementById("pl-linkedin").checked;
    platforms.indeed   = document.getElementById("pl-indeed").checked;
    platforms.dice     = document.getElementById("pl-dice").checked;
    platforms.gmail    = document.getElementById("pl-gmail").checked;
  }

  // ── Max applications ──────────────────────────────────────────
  let max_applications = parseInt(ls("jc_max_apps") || "30");
  const maxM = t.match(/(\d+)\s+(?:jobs?|applications?|roles?)/);
  if (maxM) max_applications = parseInt(maxM[1]);

  // ── Job query ─────────────────────────────────────────────────
  // "apply <QUERY> jobs" or "search <QUERY>"
  let query = ls("jc_query") || "Java Full Stack Engineer";
  const applyM  = text.match(/apply\s+(?:\d+\s+)?(.+?)\s+(?:jobs?|roles?|positions?|openings?)/i);
  const searchM = text.match(/search\s+(?:for\s+)?(.+?)(?:\s+(?:jobs?|roles?|in |on |posted|\s*$))/i);
  if (applyM)       query = applyM[1].trim();
  else if (searchM) query = searchM[1].trim();

  // ── Location ──────────────────────────────────────────────────
  let location = ls("jc_location") || "United States";
  const locM = text.match(
    /\bin\s+([\w\s]+?)(?=\s+(?:posted|this|last|today|24|week|month|on |for |$))/i
  );
  if (locM) {
    const candidate = locM[1].trim();
    // Only override if it looks like a real location (not "linkedin", "last", etc.)
    if (!/linkedin|indeed|dice|last|this|any/i.test(candidate)) {
      location = candidate;
    }
  }

  const f = getFilters();
  return {
    query, location, date_filter, max_applications, platforms,
    job_types:         f.job_types,
    work_modes:        f.work_modes,
    experience_levels: f.experience_levels,
    easy_apply_only:   f.easy_apply_only,
  };
}

/* ── LinkedIn post command ───────────────────────────────────── */
function isPostCommand(text) {
  return /post.*(linkedin|update|status)|linkedin.*post/i.test(text);
}

/* ── Run command ────────────────────────────────────────────── */
function runCommand() {
  const raw = document.getElementById("cmd-input").value.trim();
  if (!raw) return;

  document.getElementById("cmd-input").value = "";
  const tl = raw.toLowerCase().trim();

  // ── Built-in commands (never start agent) ───────────────────
  if (/^(clear|cls)(\s+.*)?$/.test(tl)) {
    document.getElementById("feed").innerHTML = "";
    return;
  }

  // Save to history
  if (cmdHistory[0] !== raw) cmdHistory.unshift(raw);
  if (cmdHistory.length > 50) cmdHistory.pop();
  histIdx = -1;

  // Show user message in feed
  addUserEntry(raw);

  // ── If agent is running → send as chat/control message ──────
  if (agentRunning) {
    if (/\bstop\b|\bhalt\b|\bquit\b|\bcancel\b/.test(tl)) {
      send({ type: "stop_agent" });
    } else if (/\bskip\b/.test(tl)) {
      send({ type: "approval", approved: false });
    } else if (/\bapprove\b|\byes\b|\bsubmit\b/.test(tl)) {
      send({ type: "approval", approved: true });
    } else if (/switch.*(auto|automatic)/.test(tl)) {
      setPermMode("auto");
      send({ type: "set_mode", key: "permission", value: "auto" });
    } else if (/switch.*(ask|manual)/.test(tl)) {
      setPermMode("ask");
      send({ type: "set_mode", key: "permission", value: "ask" });
    } else if (/switch.*(plan)/.test(tl)) {
      setPermMode("plan");
      send({ type: "set_mode", key: "permission", value: "plan" });
    } else {
      send({ type: "chat", message: raw });
    }
    return;
  }

  // ── Agent not running → start it ───────────────────────────
  const creds = loadCreds();
  if (!creds.linkedin_email || !creds.linkedin_password) {
    showSettings();
    addSysEntry("Save your LinkedIn credentials in Settings first.", "warn");
    return;
  }

  const parsed = parseCommand(raw);

  // ── Validate: at least one platform selected ────────────────
  const activePlatforms = Object.entries(parsed.platforms)
    .filter(([, v]) => v).map(([k]) => k);
  if (!activePlatforms.length) {
    addSysEntry("No platform selected. Check LinkedIn / Indeed / Dice / Gmail in the sidebar.", "warn");
    return;
  }

  // ── Show run summary so user knows what's about to happen ───
  const modeLabel  = { ask: "Ask before each", auto: "Auto-apply", plan: "Plan only" }[getMode()] || getMode();
  const effortLabel= { low: "Low (≥4)", med: "Med (≥6)", high: "High (≥8)" }[getEffort()] || getEffort();
  addSysEntry(
    `Platforms: ${activePlatforms.join(", ")}  |  ${parsed.date_filter}  |  ${modeLabel}  |  Effort: ${effortLabel}`,
    "info"
  );

  const config = {
    ...creds, ...parsed,
    permission:   getMode(),
    effort:       getEffort(),
    continuous:   false,
    post_enabled: isPostEnabled(),
  };
  _lastConfig = config;
  send({ type: "start_agent", config });
}

function setPermMode(val) {
  const grp = document.getElementById('mg-perm');
  if (!grp) return;
  grp.querySelectorAll('.mode-pill').forEach(p => p.classList.toggle('active', p.dataset.val === val));
  lsSet('jc_mode_mg-perm', val);
}

function addSysEntry(msg, level = "info") {
  addEntry({ tool: "System", time: new Date().toTimeString().slice(0,8), message: msg, level, job: {} });
}

function addUserEntry(text) {
  addEntry({ tool: "You", time: new Date().toTimeString().slice(0,8), message: text, level: "user", job: {} });
}

/* ── localStorage helpers ───────────────────────────────────── */
const ls      = key => localStorage.getItem(key) || "";
const lsSet   = (key, val) => localStorage.setItem(key, val);

function loadCreds() {
  return {
    linkedin_email:    ls("jc_email"),
    linkedin_password: ls("jc_password"),
    dice_email:        ls("jc_dice_email"),
    dice_password:     ls("jc_dice_password"),
    openai_api_key:    ls("jc_openai_key"),
  };
}

/* ── Settings panel ─────────────────────────────────────────── */
function showSettings() {
  document.getElementById("s-li-email").value    = ls("jc_email");
  document.getElementById("s-li-password").value = ls("jc_password");
  document.getElementById("s-dice-email").value    = ls("jc_dice_email");
  document.getElementById("s-dice-password").value = ls("jc_dice_password");
  document.getElementById("s-openai-key").value  = ls("jc_openai_key");
  document.getElementById("s-query").value       = ls("jc_query")    || "Java Full Stack Engineer";
  document.getElementById("s-location").value    = ls("jc_location") || "United States";
  document.getElementById("s-max-apps").value    = ls("jc_max_apps") || "30";
  document.getElementById("settings-overlay").style.display = "block";
}

function saveSettings() {
  lsSet("jc_email",         document.getElementById("s-li-email").value.trim());
  lsSet("jc_password",      document.getElementById("s-li-password").value);
  lsSet("jc_dice_email",    document.getElementById("s-dice-email").value.trim());
  lsSet("jc_dice_password", document.getElementById("s-dice-password").value);
  lsSet("jc_openai_key",    document.getElementById("s-openai-key").value.trim());
  lsSet("jc_query",         document.getElementById("s-query").value.trim());
  lsSet("jc_location",      document.getElementById("s-location").value.trim());
  lsSet("jc_max_apps",      document.getElementById("s-max-apps").value);
  document.getElementById("settings-overlay").style.display = "none";
  addSysEntry("Settings saved.", "success");
}

function hideSettings() {
  document.getElementById("settings-overlay").style.display = "none";
}

/* ── Approval modal ─────────────────────────────────────────── */
function showModal(msg) {
  const j = msg.job || {};
  set("modal-title",    `${j.title || "Job"} @ ${j.company || ""}`);
  set("modal-subtitle", j.location || "");
  set("modal-score",    j.score ? `${j.score}/10` : "");
  set("modal-tag",      msg.modal_type === "email" ? "Reply" : "Apply");

  // ATS score badge
  const atsScore = msg.ats_score || 0;
  const atsBadge = document.getElementById("modal-ats-badge");
  if (atsScore > 0) {
    atsBadge.textContent = `ATS ${atsScore}%`;
    atsBadge.className = `ats-badge ${atsScore >= 75 ? "ats-good" : atsScore >= 50 ? "ats-ok" : "ats-low"}`;
    atsBadge.style.display = "";
  } else {
    atsBadge.style.display = "none";
  }

  document.getElementById("modal-jd").textContent         = j.jd    || "(No description)";
  document.getElementById("modal-resume").textContent     = msg.resume || "";
  document.getElementById("modal-resume-edit").value      = msg.resume || "";
  document.getElementById("modal-coverletter").textContent = msg.cover_letter || "";
  // Reset to resume tab and edit mode
  switchModalTab("resume");
  _setResumeEditMode(false);
  document.getElementById("approval-modal").style.display = "flex";

  secsLeft = 90; set("countdown", secsLeft); clearInterval(timer);
  timer = setInterval(() => {
    set("countdown", --secsLeft);
    if (secsLeft <= 0) { clearInterval(timer); approve(false); }
  }, 1000);
}

function switchModalTab(tab) {
  document.getElementById("tab-resume").classList.toggle("active", tab === "resume");
  document.getElementById("tab-coverletter").classList.toggle("active", tab === "coverletter");
  document.getElementById("modal-tab-resume").style.display      = tab === "resume"      ? "" : "none";
  document.getElementById("modal-tab-coverletter").style.display = tab === "coverletter" ? "" : "none";
  // Hide edit button on cover letter tab
  document.getElementById("btn-edit-resume").style.display = tab === "resume" ? "" : "none";
}

function _setResumeEditMode(editing) {
  const pre  = document.getElementById("modal-resume");
  const ta   = document.getElementById("modal-resume-edit");
  const btn  = document.getElementById("btn-edit-resume");
  if (editing) {
    ta.value = pre.textContent;
    pre.style.display = "none";
    ta.style.display  = "flex";
    btn.textContent = "✓ Done";
    btn.classList.add("active");
  } else {
    pre.textContent  = ta.value;
    pre.style.display = "";
    ta.style.display  = "none";
    btn.textContent = "✎ Edit";
    btn.classList.remove("active");
  }
}

function closeModal() {
  document.getElementById("approval-modal").style.display = "none";
  _setResumeEditMode(false);
  clearInterval(timer);
}
function approve(ok) {
  // Send edited resume text if user made changes
  const ta = document.getElementById("modal-resume-edit");
  const pre = document.getElementById("modal-resume");
  const editedResume = ta.style.display !== "none" ? ta.value : pre.textContent;
  send({ type: "approval", approved: ok, resume: editedResume });
  closeModal();
}

/* ── Navigation ─────────────────────────────────────────────── */
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.getElementById(`view-${name}`).classList.add("active");
  if (name === "tracker") loadTracker();
  if (name === "resumes") loadResumes();
}

/* ── Resumes ────────────────────────────────────────────────── */
async function loadResumes() {
  const list   = document.getElementById("resume-list");
  const viewer = document.getElementById("resume-viewer");
  list.innerHTML = `<div style="padding:16px;color:var(--dim);font-size:12px">Loading…</div>`;
  viewer.innerHTML = `<div class="resume-viewer-empty">Select a resume to preview</div>`;

  try {
    const items = await fetch("/api/resumes").then(r => r.json());
    if (!items.length) {
      list.innerHTML = `<div style="padding:16px;color:var(--dim);font-size:12px">No resumes yet.<br>Run the agent to generate tailored resumes.</div>`;
      return;
    }
    list.innerHTML = items.map(r => `
      <div class="resume-item" data-stem="${x(r.stem)}" onclick="viewResume('${x(r.stem)}',this)">
        <div class="resume-item-title">${x(r.label)}</div>
        <div class="resume-item-meta">
          <span>${x(r.date.replace('-',' · '))}</span>
          ${r.has_docx ? `<a href="/api/resumes/download/${x(r.stem)}.docx" target="_blank" onclick="event.stopPropagation()">DOCX</a>` : ""}
          ${r.has_pdf  ? `<a href="/api/resumes/download/${x(r.stem)}.pdf"  target="_blank" onclick="event.stopPropagation()">PDF</a>`  : ""}
          <button class="resume-item-delete" title="Delete" onclick="event.stopPropagation();deleteResume('${x(r.stem)}',this)">✕</button>
        </div>
      </div>
    `).join("");

    // Auto-open the most recent one
    const first = list.querySelector(".resume-item");
    if (first) first.click();
  } catch {
    list.innerHTML = `<div style="padding:16px;color:var(--dim)">Could not load resumes.</div>`;
  }
}

/* Convert **bold** and # headings to HTML — no raw stars shown */
function renderResume(text) {
  return text.split('\n').map(line => {
    // Section headings: lines that are ALL CAPS or start with #
    if (/^#{1,3}\s/.test(line)) {
      return `<div class="rv-heading">${x(line.replace(/^#+\s*/, ''))}</div>`;
    }
    if (/^[A-Z][A-Z\s&\/\-]{3,}$/.test(line.trim()) && line.trim().length > 3) {
      return `<div class="rv-heading">${x(line)}</div>`;
    }
    // Bold: **text**
    let html = x(line).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Bullet points
    if (/^\s*[-•]\s/.test(line)) {
      html = `<div class="rv-bullet">${html.replace(/^\s*[-•]\s/, '')}</div>`;
      return html;
    }
    return line.trim() === '' ? '<div class="rv-spacer"></div>' : `<div class="rv-line">${html}</div>`;
  }).join('');
}

async function deleteResume(stem, btnEl) {
  if (!confirm(`Delete resume "${stem.replace(/_/g, ' ')}"?`)) return;
  try {
    const r = await fetch(`/api/resumes/${encodeURIComponent(stem)}`, { method: "DELETE" });
    const data = await r.json();
    if (data.ok) {
      // Remove the list item
      btnEl.closest(".resume-item").remove();
      // If this was the active resume, clear the viewer
      const viewer = document.getElementById("resume-viewer");
      if (viewer.dataset.stem === stem) {
        viewer.innerHTML = `<div class="resume-viewer-empty">Select a resume to preview</div>`;
        delete viewer.dataset.stem;
        delete viewer.dataset.rawText;
      }
      // If list is now empty, show empty state
      const list = document.getElementById("resume-list");
      if (!list.querySelector(".resume-item")) {
        list.innerHTML = `<div style="padding:16px;color:var(--dim);font-size:12px">No resumes yet.<br>Run the agent to generate tailored resumes.</div>`;
      }
    }
  } catch {
    alert("Delete failed.");
  }
}

async function viewResume(stem, el) {
  document.querySelectorAll(".resume-item").forEach(i => i.classList.remove("active"));
  el.classList.add("active");

  const viewer = document.getElementById("resume-viewer");
  viewer.innerHTML = `<div class="resume-viewer-empty">Loading…</div>`;

  try {
    const data = await fetch(`/api/resumes/text/${encodeURIComponent(stem)}`).then(r => r.json());
    if (data.error) { viewer.innerHTML = `<div class="resume-viewer-empty">${x(data.error)}</div>`; return; }

    const item  = document.querySelector(`.resume-item[data-stem="${stem}"]`);
    const hasDocx = item?.querySelector('a[href$=".docx"]');
    const hasPdf  = item?.querySelector('a[href$=".pdf"]');
    const skills  = data.skills || {};
    const strong  = skills.strong || [];
    const missing = skills.missing || [];
    const tip     = skills.tip || "";

    // Skills panel HTML
    const skillsHtml = (strong.length || missing.length) ? `
      <div class="rv-skills-panel">
        ${strong.length ? `
          <div class="rv-skills-row">
            <span class="rv-skills-label match">✓ Strong match</span>
            <div class="rv-skills-tags">
              ${strong.map(s => `<span class="rv-tag match">${x(s)}</span>`).join('')}
            </div>
          </div>` : ''}
        ${missing.length ? `
          <div class="rv-skills-row">
            <span class="rv-skills-label gap">✗ Missing</span>
            <div class="rv-skills-tags">
              ${missing.map(s => `<span class="rv-tag gap">${x(s)}</span>`).join('')}
            </div>
          </div>` : ''}
        ${tip ? `<div class="rv-tip">💡 ${x(tip)}</div>` : ''}
      </div>` : '';

    viewer.innerHTML = `
      <div class="resume-viewer-hd">
        <span class="resume-viewer-title">${x(stem.replace(/_/g,' '))}</span>
        <div class="resume-viewer-actions">
          <button class="btn-ghost sm" id="rv-btn-copy" onclick="copyResume('${x(stem)}')">Copy</button>
          <button class="btn-ghost sm" id="rv-btn-edit" onclick="editResume('${x(stem)}')">Edit</button>
          ${hasDocx ? `<a href="/api/resumes/download/${x(stem)}.docx" target="_blank" class="btn-ghost sm">↓ DOCX</a>` : ''}
          ${hasPdf  ? `<a href="/api/resumes/download/${x(stem)}.pdf"  target="_blank" class="btn-ghost sm">↓ PDF</a>`  : ''}
        </div>
      </div>
      ${skillsHtml}
      <div class="rv-body" id="rv-body-${x(stem)}">${renderResume(data.text)}</div>
    `;

    // Store raw text for copy/edit
    viewer.dataset.rawText = data.text;
    viewer.dataset.stem    = stem;
  } catch {
    viewer.innerHTML = `<div class="resume-viewer-empty">Could not load resume text.</div>`;
  }
}

function copyResume(stem) {
  const viewer = document.getElementById("resume-viewer");
  const text = viewer.dataset.rawText || "";
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById("rv-btn-copy");
    if (btn) { btn.textContent = "Copied!"; setTimeout(() => btn.textContent = "Copy", 1800); }
  });
}

function editResume(stem) {
  const viewer = document.getElementById("resume-viewer");
  const rawText = viewer.dataset.rawText || "";
  const body = document.getElementById(`rv-body-${stem}`);
  if (!body) return;

  // Toggle: if already editing, do nothing (save button handles it)
  if (body.querySelector("textarea")) return;

  const editBtn = document.getElementById("rv-btn-edit");
  // Use DOM to set textarea value — avoids HTML entity issues
  const wrapper = document.createElement('div');
  wrapper.innerHTML = `
    <textarea class="rv-textarea" id="rv-edit-ta"></textarea>
    <div class="rv-edit-actions">
      <button class="btn-run sm" onclick="saveResume('${x(stem)}')">Save</button>
      <button class="btn-ghost sm" onclick="viewResume('${x(stem)}', document.querySelector('.resume-item.active'))">Cancel</button>
    </div>`;
  body.innerHTML = '';
  body.appendChild(wrapper);
  document.getElementById("rv-edit-ta").value = rawText;
  if (editBtn) editBtn.style.display = "none";
  document.getElementById("rv-edit-ta").focus();
}


async function saveResume(stem) {
  const ta = document.getElementById("rv-edit-ta");
  if (!ta) return;
  const text = ta.value;
  try {
    const r = await fetch(`/api/resumes/text/${encodeURIComponent(stem)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    const data = await r.json();
    if (data.ok) {
      // Re-render with updated text
      const activeItem = document.querySelector(".resume-item.active");
      if (activeItem) viewResume(stem, activeItem);
    }
  } catch {
    alert("Save failed — server error");
  }
}

/* ── Tracker ────────────────────────────────────────────────── */
async function loadTracker() {
  const wrap = document.getElementById("tracker-table");
  try {
    const apps = await fetch("/api/applications").then(r => r.json());
    if (!apps.length) {
      wrap.innerHTML = "<p style='padding:20px;color:var(--dim)'>No applications yet.</p>";
      return;
    }
    const rows = apps.slice().reverse().map(a => `
      <tr>
        <td>${a.id}</td>
        <td>${x(a.date?.slice(0,10)||"")}</td>
        <td>${x(a.company)}</td>
        <td>${x(a.role)}</td>
        <td>${x(a.source||"")}</td>
        <td><span class="badge ${bc(a.status)}">${x(a.status)}</span></td>
      </tr>`).join("");
    wrap.innerHTML = `<table>
      <thead><tr><th>#</th><th>Date</th><th>Company</th><th>Role</th><th>Source</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch {
    wrap.innerHTML = "<p style='padding:20px;color:var(--dim)'>Could not load.</p>";
  }
}

/* ── Helpers ────────────────────────────────────────────────── */
const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
const x   = s => String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const bc  = s => ({
  Applied:"badge-applied","Phone Screen":"badge-screen","Technical Round":"badge-screen",
  "Final Round":"badge-screen",Offer:"badge-offer",Rejected:"badge-rejected",Ghosted:"badge-ghosted",
}[s]||"badge-applied");

/* ── Event listeners ────────────────────────────────────────── */
document.getElementById("btn-run")    .addEventListener("click", runCommand);
document.getElementById("btn-approve").addEventListener("click", () => approve(true));
document.getElementById("btn-reject") .addEventListener("click", () => approve(false));
document.getElementById("btn-stop")   .addEventListener("click", () => send({ type: "stop_agent" }));

document.getElementById("btn-copy-resume").addEventListener("click", () => {
  const ta  = document.getElementById("modal-resume-edit");
  const pre = document.getElementById("modal-resume");
  const text = ta.style.display !== "none" ? ta.value : pre.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById("btn-copy-resume");
    btn.textContent = "✓ Copied";
    setTimeout(() => { btn.textContent = "⎘ Copy"; }, 1500);
  });
});

document.getElementById("btn-edit-resume").addEventListener("click", () => {
  const ta  = document.getElementById("modal-resume-edit");
  const editing = ta.style.display !== "none";
  _setResumeEditMode(!editing);
});
document.getElementById("btn-clear-feed").addEventListener("click", () => {
  document.getElementById("feed").innerHTML = "";
});
document.getElementById("btn-tracker")      .addEventListener("click", () => showView("tracker"));
document.getElementById("btn-back")         .addEventListener("click", () => showView("feed"));
document.getElementById("btn-resumes")      .addEventListener("click", () => showView("resumes"));
document.getElementById("btn-back-resumes") .addEventListener("click", () => showView("feed"));
document.getElementById("btn-settings").addEventListener("click", showSettings);
document.getElementById("settings-close").addEventListener("click", hideSettings);
document.getElementById("settings-save") .addEventListener("click", saveSettings);
document.getElementById("settings-overlay").addEventListener("click", e => {
  if (e.target === e.currentTarget) hideSettings();
});

// Suggestion pills
document.querySelectorAll(".sug-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    document.getElementById("cmd-input").value = pill.dataset.cmd;
    document.getElementById("cmd-input").focus();
  });
});

// Enter key to run, arrow keys for history
document.getElementById("cmd-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    runCommand();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    if (histIdx < cmdHistory.length - 1) {
      histIdx++;
      document.getElementById("cmd-input").value = cmdHistory[histIdx];
    }
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    if (histIdx > 0) {
      histIdx--;
      document.getElementById("cmd-input").value = cmdHistory[histIdx];
    } else {
      histIdx = -1;
      document.getElementById("cmd-input").value = "";
    }
  }
});

/* ── Resume upload ──────────────────────────────────────────── */
function initResumeUpload() {
  const drop  = document.getElementById("resume-drop");
  const input = document.getElementById("resume-file");
  const label = document.getElementById("resume-label");
  if (!drop) return;

  drop.addEventListener("click", () => input.click());

  drop.addEventListener("dragover",  e => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", e => {
    e.preventDefault();
    drop.classList.remove("over");
    const file = e.dataTransfer.files[0];
    if (file) uploadResume(file);
  });

  input.addEventListener("change", () => {
    if (input.files[0]) uploadResume(input.files[0]);
  });

  function uploadResume(file) {
    label.textContent = `Uploading ${file.name}…`;
    const form = new FormData();
    form.append("file", file);
    fetch("/api/upload-resume", { method: "POST", body: form })
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          drop.classList.add("done");
          label.textContent = `✓ ${file.name} uploaded`;
          addSysEntry(`Resume uploaded: ${file.name}`, "success");
        } else {
          label.textContent = `Upload failed: ${d.error}`;
        }
      })
      .catch(() => { label.textContent = "Upload failed — check server"; });
  }
}

/* ── Init ───────────────────────────────────────────────────── */
connect();
initChips();
initModes();
initResumeUpload();

// Show settings on first run if no creds saved
if (!ls("jc_email")) {
  setTimeout(() => {
    showSettings();
    addSysEntry("Welcome to xHR! Save your LinkedIn credentials in Settings to get started.", "info");
  }, 800);
} else {
  // Show a welcome hint
  setTimeout(() => {
    addSysEntry(
      `Ready — type a command or click a suggestion above.\n` +
      `Example: apply java full stack jobs posted last 24 hours`,
      "info"
    );
  }, 300);
}
