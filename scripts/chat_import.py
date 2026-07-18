"""与聊天来源无关的导入工具：规范化 JSON，并建立拾光本地数据库。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


CN = timezone(timedelta(hours=8))


def _message_type(render_type):
    mapping = {
        "text": "text", "image": "图片", "voice": "语音", "video": "视频",
        "emoji": "表情", "file": "文件", "location": "位置", "voip": "通话",
        "system": "system", "transfer": "转账", "link": "链接",
    }
    key = str(render_type or "text").strip().lower()
    return mapping.get(key, key or "其他")


def _normalize_message(item):
    ts = int(item.get("createTime") or item.get("create_time") or item.get("ts") or 0)
    sent = item.get("isSent")
    if sent is None:
        sent = item.get("is_send", item.get("isSelf", False))
    if isinstance(sent, str):
        sent = sent.strip().lower() in {"1", "true", "yes", "on"}
    typ = _message_type(item.get("renderType") or item.get("type"))
    content = str(item.get("content") or item.get("message_content") or "").strip()
    if not content:
        content = f"[{typ}]" if typ not in ("text", "system") else ""
    return {"ts": ts, "sender": "me" if bool(sent) else "her", "type": typ, "content": content}


def _find_message_list(value):
    if isinstance(value, list) and (not value or isinstance(value[0], dict)):
        return value
    if isinstance(value, dict):
        for key in ("messages", "message", "items", "data", "records", "chatRecords"):
            if key in value:
                found = _find_message_list(value[key])
                if found is not None:
                    return found
    return None


def read_exported_json(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    items = _find_message_list(raw)
    if items is None:
        raise ValueError("这个 JSON 里没有找到聊天消息，请选择聊天记录 JSON 导出文件")
    return [_normalize_message(m) for m in items]


def write_chat_db(profile_root, messages, progress=None):
    profile_root = Path(profile_root)
    clean = profile_root / "data" / "clean"
    clean.mkdir(parents=True, exist_ok=True)
    db_path = clean / "chat.db"
    json_path = clean / "messages.json"
    db_fd, db_tmp_name = tempfile.mkstemp(prefix="chat-", suffix=".db.tmp", dir=clean)
    json_fd, json_tmp_name = tempfile.mkstemp(prefix="messages-", suffix=".json.tmp", dir=clean)
    os.close(db_fd)
    os.close(json_fd)
    db_tmp = Path(db_tmp_name)
    json_tmp = Path(json_tmp_name)
    total = len(messages)
    if progress:
        progress(stage="serializing", current=0, total=total)
    json_tmp.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    con = sqlite3.connect(db_tmp)
    try:
        con.executescript(
            """
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY, ts INTEGER, dt TEXT, date TEXT,
                hour INTEGER, weekday INTEGER, sender TEXT, type TEXT, content TEXT
            );
            CREATE INDEX idx_date ON messages(date);
            CREATE INDEX idx_sender ON messages(sender);
            CREATE INDEX idx_ts ON messages(ts);
            CREATE INDEX idx_messages_sender_date ON messages(sender,date);
            CREATE INDEX idx_messages_type_date ON messages(type,date);
            CREATE TABLE daily_summary(
                date TEXT PRIMARY KEY, me_count INTEGER, her_count INTEGER,
                narrative TEXT, topics TEXT, mood_me TEXT, mood_her TEXT,
                events TEXT, her_reflection TEXT, quotes TEXT, tags TEXT,
                model TEXT, built_at INTEGER
            );
            CREATE TABLE monthly_summary(
                month TEXT PRIMARY KEY, narrative TEXT, mood_trend TEXT, growth TEXT,
                topics TEXT, key_days TEXT, model TEXT, built_at INTEGER
            );
            CREATE TABLE yearly_summary(
                id INTEGER PRIMARY KEY, narrative TEXT, themes TEXT, month_index TEXT,
                model TEXT, built_at INTEGER
            );
            """
        )
        rows = []
        for i, message in enumerate(messages):
            ts = int(message.get("ts") or 0)
            date = datetime.fromtimestamp(ts, CN) if ts else None
            rows.append((
                i, ts, date.strftime("%Y-%m-%d %H:%M:%S") if date else "",
                date.strftime("%Y-%m-%d") if date else "", date.hour if date else None,
                date.weekday() if date else None, message.get("sender", "system"),
                message.get("type", "其他"), message.get("content", ""),
            ))
        batch_size = 5000
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            con.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", batch)
            if progress:
                progress(
                    stage="writing", current=start + len(batch), total=total,
                )
        con.commit()
    finally:
        con.close()
    db_backup = db_path.with_suffix(".db.swap-backup")
    json_backup = json_path.with_suffix(".json.swap-backup")
    old_db = db_path.exists()
    old_json = json_path.exists()
    if progress:
        progress(stage="finalizing", current=total, total=total)
    try:
        if old_db:
            shutil.copy2(db_path, db_backup)
        if old_json:
            shutil.copy2(json_path, json_backup)
        os.replace(db_tmp, db_path)
        os.replace(json_tmp, json_path)
    except Exception:
        if old_db and db_backup.exists():
            os.replace(db_backup, db_path)
        elif not old_db and db_path.exists():
            db_path.unlink()
        if old_json and json_backup.exists():
            os.replace(json_backup, json_path)
        elif not old_json and json_path.exists():
            json_path.unlink()
        raise
    finally:
        for path in (db_tmp, json_tmp, db_backup, json_backup):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    dates = [datetime.fromtimestamp(int(m["ts"]), CN).strftime("%Y-%m-%d")
             for m in messages if m.get("ts")]
    return {
        "path": str(db_path), "message_count": len(messages),
        "date_from": min(dates) if dates else "", "date_to": max(dates) if dates else "",
    }


def choose_json_file():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askopenfilename(
            title="选择微信聊天记录 JSON",
            filetypes=[("聊天记录 JSON", "*.json"), ("所有文件", "*.*")],
        )
    finally:
        root.destroy()
