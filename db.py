"""SQLite storage for Thrice users, daily questions, and daily scores.

Schema:
  users          — one row per player (with hashed 4-digit PIN)
  questions      — one row per (game_date, question_number); answers + clues
  daily_scores   — one row per (user, game_date). Holds either a per-question
                   breakdown (q1_pts..q5_pts, each 0-3) or a total override
                   (0-15). UNIQUE (user_id, game_date) enforces one score/day.

The "effective" daily total for a user is:
    COALESCE(total_override,
             COALESCE(q1_pts,0)+COALESCE(q2_pts,0)+...+COALESCE(q5_pts,0))
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path(__file__).parent / "thrice.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    pin_salt    TEXT    NOT NULL,
    pin_hash    TEXT    NOT NULL,
    avatar_path TEXT,                                  -- relative to static/, e.g. 'avatars/1_abc.png'
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date       TEXT    NOT NULL,                    -- ISO date 'YYYY-MM-DD'
    question_number INTEGER NOT NULL CHECK (question_number BETWEEN 1 AND 5),
    category        TEXT,
    clues_json      TEXT    NOT NULL,                    -- JSON array of 3 strings
    answer          TEXT,
    scraped_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (game_date, question_number)
);

CREATE TABLE IF NOT EXISTS daily_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_date       TEXT    NOT NULL,
    q1_pts          INTEGER CHECK (q1_pts IS NULL OR q1_pts BETWEEN 0 AND 3),
    q2_pts          INTEGER CHECK (q2_pts IS NULL OR q2_pts BETWEEN 0 AND 3),
    q3_pts          INTEGER CHECK (q3_pts IS NULL OR q3_pts BETWEEN 0 AND 3),
    q4_pts          INTEGER CHECK (q4_pts IS NULL OR q4_pts BETWEEN 0 AND 3),
    q5_pts          INTEGER CHECK (q5_pts IS NULL OR q5_pts BETWEEN 0 AND 3),
    total_override  INTEGER CHECK (total_override IS NULL OR total_override BETWEEN 0 AND 15),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, game_date)
);

CREATE INDEX IF NOT EXISTS daily_scores_user_idx ON daily_scores(user_id);
CREATE INDEX IF NOT EXISTS daily_scores_date_idx ON daily_scores(game_date);
CREATE INDEX IF NOT EXISTS questions_date_idx    ON questions(game_date);
"""

# COALESCE expression used in queries to derive the effective daily total.
_EFFECTIVE_TOTAL_SQL = (
    "COALESCE(d.total_override, "
    "COALESCE(d.q1_pts,0)+COALESCE(d.q2_pts,0)+COALESCE(d.q3_pts,0)+"
    "COALESCE(d.q4_pts,0)+COALESCE(d.q5_pts,0))"
)


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migration: add avatar_path to existing users tables that don't have it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_path" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT")
    conn.commit()


def set_avatar_path(conn: sqlite3.Connection, user_id: int, path: Optional[str]) -> None:
    conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (path, user_id))
    conn.commit()


def _hash_pin(pin: str, salt: str) -> str:
    # PBKDF2 keeps brute-forcing a 4-digit PIN slow even if the DB leaks.
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _validate_pin(pin: str) -> None:
    if not (isinstance(pin, str) and len(pin) == 4 and pin.isdigit()):
        raise ValueError("PIN must be exactly 4 digits")


def create_user(conn: sqlite3.Connection, name: str, pin: str) -> int:
    """Create a new user with a 4-digit PIN. Raises if the name is taken."""
    name = _normalize_name(name)
    if not name:
        raise ValueError("user name cannot be empty")
    _validate_pin(pin)
    salt = os.urandom(16).hex()
    pin_hash = _hash_pin(pin, salt)
    try:
        cur = conn.execute(
            "INSERT INTO users (name, pin_salt, pin_hash) VALUES (?, ?, ?)",
            (name, salt, pin_hash),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError("user name already taken") from e
    conn.commit()
    return cur.lastrowid


def verify_user(conn: sqlite3.Connection, name: str, pin: str) -> Optional[int]:
    """Return user id if name + pin match, otherwise None."""
    name = _normalize_name(name)
    if not name or not (isinstance(pin, str) and len(pin) == 4 and pin.isdigit()):
        return None
    row = conn.execute(
        "SELECT id, pin_salt, pin_hash FROM users WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return None
    expected = _hash_pin(pin, row["pin_salt"])
    return row["id"] if hmac.compare_digest(expected, row["pin_hash"]) else None


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, avatar_path, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def upsert_question(
    conn: sqlite3.Connection,
    game_date: str,
    question_number: int,
    category: Optional[str],
    clues: Iterable[str],
    answer: Optional[str],
) -> int:
    clues_list = list(clues)
    if len(clues_list) != 3:
        raise ValueError(f"expected 3 clues, got {len(clues_list)}")
    cur = conn.execute(
        """
        INSERT INTO questions (game_date, question_number, category, clues_json, answer)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (game_date, question_number) DO UPDATE SET
            category   = excluded.category,
            clues_json = excluded.clues_json,
            answer     = excluded.answer
        """,
        (game_date, question_number, category, json.dumps(clues_list, ensure_ascii=False), answer),
    )
    conn.commit()
    # ON CONFLICT … DO UPDATE doesn't populate lastrowid reliably, so look it up.
    row = conn.execute(
        "SELECT id FROM questions WHERE game_date = ? AND question_number = ?",
        (game_date, question_number),
    ).fetchone()
    return row["id"]


def _validate_question_points(q_pts: Optional[list]) -> tuple[Optional[int], ...]:
    """Validate a list of 5 question scores; each entry must be None or int in [0,3]."""
    if q_pts is None:
        return (None,) * 5
    if len(q_pts) != 5:
        raise ValueError("q_pts must have exactly 5 entries")
    out = []
    for i, v in enumerate(q_pts, 1):
        if v is None or v == "":
            out.append(None)
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"q{i} must be an integer between 0 and 3")
        if not 0 <= iv <= 3:
            raise ValueError(f"q{i} must be between 0 and 3")
        out.append(iv)
    return tuple(out)


def upsert_daily_score(
    conn: sqlite3.Connection,
    user_id: int,
    game_date: str,
    q_pts: Optional[list] = None,
    total_override: Optional[int] = None,
) -> int:
    """Insert or update a user's score for game_date. Returns the row id."""
    q1, q2, q3, q4, q5 = _validate_question_points(q_pts)

    if total_override is not None and total_override != "":
        try:
            total_override = int(total_override)
        except (TypeError, ValueError):
            raise ValueError("total_override must be an integer between 0 and 15")
        if not 0 <= total_override <= 15:
            raise ValueError("total_override must be between 0 and 15")
    else:
        total_override = None

    conn.execute(
        """
        INSERT INTO daily_scores
            (user_id, game_date, q1_pts, q2_pts, q3_pts, q4_pts, q5_pts, total_override)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, game_date) DO UPDATE SET
            q1_pts         = excluded.q1_pts,
            q2_pts         = excluded.q2_pts,
            q3_pts         = excluded.q3_pts,
            q4_pts         = excluded.q4_pts,
            q5_pts         = excluded.q5_pts,
            total_override = excluded.total_override,
            updated_at     = datetime('now')
        """,
        (user_id, game_date, q1, q2, q3, q4, q5, total_override),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM daily_scores WHERE user_id = ? AND game_date = ?",
        (user_id, game_date),
    ).fetchone()
    return row["id"]


def get_daily_score(
    conn: sqlite3.Connection, user_id: int, game_date: str
) -> Optional[dict]:
    row = conn.execute(
        f"""
        SELECT d.q1_pts, d.q2_pts, d.q3_pts, d.q4_pts, d.q5_pts,
               d.total_override,
               {_EFFECTIVE_TOTAL_SQL} AS effective_total,
               d.updated_at
        FROM daily_scores d
        WHERE d.user_id = ? AND d.game_date = ?
        """,
        (user_id, game_date),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def leaderboard_for_date(conn: sqlite3.Connection, game_date: str) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT u.name,
               u.avatar_path,
               {_EFFECTIVE_TOTAL_SQL} AS total,
               (d.total_override IS NOT NULL) AS used_total_override
        FROM daily_scores d
        JOIN users u ON u.id = d.user_id
        WHERE d.game_date = ?
        ORDER BY total DESC, u.name COLLATE NOCASE
        """,
        (game_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def user_stats(conn: sqlite3.Connection, user_id: int) -> dict:
    """Personal stats for one user: totals, averages, per-category breakdown."""
    summary_row = conn.execute(
        f"""
        SELECT COUNT(d.id)               AS games_played,
               COALESCE(SUM({_EFFECTIVE_TOTAL_SQL}), 0) AS total_points,
               AVG({_EFFECTIVE_TOTAL_SQL} * 1.0)        AS avg_total
        FROM daily_scores d
        WHERE d.user_id = ?
        """,
        (user_id,),
    ).fetchone()

    best_day = conn.execute(
        f"""
        SELECT d.game_date,
               {_EFFECTIVE_TOTAL_SQL} AS total
        FROM daily_scores d
        WHERE d.user_id = ?
        ORDER BY total DESC, d.game_date DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    per_category = conn.execute(
        """
        WITH category_points AS (
          SELECT q.category,
                 CASE q.question_number
                   WHEN 1 THEN d.q1_pts
                   WHEN 2 THEN d.q2_pts
                   WHEN 3 THEN d.q3_pts
                   WHEN 4 THEN d.q4_pts
                   WHEN 5 THEN d.q5_pts
                 END AS pts
          FROM daily_scores d
          JOIN questions    q ON q.game_date = d.game_date
          WHERE d.user_id = ?
            AND d.total_override IS NULL
        )
        SELECT category,
               AVG(pts * 1.0) AS avg_pts,
               COUNT(pts)     AS games_played
        FROM category_points
        WHERE pts IS NOT NULL
        GROUP BY category
        ORDER BY avg_pts DESC, category COLLATE NOCASE
        """,
        (user_id,),
    ).fetchall()

    return {
        "games_played": summary_row["games_played"] or 0,
        "total_points": summary_row["total_points"] or 0,
        "avg_total":    summary_row["avg_total"],     # may be None if no games
        "best_day":     dict(best_day) if best_day else None,
        "per_category": [dict(r) for r in per_category],
    }


def leaderboard_by_category(
    conn: sqlite3.Connection,
    category: str,
) -> list[dict]:
    """Average points per user on questions of one category.

    Only per-question scores contribute. Rows where `total_override` is set
    are excluded (no per-category breakdown is recoverable). NULL qN_pts are
    treated as "not recorded" and excluded from the average; explicit 0
    counts (the user logged a zero that day).
    """
    rows = conn.execute(
        """
        WITH category_points AS (
          SELECT u.name,
                 u.avatar_path,
                 CASE q.question_number
                   WHEN 1 THEN d.q1_pts
                   WHEN 2 THEN d.q2_pts
                   WHEN 3 THEN d.q3_pts
                   WHEN 4 THEN d.q4_pts
                   WHEN 5 THEN d.q5_pts
                 END AS pts
          FROM users u
          JOIN daily_scores d ON d.user_id = u.id
          JOIN questions    q ON q.game_date = d.game_date
          WHERE d.total_override IS NULL
            AND q.category = ?
        )
        SELECT name,
               MAX(avatar_path)  AS avatar_path,
               AVG(pts * 1.0)    AS avg_pts,
               COUNT(pts)        AS games_played
        FROM category_points
        WHERE pts IS NOT NULL
        GROUP BY name
        ORDER BY avg_pts DESC, name COLLATE NOCASE
        """,
        (category,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    """Distinct categories with how many questions have each one."""
    rows = conn.execute(
        """
        SELECT category, COUNT(*) AS count
        FROM questions
        WHERE category IS NOT NULL AND category != ''
        GROUP BY category
        ORDER BY count DESC, category COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, created_at FROM users ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def daily_totals_for_users(
    conn: sqlite3.Connection,
    user_names: list[str],
    date_from: str,
    date_to: str,
) -> list[dict]:
    """Return (name, game_date, total) rows for the given users in the range."""
    if not user_names:
        return []
    placeholders = ",".join("?" * len(user_names))
    rows = conn.execute(
        f"""
        SELECT u.name, d.game_date, {_EFFECTIVE_TOTAL_SQL} AS total
        FROM daily_scores d
        JOIN users u ON u.id = d.user_id
        WHERE u.name IN ({placeholders})
          AND d.game_date BETWEEN ? AND ?
        ORDER BY d.game_date, u.name COLLATE NOCASE
        """,
        (*user_names, date_from, date_to),
    ).fetchall()
    return [dict(r) for r in rows]


def leaderboard_by_total(conn: sqlite3.Connection) -> list[dict]:
    """All-time total points across every recorded day, descending."""
    rows = conn.execute(
        f"""
        SELECT u.name,
               u.avatar_path,
               COUNT(d.id)                       AS games_played,
               SUM({_EFFECTIVE_TOTAL_SQL})       AS total_points
        FROM users u
        JOIN daily_scores d ON d.user_id = u.id
        GROUP BY u.id
        ORDER BY total_points DESC, u.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def leaderboard_by_average(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT u.name,
               u.avatar_path,
               COUNT(d.id) AS games_played,
               AVG({_EFFECTIVE_TOTAL_SQL} * 1.0) AS avg_total,
               SUM({_EFFECTIVE_TOTAL_SQL})      AS total_points
        FROM users u
        JOIN daily_scores d ON d.user_id = u.id
        GROUP BY u.id
        ORDER BY avg_total DESC, u.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # Initialize the DB file when run directly.
    conn = connect()
    init_schema(conn)
    print(f"Initialized {DB_PATH}")
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name"
    ):
        print(" ", row["name"])
