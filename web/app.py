"""AI Job Search Buddy - web app.

Hosted mode (default): accounts, credit metering, Stripe credit packs.
Local mode (APP_MODE=local): single user, no login, no credit limits.

Run:  pip install -r web/requirements.txt && python web/app.py
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import billing

BASE = Path(__file__).parent
MODE = os.environ.get("APP_MODE", "hosted").lower()
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE / "buddy.db"))
FREE_CREDITS = int(os.environ.get("FREE_CREDITS", "10"))
COSTS = {"evaluate": 1, "apply": 3}
STATUSES = ["drafted", "applied", "interview", "offer", "rejected", "withdrawn"]

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1" if MODE == "hosted" else "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  credits INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  company TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'drafted',
  fit_score INTEGER, posting TEXT DEFAULT '', evaluation TEXT DEFAULT '',
  cover_letter TEXT DEFAULT '', cv_bullets TEXT DEFAULT '', notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
  reason TEXT NOT NULL, ref TEXT UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        if MODE == "local":
            conn.execute(
                "INSERT OR IGNORE INTO users (id, email, pw_hash, credits) VALUES (1, 'local@localhost', '!', 0)"
            )


def err(msg, code=400):
    return jsonify({"error": msg}), code


def current_user():
    uid = 1 if MODE == "local" else session.get("uid")
    if not uid:
        return None
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if user is None:
            return err("Not signed in", 401)
        g.user = user
        return fn(*a, **kw)
    return wrapper


@app.before_request
def csrf_guard():
    # JSON-only API + SameSite=Lax cookie: cross-site forms can't send application/json.
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if request.path == "/api/billing/webhook":
            return None
        if not request.is_json:
            return err("Expected application/json", 415)
    return None


def spend(user_id: int, action: str) -> bool:
    """Atomically deduct credits. Returns False if balance too low."""
    if MODE == "local":
        return True
    cost = COSTS[action]
    conn = db()
    cur = conn.execute(
        "UPDATE users SET credits = credits - ? WHERE id=? AND credits >= ?", (cost, user_id, cost)
    )
    if cur.rowcount == 0:
        return False
    conn.execute("INSERT INTO ledger (user_id, delta, reason) VALUES (?,?,?)", (user_id, -cost, action))
    conn.commit()
    return True


def refund(user_id: int, action: str):
    if MODE == "local":
        return
    cost = COSTS[action]
    conn = db()
    conn.execute("UPDATE users SET credits = credits + ? WHERE id=?", (cost, user_id))
    conn.execute("INSERT INTO ledger (user_id, delta, reason) VALUES (?,?,?)", (user_id, cost, f"refund:{action}"))
    conn.commit()


# ---------- pages ----------
@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/config")
def config():
    return jsonify({
        "mode": MODE, "costs": COSTS, "statuses": STATUSES, "free_credits": FREE_CREDITS,
        "billing_enabled": billing.enabled(), "packs": billing.PACKS,
    })


# ---------- auth ----------
@app.post("/api/signup")
def signup():
    if MODE == "local":
        return err("Not needed in local mode")
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    pw = data.get("password") or ""
    if "@" not in email or len(email) > 254:
        return err("Enter a valid email")
    if len(pw) < 8:
        return err("Password must be at least 8 characters")
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, pw_hash, credits) VALUES (?,?,?)",
            (email, generate_password_hash(pw), FREE_CREDITS),
        )
        conn.execute("INSERT INTO ledger (user_id, delta, reason) VALUES (?,?,?)",
                     (cur.lastrowid, FREE_CREDITS, "signup_bonus"))
        conn.commit()
    except sqlite3.IntegrityError:
        return err("Account already exists")
    session.clear()
    session["uid"] = cur.lastrowid
    return jsonify({"ok": True})


@app.post("/api/login")
def login():
    if MODE == "local":
        return err("Not needed in local mode")
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    user = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["pw_hash"], data.get("password") or ""):
        return err("Wrong email or password", 401)
    session.clear()
    session["uid"] = user["id"]
    return jsonify({"ok": True})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def me():
    u = g.user
    return jsonify({"email": u["email"], "credits": None if MODE == "local" else u["credits"],
                    "profile": u["profile"]})


@app.put("/api/profile")
@login_required
def save_profile():
    profile = (request.get_json().get("profile") or "")[:50_000]
    db().execute("UPDATE users SET profile=? WHERE id=?", (profile, g.user["id"]))
    db().commit()
    return jsonify({"ok": True})


# ---------- applications ----------
def row(r):
    return dict(r)


@app.get("/api/applications")
@login_required
def list_apps():
    rows = db().execute(
        "SELECT * FROM applications WHERE user_id=? ORDER BY id DESC", (g.user["id"],)
    ).fetchall()
    return jsonify([row(r) for r in rows])


@app.post("/api/applications")
@login_required
def create_app():
    d = request.get_json()
    company, role = (d.get("company") or "").strip(), (d.get("role") or "").strip()
    if not company or not role:
        return err("Company and role are required")
    cur = db().execute(
        "INSERT INTO applications (user_id, company, role, posting, notes) VALUES (?,?,?,?,?)",
        (g.user["id"], company[:200], role[:200], (d.get("posting") or "")[:60_000], (d.get("notes") or "")[:5000]),
    )
    db().commit()
    return jsonify({"id": cur.lastrowid})


def owned_app(app_id):
    return db().execute(
        "SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, g.user["id"])
    ).fetchone()


@app.patch("/api/applications/<int:app_id>")
@login_required
def update_app(app_id):
    if not owned_app(app_id):
        return err("Not found", 404)
    d = request.get_json()
    fields = {k: d[k] for k in ("status", "notes", "company", "role", "posting") if k in d}
    if "status" in fields and fields["status"] not in STATUSES:
        return err("Invalid status")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db().execute(f"UPDATE applications SET {sets} WHERE id=?", (*[str(v)[:60_000] for v in fields.values()], app_id))
        db().commit()
    return jsonify({"ok": True})


@app.delete("/api/applications/<int:app_id>")
@login_required
def delete_app(app_id):
    db().execute("DELETE FROM applications WHERE id=? AND user_id=?", (app_id, g.user["id"]))
    db().commit()
    return jsonify({"ok": True})


def run_ai(app_id, action):
    a = owned_app(app_id)
    if not a:
        return None, err("Not found", 404)
    if not g.user["profile"].strip():
        return None, err("Add your profile/CV first")
    if not a["posting"].strip():
        return None, err("Paste the job posting first")
    if not spend(g.user["id"], action):
        return None, err("Not enough credits", 402)
    try:
        fn = ai.evaluate if action == "evaluate" else ai.tailor
        return fn(g.user["profile"], a["company"], a["role"], a["posting"]), None
    except ai.AIError as e:
        refund(g.user["id"], action)
        return None, err(str(e), 502)


@app.post("/api/applications/<int:app_id>/evaluate")
@login_required
def evaluate(app_id):
    result, failure = run_ai(app_id, "evaluate")
    if failure:
        return failure
    db().execute("UPDATE applications SET fit_score=?, evaluation=? WHERE id=?",
                 (result["fit_score"], result["summary_md"], app_id))
    db().commit()
    return jsonify(result)


@app.post("/api/applications/<int:app_id>/apply")
@login_required
def apply(app_id):
    result, failure = run_ai(app_id, "apply")
    if failure:
        return failure
    db().execute("UPDATE applications SET cover_letter=?, cv_bullets=? WHERE id=?",
                 (result["cover_letter"], result["cv_bullets"], app_id))
    db().commit()
    return jsonify(result)


# ---------- billing ----------
@app.post("/api/billing/checkout")
@login_required
def checkout():
    if MODE == "local" or not billing.enabled():
        return err("Billing is not enabled")
    pack = request.get_json().get("pack")
    try:
        url = billing.checkout_url(pack, g.user["id"], request.host_url)
    except KeyError:
        return err("Unknown pack")
    return jsonify({"url": url})


@app.post("/api/billing/webhook")
def webhook():
    if not billing.enabled():
        return err("Billing is not enabled", 404)
    try:
        grant = billing.parse_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
    except billing.WebhookError as e:
        return err(str(e), 400)
    if grant:
        conn = db()
        try:
            conn.execute("INSERT INTO ledger (user_id, delta, reason, ref) VALUES (?,?,?,?)",
                         (grant["user_id"], grant["credits"], "purchase", grant["ref"]))
            conn.execute("UPDATE users SET credits = credits + ? WHERE id=?", (grant["credits"], grant["user_id"]))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()  # already processed this checkout session
    return jsonify({"received": True})


init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=int(os.environ.get("PORT", "5050")), debug=False)
