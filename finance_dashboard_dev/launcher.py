#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


REPO_URL = os.environ.get("FINANCE_DEV_REPO_URL", "https://github.com/jovanni-hernandez/homeassistant-finance-dashboard.git")
BRANCH = os.environ.get("FINANCE_DEV_BRANCH", "main")
WORKDIR = Path(os.environ.get("FINANCE_DEV_WORKDIR", "/data/finance-dashboard-workspace"))
JOBS_DIR = Path(os.environ.get("FINANCE_DEV_JOBS", "/data/finance-dashboard-jobs"))
PORT = int(os.environ.get("PORT", "8000"))

COMMANDS = {
    "backend-syntax": {"command": ["python3", "-m", "py_compile"], "cwd": "app"},
    "selenium": {
        "command": ["bash", "hacks/validate-ui-selenium.sh"],
        "cwd": "app",
        "env": {"FINANCE_DASHBOARD_URL": "local"},
    },
    "status": {"command": ["git", "status", "--short"], "cwd": "repo"},
    "pull": {"command": ["git", "pull", "--ff-only", "origin", BRANCH], "cwd": "repo"},
}

ARTIFACTS = {
    "selenium": [
        f"/data/v2/cache/opencode/finance-dashboard-real-selenium-{device}{suffix}.png"
        for device in ("desktop", "iphone", "pixel")
        for suffix in ("", "-recurring", "-settings")
    ],
}

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def ensure_repo() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if not (WORKDIR / ".git").exists():
        subprocess.run(["git", "clone", "--branch", BRANCH, "--single-branch", REPO_URL, str(WORKDIR)], check=True)
    else:
        subprocess.run(["git", "-C", str(WORKDIR), "pull", "--ff-only", "origin", BRANCH], check=True)


def app_directory() -> Path:
    nested = WORKDIR / "finance_dashboard"
    return nested if (nested / "backend").is_dir() else WORKDIR


def write_job(job_id: str, **updates) -> dict:
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {"id": job_id})
        job.update(updates)
        (JOBS_DIR / f"{job_id}.json").write_text(json.dumps(job, indent=2, sort_keys=True))
        return job


def spawn_job(label: str) -> dict:
    spec = COMMANDS[label]
    app_dir = app_directory()
    cwd = app_dir if spec["cwd"] == "app" else WORKDIR
    command = list(spec["command"])
    if label == "backend-syntax":
        python_files = sorted(str(path.relative_to(cwd)) for path in (cwd / "backend").glob("*.py"))
        if not python_files:
            raise FileNotFoundError(f"No backend Python files found under {cwd / 'backend'}")
        command.extend(python_files)
    env = os.environ.copy()
    env.update(spec.get("env", {}))

    job_id = uuid.uuid4().hex[:12]
    log_path = JOBS_DIR / f"{job_id}.log"
    write_job(
        job_id,
        label=label,
        command=" ".join(command),
        status="queued",
        log=str(log_path),
        artifacts=ARTIFACTS.get(label, []),
        created_at=time.time(),
    )

    def runner():
        write_job(job_id, status="running", started_at=time.time())
        with log_path.open("w") as log:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    shell=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                rc = proc.wait()
                write_job(job_id, status="completed" if rc == 0 else "failed", exit_code=rc, finished_at=time.time())
            except Exception as exc:
                log.write(f"\n[launcher] {exc}\n")
                write_job(job_id, status="failed", error=str(exc), finished_at=time.time())

    threading.Thread(target=runner, daemon=True).start()
    return JOBS[job_id]


def list_jobs() -> list[dict]:
    with JOBS_LOCK:
        items = list(JOBS.values())
    return sorted(items, key=lambda item: item.get("created_at", 0), reverse=True)


def read_log(job_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise FileNotFoundError(job_id)
    path = JOBS_DIR / f"{job_id}.log"
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-20000:]


def read_artifact(job_id: str, name: str) -> tuple[bytes, str]:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise FileNotFoundError(job_id)
    job = JOBS.get(job_id, {})
    for artifact in job.get("artifacts", []):
        path = Path(artifact)
        if path.name == name and path.exists():
            mime, _ = mimetypes.guess_type(path.name)
            return path.read_bytes(), mime or "application/octet-stream"
    raise FileNotFoundError(name)


def html_page() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Finance Dev Launcher</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .card {{ background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 16px; margin-bottom: 16px; }}
    .row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    button, input, textarea {{ font: inherit; }}
    button {{ background: #2563eb; color: white; border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; }}
    button.secondary {{ background: #334155; }}
    button.warn {{ background: #be123c; }}
    input, textarea {{ width: 100%; border-radius: 10px; border: 1px solid #475569; background: #0b1220; color: #e2e8f0; padding: 10px 12px; box-sizing: border-box; }}
    textarea {{ min-height: 100px; resize: vertical; }}
    code, pre {{ white-space: pre-wrap; word-break: break-word; }}
    .jobs {{ display: grid; gap: 12px; }}
    .job {{ border: 1px solid #334155; border-radius: 12px; padding: 12px; background: #0b1220; }}
    .muted {{ color: #94a3b8; }}
    .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; }}
    .running {{ background: #1d4ed8; }}
    .completed {{ background: #047857; }}
    .failed {{ background: #991b1b; }}
    .queued {{ background: #475569; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .log {{ max-height: 280px; overflow: auto; background: #020617; padding: 12px; border-radius: 10px; border: 1px solid #334155; }}
  </style>
</head>
<body>
  <main>
    <h1>Finance Dev Launcher</h1>
    <p class="muted">Repo: {html.escape(str(WORKDIR))}</p>

    <div class="card">
      <h2>Quick actions</h2>
      <div class="row">
        <button onclick="runPreset('status')">Git status</button>
        <button onclick="runPreset('pull')" class="secondary">Pull latest</button>
        <button onclick="runPreset('backend-syntax')">Backend syntax</button>
        <button onclick="runPreset('selenium')">Selenium check</button>
      </div>
      <p class="muted" style="margin-top:12px;">Only fixed repository checks can be started from this page; arbitrary shell execution is disabled.</p>
    </div>

    <div class="card">
      <h2>Test output</h2>
      <div class="row" style="margin-top:12px;">
        <button onclick="refreshAll()" class="secondary">Refresh jobs</button>
      </div>
    </div>

    <div class="card">
      <h2>Status</h2>
      <pre id="status" class="muted">Loading…</pre>
    </div>

    <div class="card">
      <h2>Jobs</h2>
      <div id="jobs" class="jobs"></div>
    </div>
  </main>

  <script>
    window.__jobs = [];

    async function api(path, options) {{
      const res = await fetch(path, Object.assign({{ headers: {{ 'Content-Type': 'application/json' }} }}, options || {{}}));
      const text = await res.text();
      let data = null;
      try {{ data = JSON.parse(text); }} catch {{ data = text; }}
      if (!res.ok) throw new Error(typeof data === 'string' ? data : (data.error || JSON.stringify(data)));
      return data;
    }}

    function fmtTime(ts) {{
      if (!ts) return '—';
      return new Date(ts * 1000).toLocaleString();
    }}

    async function refreshStatus() {{
      const data = await api('/api/meta');
      document.getElementById('status').textContent = JSON.stringify(data, null, 2);
    }}

    async function refreshJobs() {{
      const jobs = await api('/api/jobs');
      window.__jobs = jobs;
      const root = document.getElementById('jobs');
      root.innerHTML = '';
      jobs.forEach((job) => {{
        const el = document.createElement('div');
        el.className = 'job';
        el.innerHTML = `
          <div class="row" style="justify-content:space-between;">
            <strong>${{job.label || job.id}}</strong>
            <span class="pill ${{job.status}}">${{job.status}}</span>
          </div>
          <div class="muted">${{job.command || ''}}</div>
          <div class="muted">Started: ${{fmtTime(job.started_at)}} · Finished: ${{fmtTime(job.finished_at)}} · Exit: ${{job.exit_code ?? '—'}}</div>
          <div class="row" style="margin-top:8px;">
            <button class="secondary" onclick="loadLog('${{job.id}}')">Show log</button>
            <button class="secondary" onclick="toggleArtifacts('${{job.id}}')">Screenshots</button>
          </div>
          <pre id="log-${{job.id}}" class="log" style="display:none;"></pre>
          <div id="artifacts-${{job.id}}" class="log" style="display:none; margin-top:8px;"></div>
        `;
        root.appendChild(el);
      }});
    }}

    async function loadLog(jobId) {{
      const pre = document.getElementById(`log-${{jobId}}`);
      const shown = pre.style.display !== 'none';
      if (shown) {{ pre.style.display = 'none'; return; }}
      pre.textContent = 'Loading…';
      pre.style.display = 'block';
      pre.textContent = await api(`/api/jobs/${{jobId}}/log`);
    }}

    async function toggleArtifacts(jobId) {{
      const panel = document.getElementById(`artifacts-${{jobId}}`);
      const shown = panel.style.display !== 'none';
      if (shown) {{
        panel.style.display = 'none';
        return;
      }}
      panel.style.display = 'block';
      if (panel.dataset.loaded) return;
      panel.dataset.loaded = '1';

      const job = window.__jobs.find((item) => item.id === jobId);
      const artifacts = job?.artifacts || [];
      if (!artifacts.length) {{
        panel.innerHTML = '<div class="muted">No screenshots recorded for this job.</div>';
        return;
      }}

      panel.innerHTML = artifacts.map((artifact) => `
        <div style="margin-bottom:12px;">
          <div class="muted" style="margin-bottom:6px;">${{artifact.split('/').pop()}}</div>
          <img src="/api/jobs/${{jobId}}/artifact/${{encodeURIComponent(artifact.split('/').pop())}}" alt="${{artifact}}" style="width:100%; border-radius:12px; border:1px solid #334155; background:#020617;" />
        </div>
      `).join('');
    }}

    async function runPreset(name) {{
      await api('/api/run', {{ method: 'POST', body: JSON.stringify({{ preset: name }}) }});
      await refreshAll();
    }}

    async function refreshAll() {{
      await Promise.all([refreshStatus(), refreshJobs()]);
    }}

    refreshAll();
    setInterval(refreshAll, 3000);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._send(200, html_page())
        if parsed.path == "/api/meta":
            payload = {
                "repo_url": REPO_URL,
                "branch": BRANCH,
                "workdir": str(WORKDIR),
                "job_count": len(list_jobs()),
            }
            return self._send(200, json.dumps(payload, indent=2), "application/json; charset=utf-8")
        if parsed.path == "/api/jobs":
            return self._send(200, json.dumps(list_jobs(), indent=2), "application/json; charset=utf-8")
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/log"):
            job_id = parsed.path.split("/")[3]
            try:
                log = read_log(job_id)
            except FileNotFoundError:
                return self._send(404, "Not found", "text/plain; charset=utf-8")
            return self._send(200, log, "text/plain; charset=utf-8")
        if parsed.path.startswith("/api/jobs/") and "/artifact/" in parsed.path:
            parts = parsed.path.split("/")
            job_id = parts[3]
            name = parts[-1]
            try:
                data, mime = read_artifact(job_id, name)
            except FileNotFoundError:
                return self._send(404, "Not found", "text/plain; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if parsed.path == "/api/run":
            preset = str(payload.get("preset") or "").strip()
            if "command" in payload:
                return self._send(400, json.dumps({"error": "Arbitrary shell commands are disabled; choose a preset."}), "application/json; charset=utf-8")
            if preset not in COMMANDS:
                return self._send(400, json.dumps({"error": "Unknown or missing preset"}), "application/json; charset=utf-8")
            try:
                job = spawn_job(preset)
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(job, indent=2), "application/json; charset=utf-8")
        return self._send(404, "Not found", "text/plain; charset=utf-8")


def main():
    ensure_repo()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Finance Dev Launcher listening on {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
