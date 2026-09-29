"""SQLite database layer."""
from __future__ import annotations
import sqlite3
import json
import threading
from datetime import datetime
from typing import Any, Optional
from .config import DB_PATH


class Database:
    def __init__(self):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        cur = self.conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            enabled INTEGER DEFAULT 1,
            credit INTEGER DEFAULT 0,
            tier TEXT DEFAULT 'FREE',
            cookie_path TEXT,
            cookie_exp TEXT,
            token_exp TEXT,
            proxy TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            folder_path TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            user_email TEXT,
            name TEXT,
            mode TEXT,
            quality TEXT,
            image_model TEXT,
            aspect_ratio TEXT,
            resolution TEXT,
            duration INTEGER DEFAULT 8,
            concurrent INTEGER DEFAULT 1,
            output_folder TEXT,
            total_count INTEGER DEFAULT 0,
            done_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            character_images_json TEXT,
            status TEXT DEFAULT 'PENDING',
            idea TEXT,
            flow_account_email TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS task_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            prompt TEXT,
            status TEXT DEFAULT 'PENDING',
            output_path TEXT,
            credit_cost INTEGER DEFAULT 0,
            error_message TEXT,
            extra_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS shakker_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_uuid TEXT UNIQUE NOT NULL,   -- shakker user UUID, the stable identity
            email TEXT,                        -- display only — may change
            user_id INTEGER,                   -- shakker numeric user ID
            account_id INTEGER,                -- shakker memberId (subscription record)
            token TEXT,                        -- 44-char hex API token sent in `token:` header
            webid TEXT,                        -- shakker `webid` cookie, reused as `cid` in gen payloads
            tier TEXT DEFAULT 'FREE',          -- accountLevelDesc, e.g. "Pro"
            total_power INTEGER DEFAULT 0,     -- monthly quota
            used_power INTEGER DEFAULT 0,
            usable_power INTEGER DEFAULT 0,    -- shown in UI as credit balance
            concurrent INTEGER DEFAULT 1,      -- per-account concurrent limit (server-enforced)
            expiry TEXT,                       -- subscription endTime ISO date
            enabled INTEGER DEFAULT 1,
            status TEXT DEFAULT 'PENDING',     -- PENDING | OK | TOKEN_EXPIRED | ERROR
            status_msg TEXT,
            last_check_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        self.conn.commit()
        # Idempotent column additions for existing DBs (older schema)
        self._add_column_if_missing("tasks", "duration", "INTEGER DEFAULT 8")
        self._add_column_if_missing("tasks", "user_email", "TEXT")
        self._add_column_if_missing("tasks", "idea", "TEXT")
        self._add_column_if_missing("tasks", "flow_account_email", "TEXT")
        self._add_column_if_missing("shakker_accounts", "webid", "TEXT")
        # Trình dựng video (Part B): editor state JSON + last-saved time.
        self._add_column_if_missing("projects", "data_json", "TEXT")
        self._add_column_if_missing("projects", "updated_at", "TEXT")
        # Ensure existing accounts with >= 500 credits don't stay marked as FREE
        try:
            self.conn.execute(
                "UPDATE accounts SET tier = 'ULTRA' WHERE credit >= 500 AND (tier IS NULL OR tier = 'FREE')"
            )
            self.conn.commit()
        except Exception:
            pass

    def _add_column_if_missing(self, table: str, column: str, decl: str):
        try:
            cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                self.conn.commit()
        except Exception:
            pass

    # ---------------------- Accounts ----------------------
    def get_accounts(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_account(self, account_id: int) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(r) if r else None

    def get_account_by_email(self, email: str) -> Optional[dict]:
        if not email:
            return None
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM accounts WHERE LOWER(email)=LOWER(?)", (email.strip(),)
            ).fetchone()
            return dict(r) if r else None

    def add_account(self, email: str) -> int:
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO accounts(email) VALUES(?)", (email,)
                )
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                r = self.conn.execute(
                    "SELECT id FROM accounts WHERE email=?", (email,)
                ).fetchone()
                return r["id"]

    def update_account(self, account_id: int, **fields):
        if not fields:
            return
        keys = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE accounts SET {keys} WHERE id=?",
                (*fields.values(), account_id),
            )
            self.conn.commit()

    def delete_account(self, account_id: int):
        with self._lock:
            self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.conn.commit()

    # ---------------------- Shakker Accounts ----------------------
    def get_shakker_accounts(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM shakker_accounts ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_shakker_account(self, account_id: int) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM shakker_accounts WHERE id=?", (account_id,)
            ).fetchone()
            return dict(r) if r else None

    def get_shakker_account_by_uuid(self, user_uuid: str) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM shakker_accounts WHERE user_uuid=?", (user_uuid,)
            ).fetchone()
            return dict(r) if r else None

    def upsert_shakker_account(self, user_uuid: str, **fields) -> int:
        """Insert or update a shakker account keyed by user_uuid.

        Called by the extension sync endpoint when the user logs into
        shakker.ai — the extension reports {user_uuid, email, user_id,
        account_id, token, ...} and we either create a new row or refresh
        the existing one (e.g. token rotated, credit changed).

        Returns the row id.
        """
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM shakker_accounts WHERE user_uuid=?", (user_uuid,)
            ).fetchone()
            if existing:
                if fields:
                    keys = ",".join(f"{k}=?" for k in fields)
                    self.conn.execute(
                        f"UPDATE shakker_accounts SET {keys} WHERE id=?",
                        (*fields.values(), existing["id"]),
                    )
                    self.conn.commit()
                return existing["id"]
            fields = {"user_uuid": user_uuid, **fields}
            keys = ",".join(fields.keys())
            marks = ",".join("?" * len(fields))
            cur = self.conn.execute(
                f"INSERT INTO shakker_accounts({keys}) VALUES({marks})",
                tuple(fields.values()),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_shakker_account(self, account_id: int, **fields):
        if not fields:
            return
        keys = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE shakker_accounts SET {keys} WHERE id=?",
                (*fields.values(), account_id),
            )
            self.conn.commit()

    def delete_shakker_account(self, account_id: int):
        with self._lock:
            self.conn.execute(
                "DELETE FROM shakker_accounts WHERE id=?", (account_id,)
            )
            self.conn.commit()

    # ---------------------- Tasks ----------------------
    def create_task(self, **fields) -> int:
        if "character_images" in fields:
            fields["character_images_json"] = json.dumps(fields.pop("character_images"))
        keys = ",".join(fields.keys())
        marks = ",".join("?" * len(fields))
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO tasks({keys}) VALUES({marks})", tuple(fields.values())
            )
            self.conn.commit()
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(r) if r else None

    def update_task(self, task_id: int, **fields):
        if not fields:
            return
        keys = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE tasks SET {keys} WHERE id=?",
                (*fields.values(), task_id),
            )
            self.conn.commit()

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------------- Task Items ----------------------
    def add_task_item(self, task_id: int, prompt: str, extra: Optional[dict] = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO task_items(task_id, prompt, extra_json) VALUES(?,?,?)",
                (task_id, prompt, json.dumps(extra) if extra else None),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_task_items(self, task_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_items WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_item(self, item_id: int) -> Optional[dict]:
        """Direct lookup of a single task_item by its primary key. Used by the
        per-item retry endpoint (avoids scanning every task)."""
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM task_items WHERE id=?", (item_id,)
            ).fetchone()
            return dict(r) if r else None

    def update_item(self, item_id: int, **fields):
        keys = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE task_items SET {keys} WHERE id=?",
                (*fields.values(), item_id),
            )
            self.conn.commit()

    # ---------------------- Projects (Trình dựng video) ----------------------
    def create_project(self, name: str, data_json: str = "{}") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO projects(name, data_json, updated_at) VALUES(?,?,datetime('now'))",
                (name, data_json),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_project(self, project_id: int) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            return dict(r) if r else None

    def list_projects(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, created_at, updated_at FROM projects ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_project(self, project_id: int, **fields):
        if not fields:
            return
        keys = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE projects SET {keys} WHERE id=?",
                (*fields.values(), project_id),
            )
            self.conn.commit()

    def delete_project(self, project_id: int):
        with self._lock:
            self.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            self.conn.commit()

    # ---------------------- Settings ----------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not r:
                return default
            try:
                return json.loads(r["value"])
            except Exception:
                return r["value"]

    def set_setting(self, key: str, value: Any):
        v = json.dumps(value) if not isinstance(value, str) else value
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, v),
            )
            self.conn.commit()

    def all_settings(self) -> dict:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
            out = {}
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except Exception:
                    out[r["key"]] = r["value"]
            return out


db = Database()
