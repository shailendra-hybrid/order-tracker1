#!/usr/bin/env python3
"""
Order Tracker - deploy webhook.

Runs directly on the VM (not inside Docker) so it can freely call `git` and
`docker compose` with normal filesystem/socket access - no docker-in-docker
mounting tricks needed.

  POST /deploy         start a deploy (git pull + docker compose up -d --build)
                        Header: Authorization: Bearer <DEPLOY_TOKEN>
                        202 {"status":"started", ...} or 409 if one is already running
  GET  /deploy/status   current/last deploy state, exit codes, log tail, git commit
  GET  /healthz         liveness check (no auth)

Only two fixed command lines ever run (git ..., docker compose ...); nothing
from the request body or headers is interpolated into a shell command.

This is the thing an n8n "MCP Server Trigger" tool calls to redeploy the
container - see ../n8n-mcp-deploy.workflow.ts.
"""
import http.server
import json
import os
import re
import subprocess
import threading
import time
import hmac
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("REPO_DIR") or HERE.parent).resolve()


def _load_env():
    for p in (REPO_DIR / ".env", HERE / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

TOKEN = os.environ.get("DEPLOY_TOKEN", "")
BRANCH = os.environ.get("DEPLOY_BRANCH", "main")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", str(REPO_DIR / "docker-compose.yml"))
SERVICE = os.environ.get("COMPOSE_SERVICE", "app")
PORT = int(os.environ.get("DEPLOY_PORT", "8091"))
HOST = os.environ.get("DEPLOY_HOST", "127.0.0.1")
LOG_PATH = Path(os.environ.get("DEPLOY_LOG") or (REPO_DIR / "data" / "deploy.log"))
MAX_LOG = 200_000  # bytes kept on disk
TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT", "600"))

lock = threading.Lock()
state = {
    "state": "idle",       # idle | running | done | failed
    "started": None,
    "finished": None,
    "exit_code": None,
    "step": None,
    "log": "",
    "commit": None,
}


def sh(cmd, cwd=None, timeout=TIMEOUT):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = "$ " + " ".join(cmd) + "\n" + (r.stdout or "") + (r.stderr or "") + "\n"
    return r.returncode, out


def git_commit():
    try:
        r = subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


def append_log(text):
    state["log"] = (state["log"] + text)[-MAX_LOG:]
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(text)
        if LOG_PATH.stat().st_size > MAX_LOG * 2:
            data = LOG_PATH.read_text()[-MAX_LOG:]
            LOG_PATH.write_text(data)
    except OSError:
        pass


def run_deploy():
    state.update(state="running", started=time.time(), finished=None, exit_code=None, step="git pull", log="")
    append_log(f"=== deploy started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    try:
        code, out = sh(["git", "-C", str(REPO_DIR), "fetch", "--all", "--prune"])
        append_log(out)
        if code == 0:
            code, out = sh(["git", "-C", str(REPO_DIR), "reset", "--hard", f"origin/{BRANCH}"])
            append_log(out)
        if code != 0:
            raise RuntimeError("git step failed")

        state["step"] = "docker compose up -d --build"
        code, out = sh(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build", SERVICE])
        append_log(out)
        if code != 0:
            raise RuntimeError("docker compose step failed")

        state.update(state="done", exit_code=0, step="done")
    except subprocess.TimeoutExpired as e:
        append_log(f"\n!! timed out: {e}\n")
        state.update(state="failed", exit_code=-1)
    except Exception as e:
        append_log(f"\n!! {e}\n")
        state.update(state="failed", exit_code=state.get("exit_code") or 1)
    finally:
        state["finished"] = time.time()
        state["commit"] = git_commit()
        append_log(f"=== deploy {state['state']} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OrderTrackerDeploy/1"

    def log_message(self, fmt, *args):
        pass  # quiet; app-level logging happens via append_log

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        m = re.match(r"^Bearer\s+(.+)$", auth)
        tok = self.headers.get("X-Api-Token", "") if not m else m.group(1)
        return bool(tok) and hmac.compare_digest(tok, TOKEN)

    def _status_body(self):
        s = dict(state)
        s["commit"] = s["commit"] or git_commit()
        s["log_tail"] = s.pop("log")[-4000:]
        return s

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/deploy/status":
            if not self._authed():
                return self._send(401, {"error": "bad or missing token"})
            return self._send(200, self._status_body())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/deploy":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "bad or missing token"})
        with lock:
            if state["state"] == "running":
                return self._send(409, self._status_body())
            threading.Thread(target=run_deploy, daemon=True).start()
            time.sleep(0.05)  # let state flip to "running" before we respond
        self._send(202, {"status": "started", "branch": BRANCH})


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DEPLOY_TOKEN is not set (add it to .env) - refusing to start unauthenticated")
    state["commit"] = git_commit()
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
