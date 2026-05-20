"""Flask app for the Thrice trivia scraper.

  Run:  python app.py
  Open: http://127.0.0.1:5000/

Features:
  - Daily scrape of thrice.geekswhodrink.com persisted to SQLite.
  - APScheduler runs the scrape automatically every day.
  - Username + 4-digit PIN login (Flask signed cookie session).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import secrets
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.utils import secure_filename

import db
from scraper import scrape, ScrapeError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("thrice")

BASE_DIR = Path(__file__).parent
SECRET_PATH = BASE_DIR / ".flask_secret"

app = Flask(__name__)


def _load_secret() -> bytes:
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(secret)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    return secret


app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # 2 MB upload cap
)

AVATAR_DIR = BASE_DIR / "static" / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_AVATAR_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}

# ─── DB plumbing ────────────────────────────────────────────────────────────

def get_conn():
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def _close_conn(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


# ─── Scraping + persistence ────────────────────────────────────────────────

_scrape_lock = threading.Lock()


def _today() -> str:
    return dt.date.today().isoformat()


def _persist_game(data: dict, game_date: str) -> None:
    conn = db.connect()
    try:
        for q in data["questions"]:
            db.upsert_question(
                conn,
                game_date=game_date,
                question_number=q["number"],
                category=q.get("category"),
                clues=q["clues"],
                answer=q.get("answer"),
            )
    finally:
        conn.close()


def _load_game_from_db(game_date: str) -> dict | None:
    conn = db.connect()
    try:
        rows = list(
            conn.execute(
                "SELECT question_number, category, clues_json, answer "
                "FROM questions WHERE game_date = ? ORDER BY question_number",
                (game_date,),
            )
        )
    finally:
        conn.close()
    if len(rows) != 5:
        return None
    import json as _json
    return {
        "game_date": game_date,
        "questions": [
            {
                "number": r["question_number"],
                "category": r["category"],
                "clues": _json.loads(r["clues_json"]),
                "answer": r["answer"],
            }
            for r in rows
        ],
    }


def run_scrape_and_persist(game_date: str | None = None) -> dict:
    """Scrape the site and persist to the DB. Returns the game dict."""
    game_date = game_date or _today()
    with _scrape_lock:
        data = asyncio.run(scrape())
        _persist_game(data, game_date)
        return _load_game_from_db(game_date) or {"game_date": game_date, "questions": data["questions"]}


# ─── Scheduler ─────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()  # picks local timezone via tzlocal


def scheduled_scrape():
    date = _today()
    log.info("scheduled scrape starting for %s", date)
    try:
        run_scrape_and_persist(date)
        log.info("scheduled scrape ok for %s", date)
    except Exception as e:
        log.exception("scheduled scrape failed for %s: %s", date, e)


def _start_scheduler():
    if scheduler.running:
        return
    # Run daily at 06:00 local time — the Geeks Who Drink daily game flips at midnight,
    # giving us a comfortable buffer.
    scheduler.add_job(scheduled_scrape, CronTrigger(hour=6, minute=0), id="daily_scrape", replace_existing=True)
    scheduler.start()
    log.info("scheduler started; daily_scrape scheduled at 06:00")

    # Catch-up: if today's game isn't in the DB yet, scrape it now in the background.
    def _catchup():
        if _load_game_from_db(_today()) is None:
            log.info("catch-up scrape for %s", _today())
            try:
                run_scrape_and_persist(_today())
            except Exception as e:
                log.exception("catch-up scrape failed: %s", e)

    threading.Thread(target=_catchup, daemon=True).start()


# ─── Auth helpers ─────────────────────────────────────────────────────────

def current_user_id() -> int | None:
    return session.get("user_id")


def _avatar_url_for(path: str | None) -> str | None:
    return f"/static/{path}" if path else None


def current_user() -> dict | None:
    uid = current_user_id()
    if uid is None:
        return None
    row = db.get_user(get_conn(), uid)
    if row is None:
        session.clear()
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "avatar_url": _avatar_url_for(row["avatar_path"]),
    }


# ─── Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/me")
def api_me():
    return jsonify({"user": current_user()})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    pin = body.get("pin") or ""
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        uid = db.create_user(get_conn(), name, pin)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    session.permanent = True
    session["user_id"] = uid
    return jsonify({"user": current_user()})


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    pin = body.get("pin") or ""
    uid = db.verify_user(get_conn(), name, pin)
    if uid is None:
        return jsonify({"error": "invalid username or PIN"}), 401
    session.permanent = True
    session["user_id"] = uid
    return jsonify({"user": current_user()})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/avatar", methods=["POST"])
def api_avatar_upload():
    uid = current_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "no file"}), 400

    fname = secure_filename(file.filename).lower()
    ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
    if ext not in ALLOWED_AVATAR_EXTS:
        return jsonify({"error": f"unsupported file type: .{ext or '?'}"}), 400

    # Verify it actually looks like an image (header sniff — defends against
    # someone uploading a text file with .png renamed on).
    header = file.stream.read(16)
    file.stream.seek(0)
    sigs = (
        b"\x89PNG\r\n\x1a\n",   # png
        b"\xff\xd8\xff",         # jpeg
        b"GIF87a", b"GIF89a",    # gif
        b"RIFF",                 # webp (RIFF...WEBP)
    )
    if not any(header.startswith(s) for s in sigs):
        return jsonify({"error": "file does not look like an image"}), 400

    # Delete any prior avatar for this user.
    conn = get_conn()
    prior = db.get_user(conn, uid)
    if prior and prior["avatar_path"]:
        prior_full = BASE_DIR / "static" / prior["avatar_path"]
        try:
            prior_full.unlink(missing_ok=True)
        except OSError:
            pass

    suffix = secrets.token_hex(6)
    new_name = f"{uid}_{suffix}.{ext}"
    file.save(AVATAR_DIR / new_name)
    db.set_avatar_path(conn, uid, f"avatars/{new_name}")

    return jsonify({"user": current_user()})


@app.route("/api/avatar", methods=["DELETE"])
def api_avatar_clear():
    uid = current_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    conn = get_conn()
    row = db.get_user(conn, uid)
    if row and row["avatar_path"]:
        full = BASE_DIR / "static" / row["avatar_path"]
        try:
            full.unlink(missing_ok=True)
        except OSError:
            pass
    db.set_avatar_path(conn, uid, None)
    return jsonify({"user": current_user()})


@app.route("/api/me/stats")
def api_me_stats():
    uid = current_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    return jsonify(db.user_stats(get_conn(), uid))


def _require_user_id() -> int | None:
    return current_user_id()


@app.route("/api/today")
def api_today():
    """Return the categories + question numbers for today (no scoring required)."""
    date = _today()
    data = _load_game_from_db(date)
    if data is None:
        # Don't trigger a scrape from this endpoint — it should be fast.
        return jsonify({"game_date": date, "questions": []})
    return jsonify(
        {
            "game_date": date,
            "questions": [
                {"number": q["number"], "category": q["category"]}
                for q in data["questions"]
            ],
        }
    )


@app.route("/api/score")
def api_score_get():
    uid = _require_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    date = request.args.get("date") or _today()
    row = db.get_daily_score(get_conn(), uid, date)
    return jsonify({"game_date": date, "score": row})


@app.route("/api/score", methods=["PUT"])
def api_score_put():
    uid = _require_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    body = request.get_json(silent=True) or {}
    date = body.get("game_date") or _today()
    q_pts = body.get("q_pts")  # list of 5 ints/null, or None
    total_override = body.get("total_override")  # int or None

    # Treat empty payload as a clarifying error.
    if q_pts is None and total_override in (None, ""):
        return jsonify({"error": "provide q_pts (list of 5) or total_override"}), 400

    try:
        db.upsert_daily_score(get_conn(), uid, date, q_pts=q_pts, total_override=total_override)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    row = db.get_daily_score(get_conn(), uid, date)
    return jsonify({"game_date": date, "score": row})


@app.route("/api/score", methods=["DELETE"])
def api_score_delete():
    uid = _require_user_id()
    if uid is None:
        return jsonify({"error": "not signed in"}), 401
    date = (request.args.get("date") or _today())
    conn = get_conn()
    conn.execute(
        "DELETE FROM daily_scores WHERE user_id = ? AND game_date = ?", (uid, date)
    )
    conn.commit()
    return jsonify({"ok": True, "game_date": date})


@app.route("/api/leaderboard/daily")
def api_leaderboard_daily():
    date = request.args.get("date") or _today()
    rows = db.leaderboard_for_date(get_conn(), date)
    return jsonify({"game_date": date, "entries": rows})


@app.route("/api/categories")
def api_categories():
    return jsonify({"categories": db.list_categories(get_conn())})


@app.route("/api/users")
def api_users():
    rows = db.list_users(get_conn())
    return jsonify({"users": [{"id": r["id"], "name": r["name"]} for r in rows]})


@app.route("/api/compare")
def api_compare():
    """Side-by-side daily totals for selected users.

    Query: ?days=N&users=name1,name2,...
    Returns dates (chronological) + per-user scores keyed by date.
    """
    raw_days = request.args.get("days", "7")
    try:
        days = int(raw_days)
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400
    days = max(1, min(days, 365))

    users_str = request.args.get("users", "")
    user_names = [n.strip().lower() for n in users_str.split(",") if n.strip()]

    today = dt.date.today()
    date_from = (today - dt.timedelta(days=days - 1)).isoformat()
    date_to = today.isoformat()

    rows = db.daily_totals_for_users(get_conn(), user_names, date_from, date_to)

    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    scores: dict[str, dict[str, int]] = {name: {} for name in user_names}
    for r in rows:
        scores[r["name"]][r["game_date"]] = r["total"]

    return jsonify(
        {"from": date_from, "to": date_to, "users": user_names, "dates": dates, "scores": scores}
    )


@app.route("/api/leaderboard/total")
def api_leaderboard_total():
    rows = db.leaderboard_by_total(get_conn())
    return jsonify({"entries": rows})


@app.route("/api/leaderboard/average")
def api_leaderboard_average():
    rows = db.leaderboard_by_average(get_conn())
    return jsonify({"entries": rows})


@app.route("/api/leaderboard/category")
def api_leaderboard_category():
    category = request.args.get("category")
    if not category:
        return jsonify({"error": "category is required"}), 400
    rows = db.leaderboard_by_category(get_conn(), category)
    return jsonify({"category": category, "entries": rows})


@app.route("/api/scrape")
def api_scrape():
    """Return today's game from the DB; if missing, scrape it on demand."""
    date = _today()
    data = _load_game_from_db(date)
    if data is None:
        try:
            data = run_scrape_and_persist(date)
        except ScrapeError as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"cached": False, "game_date": date, "questions": data["questions"]})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    date = _today()
    try:
        data = run_scrape_and_persist(date)
    except ScrapeError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"cached": False, "game_date": date, "questions": data["questions"]})


# ─── Entry point ──────────────────────────────────────────────────────────

# Make sure schema exists at import time so endpoints can rely on it.
_bootstrap = db.connect()
try:
    db.init_schema(_bootstrap)
finally:
    _bootstrap.close()


if __name__ == "__main__":
    _start_scheduler()
    # use_reloader=False so the scheduler / scraper don't get duplicated.
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
