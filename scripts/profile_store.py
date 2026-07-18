"""拾光关系档案注册表。

每个联系人/关系拥有完全独立的数据目录，聊天库、AI 对话、报告和密钥互不混用。
注册表只保存档案元信息，不保存聊天正文。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


RELATION_TYPES = {
    "couple": "伴侣",
    "friend": "朋友",
    "family": "家人",
    "classmate": "同学",
    "colleague": "同事",
    "other": "其他",
}


def _safe_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-")
    return value[:48] or uuid.uuid4().hex[:12]


class ProfileStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.db_path = self.root / "data" / "profiles.db"
        self.profiles_dir = self.root / "profiles"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def _connect(self):
        con = self.connect()
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init(self):
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles(
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    contact_id TEXT NOT NULL DEFAULT '',
                    relation_type TEXT NOT NULL DEFAULT 'friend',
                    relation_note TEXT NOT NULL DEFAULT '',
                    edition TEXT NOT NULL DEFAULT 'general',
                    data_root TEXT NOT NULL,
                    import_source TEXT NOT NULL DEFAULT '',
                    import_status TEXT NOT NULL DEFAULT 'new',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    date_from TEXT NOT NULL DEFAULT '',
                    date_to TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registry_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
        self._adopt_legacy_if_needed()
        self._refresh_legacy_stats()

    def _refresh_legacy_stats(self):
        item = self.get("legacy")
        path = self.root / "data" / "clean" / "chat.db"
        if not item or not path.exists() or int(item.get("message_count") or 0) > 0:
            return
        try:
            con = sqlite3.connect(path)
            count, start, end = con.execute(
                "SELECT count(*),min(date),max(date) FROM messages"
            ).fetchone()
            con.close()
            self.update("legacy", message_count=int(count or 0), date_from=start or "", date_to=end or "")
        except sqlite3.Error:
            pass

    def _adopt_legacy_if_needed(self):
        """开发者原工程已经有 chat.db 时，将其登记为现有档案，不搬动私密数据。"""
        legacy_chat = self.root / "data" / "clean" / "chat.db"
        with self._connect() as con:
            count = con.execute("SELECT count(*) FROM profiles").fetchone()[0]
            if count or not legacy_chat.exists():
                return
            now = int(time.time())
            con.execute(
                """INSERT INTO profiles(
                    id,display_name,contact_id,relation_type,relation_note,edition,data_root,
                    import_source,import_status,message_count,date_from,date_to,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("legacy", "已有关系", "", "other", "从已有本地数据库登记",
                 "general", str(self.root), "legacy", "ready", 0, "", "", now, now),
            )
            con.execute(
                "INSERT OR REPLACE INTO registry_settings(key,value,updated_at) VALUES('active_profile_id',?,?)",
                ("legacy", now),
            )
        self._write_profile_json(self.get("legacy"))

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def list_profiles(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM profiles ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, profile_id: str):
        with self._connect() as con:
            row = con.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return self._row(row)

    def active_id(self):
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM registry_settings WHERE key='active_profile_id'"
            ).fetchone()
        return row[0] if row else ""

    def active(self):
        item = self.get(self.active_id()) if self.active_id() else None
        if item:
            return item
        items = self.list_profiles()
        return items[0] if items else None

    def set_active(self, profile_id: str):
        if not self.get(profile_id):
            raise ValueError("关系档案不存在")
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO registry_settings(key,value,updated_at) VALUES('active_profile_id',?,?)",
                (profile_id, int(time.time())),
            )
        return self.get(profile_id)

    def create(self, display_name: str, relation_type="friend", relation_note="",
               edition="general", contact_id=""):
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("请填写对方的称呼")
        relation_type = relation_type if relation_type in RELATION_TYPES else "other"
        pid = _safe_id(contact_id) + "-" + uuid.uuid4().hex[:6]
        data_root = self.profiles_dir / pid
        for rel in ("data/clean", "data/plain", "config", "reports/care", "work", "logs"):
            (data_root / rel).mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        had_active = self.active_id()
        with self._connect() as con:
            con.execute(
                """INSERT INTO profiles(
                    id,display_name,contact_id,relation_type,relation_note,edition,data_root,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (pid, name, str(contact_id or ""), relation_type, str(relation_note or ""),
                 edition, str(data_root), now, now),
            )
        if not had_active:
            self.set_active(pid)
        self._write_profile_json(self.get(pid))
        return self.get(pid)

    def update(self, profile_id: str, **values):
        allowed = {
            "display_name", "contact_id", "relation_type", "relation_note", "edition",
            "import_source", "import_status", "message_count", "date_from", "date_to",
        }
        changes = {k: v for k, v in values.items() if k in allowed}
        if not changes:
            return self.get(profile_id)
        changes["updated_at"] = int(time.time())
        sql = ",".join(f"{k}=?" for k in changes)
        with self._connect() as con:
            con.execute(f"UPDATE profiles SET {sql} WHERE id=?", (*changes.values(), profile_id))
        item = self.get(profile_id)
        self._write_profile_json(item)
        return item

    def _write_profile_json(self, item):
        if not item:
            return
        path = Path(item["data_root"]) / "profile.json"
        public = {k: item[k] for k in (
            "id", "display_name", "contact_id", "relation_type", "relation_note", "edition",
            "import_source", "import_status", "message_count", "date_from", "date_to",
        )}
        path.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete(self, profile_id: str, delete_files=False):
        item = self.get(profile_id)
        if not item:
            return False
        if item["id"] == "legacy":
            raise ValueError("现有主档案不能从应用内删除")
        with self._connect() as con:
            con.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        if self.active_id() == profile_id:
            remaining = self.list_profiles()
            if remaining:
                self.set_active(remaining[0]["id"])
        if delete_files:
            import shutil
            target = Path(item["data_root"]).resolve()
            base = self.profiles_dir.resolve()
            if base in target.parents:
                shutil.rmtree(target, ignore_errors=True)
        return True
