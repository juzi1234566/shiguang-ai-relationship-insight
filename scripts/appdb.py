"""拾光应用状态库：持久化普通 AI 对话，不与微信原始消息混表。"""
import json
import os
import sqlite3
import threading
import time
import uuid

ROOT = os.environ.get("SHIGUANG_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "clean", "app.db")
_READY = False
_INIT_LOCK = threading.Lock()


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    global _READY
    if _READY:
        return
    with _INIT_LOCK:
        if _READY:
            return
        con = connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
            CREATE TABLE IF NOT EXISTS ai_conversations(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                model TEXT,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation
                ON ai_messages(conversation_id,id);
            CREATE INDEX IF NOT EXISTS idx_ai_conversations_updated
                ON ai_conversations(updated_at DESC);
            CREATE TABLE IF NOT EXISTS app_settings(
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS privacy_exclusions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_from TEXT NOT NULL DEFAULT '',
                date_to TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS insight_feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL,
                correction TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_pins(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL DEFAULT '',
                message_id INTEGER,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS future_letters(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                unlock_date TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                opened_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS report_jobs(
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                angle TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'deep',
                status TEXT NOT NULL,
                path TEXT,
                error TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_report_jobs_updated ON report_jobs(updated_at DESC);
            CREATE TABLE IF NOT EXISTS saved_searches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                query_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS couple_prompts(
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer_me TEXT NOT NULL DEFAULT '',
                answer_partner TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """)
            cols = {r[1] for r in con.execute("PRAGMA table_info(ai_conversations)")}
            if "summary" not in cols:
                con.execute("ALTER TABLE ai_conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            if "summary_updated_at" not in cols:
                con.execute("ALTER TABLE ai_conversations ADD COLUMN summary_updated_at INTEGER NOT NULL DEFAULT 0")
            con.execute("INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                        (1, "base conversations", int(time.time())))
            con.execute("INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                        (2, "settings privacy memories and jobs", int(time.time())))
            con.execute("INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                        (3, "couple prompts and conversation summaries", int(time.time())))
            con.commit()
            _READY = True
        finally:
            con.close()


def create_conversation(title="新对话"):
    init()
    cid = uuid.uuid4().hex
    now = int(time.time())
    con = connect()
    try:
        con.execute("INSERT INTO ai_conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (cid, title, now, now))
        con.commit()
    finally:
        con.close()
    return cid


def exists(conversation_id):
    if not conversation_id:
        return False
    init()
    con = connect()
    try:
        return con.execute("SELECT 1 FROM ai_conversations WHERE id=?", (conversation_id,)).fetchone() is not None
    finally:
        con.close()


def add_message(conversation_id, role, content, model="", meta=None):
    init()
    now = int(time.time())
    content = str(content or "")
    con = connect()
    try:
        before = con.execute(
            "SELECT count(*) FROM ai_messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        con.execute(
            "INSERT INTO ai_messages(conversation_id,role,content,model,meta_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (conversation_id, role, content, model or "",
             json.dumps(meta or {}, ensure_ascii=False), now))
        if role == "user" and before == 0:
            title = " ".join(content.strip().split())[:32] or "新对话"
            con.execute("UPDATE ai_conversations SET title=?,updated_at=? WHERE id=?",
                        (title, now, conversation_id))
        else:
            con.execute("UPDATE ai_conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        con.commit()
    finally:
        con.close()


def list_conversations(limit=100):
    init()
    con = connect()
    try:
        rows = con.execute(
            "SELECT c.id,c.title,c.created_at,c.updated_at,count(m.id) message_count "
            "FROM ai_conversations c LEFT JOIN ai_messages m ON m.conversation_id=c.id "
            "GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_conversation(conversation_id, limit=200):
    init()
    con = connect()
    try:
        conv = con.execute("SELECT * FROM ai_conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conv:
            return None
        rows = con.execute(
            "SELECT id,role,content,model,meta_json,created_at FROM "
            "(SELECT * FROM ai_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id", (conversation_id, min(max(limit, 1), 1000))).fetchall()
        messages = []
        for r in rows:
            item = dict(r)
            try:
                item["meta"] = json.loads(item.pop("meta_json") or "{}")
            except json.JSONDecodeError:
                item["meta"] = {}
            messages.append(item)
        return {"conversation": dict(conv), "messages": messages}
    finally:
        con.close()


def recent_history(conversation_id, limit=12):
    data = get_conversation(conversation_id, limit=limit)
    return [{"role": m["role"], "content": m["content"]} for m in (data or {}).get("messages", [])]


def delete_conversation(conversation_id):
    init()
    con = connect()
    try:
        cur = con.execute("DELETE FROM ai_conversations WHERE id=?", (conversation_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


DEFAULT_SETTINGS = {
    "user_name": "我",
    "partner_name": "对方",
    "relationship_type": "friend",
    "relationship_note": "",
    "relationship_start": "",
    "anniversary_name": "重要纪念日",
    "privacy_notice": True,
    "care_tone": "温柔但不下诊断",
}


def get_settings():
    init()
    values = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(ROOT, "profile.json"), encoding="utf-8") as f:
            profile = json.load(f)
        values["partner_name"] = profile.get("display_name") or values["partner_name"]
        values["relationship_type"] = profile.get("relation_type") or values["relationship_type"]
        values["relationship_note"] = profile.get("relation_note") or ""
    except (OSError, ValueError):
        pass
    con = connect()
    try:
        for row in con.execute("SELECT key,value_json FROM app_settings"):
            try:
                values[row["key"]] = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                pass
        return values
    finally:
        con.close()


def update_settings(values):
    init()
    allowed = set(DEFAULT_SETTINGS) | {"ai_base_url", "ai_model"}
    now = int(time.time())
    con = connect()
    try:
        for key, value in (values or {}).items():
            if key not in allowed:
                continue
            con.execute(
                "INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now))
        con.commit()
    finally:
        con.close()
    return get_settings()


def list_exclusions(active_only=False):
    init()
    con = connect()
    try:
        sql = "SELECT * FROM privacy_exclusions"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY created_at DESC,id DESC"
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def add_exclusion(date_from="", date_to="", keyword="", reason=""):
    init()
    if not (date_from or date_to or keyword):
        raise ValueError("日期范围和关键词至少填写一项")
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO privacy_exclusions(date_from,date_to,keyword,reason,created_at) VALUES(?,?,?,?,?)",
            (date_from or "", date_to or date_from or "", keyword or "", reason or "", int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def delete_exclusion(item_id):
    init()
    con = connect()
    try:
        cur = con.execute("DELETE FROM privacy_exclusions WHERE id=?", (int(item_id),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_feedback(source_type, source_id="", verdict="correct", correction=""):
    init()
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO insight_feedback(source_type,source_id,verdict,correction,created_at) VALUES(?,?,?,?,?)",
            (source_type or "general", source_id or "", verdict or "correct", correction or "", int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_feedback(limit=100):
    init()
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM insight_feedback ORDER BY created_at DESC,id DESC LIMIT ?", (min(max(limit, 1), 500),))]
    finally:
        con.close()


def pin_memory(content, conversation_id="", message_id=None):
    init()
    content = str(content or "").strip()
    if not content:
        raise ValueError("收藏内容不能为空")
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO memory_pins(conversation_id,message_id,content,created_at) VALUES(?,?,?,?)",
            (conversation_id or "", message_id, content[:4000], int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_pins(limit=100):
    init()
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM memory_pins ORDER BY created_at DESC,id DESC LIMIT ?", (min(max(limit, 1), 500),))]
    finally:
        con.close()


def delete_pin(item_id):
    init()
    con = connect()
    try:
        cur = con.execute("DELETE FROM memory_pins WHERE id=?", (int(item_id),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def create_letter(title, content, unlock_date):
    init()
    if not str(content or "").strip() or not unlock_date:
        raise ValueError("信件内容和解锁日期不能为空")
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO future_letters(title,content,unlock_date,created_at) VALUES(?,?,?,?)",
            (str(title or "给未来的我们")[:80], str(content)[:20000], unlock_date, int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_letters(today=None):
    init()
    today = today or time.strftime("%Y-%m-%d")
    con = connect()
    try:
        rows = []
        for row in con.execute("SELECT * FROM future_letters ORDER BY unlock_date,created_at"):
            item = dict(row)
            item["locked"] = item["unlock_date"] > today
            if item["locked"]:
                item["content"] = ""
            rows.append(item)
        return rows
    finally:
        con.close()


def delete_letter(item_id):
    init()
    con = connect()
    try:
        cur = con.execute("DELETE FROM future_letters WHERE id=?", (int(item_id),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def save_couple_answer(prompt_id, question, side, answer):
    init()
    if side not in ("me", "partner"):
        raise ValueError("side 必须是 me 或 partner")
    now = int(time.time())
    col = "answer_me" if side == "me" else "answer_partner"
    con = connect()
    try:
        con.execute(
            "INSERT INTO couple_prompts(id,question,created_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET question=excluded.question,updated_at=excluded.updated_at",
            (str(prompt_id)[:80], str(question)[:500], now, now))
        con.execute(f"UPDATE couple_prompts SET {col}=?,updated_at=? WHERE id=?",
                    (str(answer or "")[:4000], now, str(prompt_id)[:80]))
        con.commit()
        row = con.execute("SELECT * FROM couple_prompts WHERE id=?", (str(prompt_id)[:80],)).fetchone()
        return dict(row)
    finally:
        con.close()


def list_couple_prompts(limit=50):
    init()
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM couple_prompts ORDER BY updated_at DESC LIMIT ?", (min(max(limit, 1), 200),))]
    finally:
        con.close()


def save_search(name, query):
    init()
    con = connect()
    try:
        cur = con.execute("INSERT INTO saved_searches(name,query_json,created_at) VALUES(?,?,?)",
                          (str(name or "保存的搜索")[:80], json.dumps(query or {}, ensure_ascii=False), int(time.time())))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_saved_searches():
    init()
    con = connect()
    try:
        out = []
        for row in con.execute("SELECT * FROM saved_searches ORDER BY created_at DESC,id DESC"):
            item = dict(row)
            try:
                item["query"] = json.loads(item.pop("query_json") or "{}")
            except json.JSONDecodeError:
                item["query"] = {}
            out.append(item)
        return out
    finally:
        con.close()


def delete_saved_search(item_id):
    init()
    con = connect()
    try:
        cur = con.execute("DELETE FROM saved_searches WHERE id=?", (int(item_id),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_conversation_summary(conversation_id, summary):
    init()
    con = connect()
    try:
        con.execute("UPDATE ai_conversations SET summary=?,summary_updated_at=? WHERE id=?",
                    (str(summary or "")[:12000], int(time.time()), conversation_id))
        con.commit()
    finally:
        con.close()


def conversation_context(conversation_id, history_limit=24):
    data = get_conversation(conversation_id, limit=history_limit)
    if not data:
        return {"summary": "", "history": []}
    return {
        "summary": data["conversation"].get("summary", ""),
        "history": [{"role": m["role"], "content": m["content"]} for m in data["messages"]],
    }


def create_report_job(job_id, topic, angle="", mode="deep"):
    init()
    now = int(time.time())
    con = connect()
    try:
        con.execute(
            "INSERT INTO report_jobs(id,topic,angle,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, topic, angle or "", mode, "排队中", now, now))
        con.commit()
    finally:
        con.close()


def update_report_job(job_id, **changes):
    init()
    allowed = {"status", "path", "error", "progress"}
    values = {k: v for k, v in changes.items() if k in allowed}
    if not values:
        return
    values["updated_at"] = int(time.time())
    con = connect()
    try:
        keys = list(values)
        con.execute("UPDATE report_jobs SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
                    [values[k] for k in keys] + [job_id])
        con.commit()
    finally:
        con.close()


def list_report_jobs(limit=100):
    init()
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM report_jobs ORDER BY updated_at DESC LIMIT ?", (min(max(limit, 1), 500),))]
    finally:
        con.close()


def get_report_job(job_id):
    init()
    con = connect()
    try:
        row = con.execute("SELECT * FROM report_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def recover_report_jobs():
    init()
    con = connect()
    try:
        rows = con.execute("SELECT id FROM report_jobs WHERE status IN ('排队中','生成中')").fetchall()
        con.execute("UPDATE report_jobs SET status='排队中',progress=0,updated_at=? "
                    "WHERE status IN ('排队中','生成中')", (int(time.time()),))
        con.commit()
        return [r[0] for r in rows]
    finally:
        con.close()
