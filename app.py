#!/usr/bin/env python3
"""
Order Tracker - small Flask + SQLite backend.

  * serves static/index.html (no-cache, so edits show up on refresh)
  * stores one JSON document per order in SQLite
  * reads workflow.json on every request (edit it live, no restart)
  * POST /api/hook/order   -> start a new order from n8n / ERP / anything
  * on new order, creates a Telegram group in the background (see tg_group.py)
  * optional outbound webhook (n8n) for order.created / step.done / step.reopened

Run in dev:   python app.py
Run in prod:  gunicorn -w 1 --threads 8 -b 127.0.0.1:8080 --reload app:app
"""
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = Path(__file__).resolve().parent


def _load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _flag(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


DB_PATH = Path(os.environ.get("DB_PATH") or BASE / "data" / "tracker.db")
WORKFLOW_PATH = BASE / "workflow.json"
STATIC_DIR = BASE / "static"

AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASS = os.environ.get("AUTH_PASS", "")
HOOK_TOKEN = os.environ.get("HOOK_TOKEN", "")          # protects POST /api/hook/order
ERP_URL_TEMPLATE = os.environ.get("ERP_URL_TEMPLATE", "")   # e.g. https://erp.example.com/orders/{erp_ref}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")        # outbound events (n8n)
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

TG_ENABLED = _flag("TG_ENABLED", False)
TG_TITLE_TEMPLATE = os.environ.get("TG_TITLE_TEMPLATE", "{customer} | {id}")
TG_ABOUT_TEMPLATE = os.environ.get("TG_ABOUT_TEMPLATE", "Onboarding for order {id}")
TG_WELCOME = os.environ.get(
    "TG_WELCOME",
    "Welcome! This group is for the onboarding of order {id}. We will share port details, "
    "test scheduling and go-live updates here.",
)
TG_ADD_USERS = [u.strip() for u in os.environ.get("TG_ADD_USERS", "").split(",") if u.strip()]

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False


# --------------------------------------------------------------------------- db
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)  # autocommit; we BEGIN explicitly
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = db()
    con.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        " id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_ms INTEGER NOT NULL)"
    )
    con.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_order(con, o):
    """Insert/replace an order inside an open transaction; updated_ms is strictly increasing."""
    last = con.execute("SELECT COALESCE(MAX(updated_ms),0) FROM orders").fetchone()[0]
    ms = max(int(time.time() * 1000), last + 1)
    con.execute(
        "INSERT INTO orders(id,data,updated_ms) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_ms=excluded.updated_ms",
        (o["id"], json.dumps(o), ms),
    )


def all_orders():
    con = db()
    try:
        return [json.loads(r["data"]) for r in con.execute("SELECT data FROM orders ORDER BY updated_ms")]
    finally:
        con.close()


def get_order(oid):
    con = db()
    try:
        r = con.execute("SELECT data FROM orders WHERE id=?", (oid,)).fetchone()
        return json.loads(r["data"]) if r else None
    finally:
        con.close()


def sync_token():
    con = db()
    try:
        m, c = con.execute("SELECT COALESCE(MAX(updated_ms),0), COUNT(*) FROM orders").fetchone()
        return f"{m}:{c}"
    finally:
        con.close()


def deep_merge(dst, patch):
    """JSON-merge-patch style: dicts merge recursively, null deletes a key, everything else replaces."""
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict):
            if not isinstance(dst.get(k), dict):
                dst[k] = {}
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


# --------------------------------------------------------------------- workflow
def read_workflow():
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def valid_step_numbers():
    try:
        return {str(s["n"]) for s in read_workflow()["steps"]}
    except Exception:
        return {str(i) for i in range(1, 14)}


def _mtime(p):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def ui_rev():
    files = [p for p in STATIC_DIR.rglob("*") if p.is_file()] if STATIC_DIR.exists() else []
    return str(max([_mtime(p) for p in files] or [0]))


# ---------------------------------------------------------------------- helpers
def clean(v, limit=2000):
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    return str(v)[:limit]


TOP_TEXT = ("customer", "contact", "service", "telco", "site", "notes", "erp_ref", "tg_user")


def sanitize_patch(p):
    """Whitelist what a client may change. Server-owned: id, created, at, by, tg status/link-from-automation."""
    if not isinstance(p, dict):
        abort(400, "body must be a JSON object")
    out = {}
    for k in TOP_TEXT:
        if k in p:
            out[k] = clean(p[k])
    if isinstance(p.get("steps"), dict):
        valid = valid_step_numbers()
        steps = {}
        for n, sp in p["steps"].items():
            if str(n) not in valid or not isinstance(sp, dict):
                continue
            e = {}
            if "done" in sp:
                e["done"] = bool(sp["done"])
            if "note" in sp:
                e["note"] = clean(sp["note"], 4000)
            if isinstance(sp.get("fields"), dict):
                e["fields"] = {str(fk)[:40]: clean(fv) for fk, fv in sp["fields"].items()}
            steps[str(n)] = e
        out["steps"] = steps
    if isinstance(p.get("tg"), dict) and "link" in p["tg"]:
        link = clean(p["tg"]["link"]).strip()
        out["tg"] = {"link": link or None, "status": "manual" if link else "none", "error": None}
    return out


def patch_order(oid, patch, user=""):
    """Atomic read-merge-write. Returns (order|None, events)."""
    events = []
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT data FROM orders WHERE id=?", (oid,)).fetchone()
        if not r:
            con.execute("ROLLBACK")
            return None, events
        o = json.loads(r["data"])
        before = {n: bool(s.get("done")) for n, s in (o.get("steps") or {}).items()}
        deep_merge(o, patch)
        o.setdefault("steps", {})
        for n, sp in (patch.get("steps") or {}).items():
            if "done" not in sp:
                continue
            st = o["steps"].setdefault(n, {})
            was, now = before.get(n, False), bool(st.get("done"))
            if now and not was:
                st["at"], st["by"] = now_iso(), user
                events.append(("step.done", n))
            elif was and not now:
                st.pop("at", None)
                st.pop("by", None)
                events.append(("step.reopened", n))
        put_order(con, o)
        con.execute("COMMIT")
        return o, events
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def new_id():
    d = datetime.now()
    return f"ORD-{d:%y%m%d}-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(3))


def create_order(d, user="", source="ui"):
    customer = str(d.get("customer") or "").strip()
    if not customer:
        abort(400, "customer name is required")
    now = now_iso()
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        oid = new_id()
        while con.execute("SELECT 1 FROM orders WHERE id=?", (oid,)).fetchone():
            oid = new_id()
        o = {
            "id": oid,
            "customer": customer[:200],
            "created": now,
            "source": source,
            "steps": {
                "1": {
                    "done": True, "at": now, "by": user or source,
                    "fields": {"payref": clean(d.get("payref")), "formdate": now[:10]},
                }
            },
            "tg": {"status": "creating" if TG_ENABLED else "none", "link": None, "error": None,
                   "started": now if TG_ENABLED else None},
        }
        for k in TOP_TEXT:
            if k != "customer":
                o[k] = clean(d.get(k))
        put_order(con, o)
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()
    fire_webhook("order.created", o)
    if TG_ENABLED:
        threading.Thread(target=_tg_worker, args=(oid,), daemon=True).start()
    return o


# --------------------------------------------------------------------- telegram
def _tg_worker(oid):
    o = get_order(oid)
    if not o:
        return
    fmt = {"id": o["id"], "customer": o.get("customer", "")}
    try:
        import tg_group  # imported lazily so the app runs without telethon installed

        users = list(TG_ADD_USERS)
        cust = (o.get("tg_user") or "").strip()
        if cust:
            users.append(cust)
        res = tg_group.create_group(
            title=TG_TITLE_TEMPLATE.format(**fmt)[:120],
            about=TG_ABOUT_TEMPLATE.format(**fmt)[:250],
            add_users=users,
            welcome=TG_WELCOME.format(**fmt) if TG_WELCOME else None,
        )
        patch_order(oid, {"tg": {"status": "ready", "link": res["link"], "chat_id": res["chat_id"],
                                 "add_failed": res.get("add_failed") or None, "error": None}})
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI
        patch_order(oid, {"tg": {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}})


def fire_webhook(event, order, step=None):
    if not WEBHOOK_URL:
        return

    def run():
        try:
            body = json.dumps({"event": event, "step": step, "order": order}).encode()
            req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Tracker-Event": event})
            if WEBHOOK_TOKEN:
                req.add_header("X-Api-Token", WEBHOOK_TOKEN)
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("webhook %s failed: %s", event, e)

    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------------ http
@app.before_request
def _auth():
    if request.path.startswith("/api/hook/") or request.path == "/healthz":
        return None
    if AUTH_USER and AUTH_PASS:
        a = request.authorization
        ok = bool(a) and hmac.compare_digest((a.username or "").encode(), AUTH_USER.encode()) \
            and hmac.compare_digest((a.password or "").encode(), AUTH_PASS.encode())
        if not ok:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Order Tracker"'})
        g.user = a.username
    else:
        g.user = request.headers.get("X-Remote-User") or "team"  # nginx can pass $remote_user here
    return None


@app.after_request
def _nocache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(HTTPException)
def _http_err(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": e.description}), e.code
    return e


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/workflow")
def api_workflow():
    try:
        wf = read_workflow()
    except Exception as e:  # invalid JSON while someone is mid-edit
        return jsonify({"error": f"workflow.json: {e}"}), 500
    wf["config"] = {"erp_url_template": ERP_URL_TEMPLATE, "tg_enabled": TG_ENABLED}
    return jsonify(wf)


@app.get("/api/sync")
def api_sync():
    tok = sync_token()
    out = {"tok": tok, "wf_rev": str(_mtime(WORKFLOW_PATH)), "ui_rev": ui_rev()}
    if request.args.get("tok") != tok:
        out["orders"] = all_orders()
    return jsonify(out)


@app.post("/api/orders")
def api_create():
    d = request.get_json(silent=True) or {}
    return jsonify(create_order(d, user=g.user, source="ui")), 201


@app.patch("/api/orders/<oid>")
def api_patch(oid):
    patch = sanitize_patch(request.get_json(silent=True))
    o, events = patch_order(oid, patch, user=g.user)
    if o is None:
        abort(404, "order not found")
    for ev, n in events:
        fire_webhook(ev, o, step=n)
    return jsonify(o)


@app.delete("/api/orders/<oid>")
def api_delete(oid):
    con = db()
    try:
        con.execute("DELETE FROM orders WHERE id=?", (oid,))
    finally:
        con.close()
    return {"ok": True}


@app.post("/api/orders/<oid>/tg")
def api_tg_retry(oid):
    if not TG_ENABLED:
        abort(400, "Telegram automation is disabled (TG_ENABLED=false)")
    o, _ = patch_order(oid, {"tg": {"status": "creating", "error": None, "started": now_iso()}})
    if o is None:
        abort(404, "order not found")
    threading.Thread(target=_tg_worker, args=(oid,), daemon=True).start()
    return jsonify(o)


@app.post("/api/hook/order")
def hook_order():
    tok = request.headers.get("X-Api-Token", "")
    if not HOOK_TOKEN or not hmac.compare_digest(tok.encode(), HOOK_TOKEN.encode()):
        abort(401, "bad or missing X-Api-Token")
    d = request.get_json(force=True, silent=True) or {}
    return jsonify(create_order(d, user="hook", source="hook")), 201


init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")), debug=True)
