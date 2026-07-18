"""数据层：对 chat.db 的只读访问、按天取原文、日内分块、摘要金字塔表结构。

被 build_pyramid / deep_research / care / insight / ask 复用。
"""
import json
import os
import re
import sqlite3
import time

ROOT = os.environ.get("SHIGUANG_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "clean", "chat.db")

SENDER_LABEL = {"me": "我", "her": "她", "system": "系统"}
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|create|replace|pragma|vacuum|reindex)\b",
    re.I,
)


def connect_ro():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ensure_search_indexes():
    """建立聊天浏览所需的结构化索引与中文友好的 trigram 全文索引。"""
    con = sqlite3.connect(DB)
    try:
        con.executescript("""
            CREATE INDEX IF NOT EXISTS idx_messages_sender_date
                ON messages(sender, date);
            CREATE INDEX IF NOT EXISTS idx_messages_type_date
                ON messages(type, date);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, sender UNINDEXED, date UNINDEXED, type UNINDEXED,
                content='messages', content_rowid='id', tokenize='trigram'
            );
            CREATE TABLE IF NOT EXISTS search_index_meta(
                name TEXT PRIMARY KEY, value TEXT
            );
            CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
              INSERT INTO messages_fts(rowid,content,sender,date,type)
              VALUES (new.id,new.content,new.sender,new.date,new.type);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
              INSERT INTO messages_fts(messages_fts,rowid,content,sender,date,type)
              VALUES ('delete',old.id,old.content,old.sender,old.date,old.type);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
              INSERT INTO messages_fts(messages_fts,rowid,content,sender,date,type)
              VALUES ('delete',old.id,old.content,old.sender,old.date,old.type);
              INSERT INTO messages_fts(rowid,content,sender,date,type)
              VALUES (new.id,new.content,new.sender,new.date,new.type);
            END;
        """)
        message_count = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        built = con.execute(
            "SELECT value FROM search_index_meta WHERE name='messages_fts_built'").fetchone()
        if not built:
            con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            con.execute("INSERT OR REPLACE INTO search_index_meta(name,value) VALUES(?,?)",
                        ("messages_fts_built", str(int(time.time()))))
        con.commit()
        return {"messages": message_count, "fts": True}
    finally:
        con.close()


def _filters(sender="", date_from="", date_to="", msg_type="", alias="m"):
    clauses = [f"{alias}.sender!='system'"]
    params = []
    if sender in ("me", "her"):
        clauses.append(f"{alias}.sender=?")
        params.append(sender)
    if date_from:
        clauses.append(f"{alias}.date>=?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{alias}.date<=?")
        params.append(date_to)
    if msg_type:
        clauses.append(f"{alias}.type=?")
        params.append(msg_type)
    return clauses, params


def search_messages(query="", mode="keyword", sender="", date_from="", date_to="",
                    msg_type="", page=1, page_size=50, sort="recent"):
    """搜索或分页浏览原始消息；返回实际使用的搜索引擎名称。"""
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 50), 10), 100)
    offset = (page - 1) * page_size
    query = (query or "").strip()
    clauses, params = _filters(sender, date_from, date_to, msg_type)
    con = connect_ro()
    try:
        use_fts = mode == "keyword" and len(query) >= 3
        if use_fts:
            phrase = '"' + query.replace('"', '""') + '"'
            where = ["messages_fts MATCH ?"] + clauses
            all_params = [phrase] + params
            from_sql = "messages_fts JOIN messages m ON m.id=messages_fts.rowid"
            order = "bm25(messages_fts), m.ts DESC" if sort == "relevance" else "m.ts DESC"
            engine = "FTS5 trigram 全文索引"
        else:
            if query:
                if mode == "exact":
                    clauses.append("m.content=?")
                    params.append(query)
                else:
                    clauses.append("m.content LIKE ?")
                    params.append(f"%{query}%")
            where, all_params = clauses, params
            from_sql = "messages m"
            order = "m.ts DESC"
            engine = ("精确整句匹配" if mode == "exact" and query else
                      "短词/子串扫描" if query else "B-tree 条件索引")
        where_sql = " AND ".join(where)
        total = con.execute(
            f"SELECT count(*) FROM {from_sql} WHERE {where_sql}", all_params).fetchone()[0]
        rows = con.execute(
            f"SELECT m.id,m.dt,m.date,m.sender,m.type,m.content "
            f"FROM {from_sql} WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            all_params + [page_size, offset]).fetchall()
        return {
            "items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "engine": engine,
        }
    finally:
        con.close()


def search_summaries(query="", date_from="", date_to="", page=1, page_size=30):
    """按叙事、情绪、事件、标签检索每日摘要。"""
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 30), 10), 100)
    offset = (page - 1) * page_size
    query = (query or "").strip()
    clauses, params = [], []
    if query:
        like = f"%{query}%"
        clauses.append("(narrative LIKE ? OR topics LIKE ? OR mood_me LIKE ? OR mood_her LIKE ? "
                       "OR events LIKE ? OR her_reflection LIKE ? OR tags LIKE ?)")
        params.extend([like] * 7)
    if date_from:
        clauses.append("date>=?")
        params.append(date_from)
    if date_to:
        clauses.append("date<=?")
        params.append(date_to)
    where = " AND ".join(clauses) if clauses else "1=1"
    con = connect_ro()
    try:
        total = con.execute(f"SELECT count(*) FROM daily_summary WHERE {where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT date,narrative,mood_me,mood_her,events,tags FROM daily_summary "
            f"WHERE {where} ORDER BY date DESC LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "page": page,
                "page_size": page_size, "engine": "每日摘要/事件索引"}
    finally:
        con.close()


def search_meta():
    con = connect_ro()
    try:
        types = [r[0] for r in con.execute(
            "SELECT type FROM messages WHERE sender!='system' GROUP BY type ORDER BY count(*) DESC")]
        span = con.execute("SELECT min(date),max(date) FROM messages WHERE sender!='system'").fetchone()
        return {"types": types, "start": span[0], "end": span[1]}
    finally:
        con.close()


def get_message_context(message_id, radius=8):
    """返回某条命中消息的上下文与当天摘要，供全局证据抽屉使用。"""
    radius = min(max(int(radius or 8), 2), 30)
    con = connect_ro()
    try:
        target = con.execute(
            "SELECT id,ts,dt,date,sender,type,content FROM messages WHERE id=?",
            (int(message_id),)).fetchone()
        if not target:
            return {"error": "消息不存在"}
        before = con.execute(
            "SELECT id,dt,date,sender,type,content FROM messages "
            "WHERE sender!='system' AND (ts<? OR (ts=? AND id<?)) "
            "ORDER BY ts DESC,id DESC LIMIT ?",
            (target["ts"], target["ts"], target["id"], radius)).fetchall()
        after = con.execute(
            "SELECT id,dt,date,sender,type,content FROM messages "
            "WHERE sender!='system' AND (ts>? OR (ts=? AND id>?)) "
            "ORDER BY ts,id LIMIT ?",
            (target["ts"], target["ts"], target["id"], radius)).fetchall()
        summary = con.execute(
            "SELECT date,narrative,mood_me,mood_her,events,tags,quotes "
            "FROM daily_summary WHERE date=?", (target["date"],)).fetchone()
        items = [dict(r) for r in reversed(before)] + [dict(target)] + [dict(r) for r in after]
        return {"target_id": int(message_id), "date": target["date"], "items": items,
                "summary": dict(summary) if summary else None}
    finally:
        con.close()


def get_day_detail(date, limit=400):
    """一天的摘要、统计和原始消息；默认最多 400 条，避免一次压垮页面。"""
    limit = min(max(int(limit or 400), 50), 1200)
    con = connect_ro()
    try:
        rows = con.execute(
            "SELECT id,dt,date,sender,type,content FROM messages "
            "WHERE date=? AND sender!='system' ORDER BY ts,id LIMIT ?",
            (date, limit + 1)).fetchall()
        summary = con.execute("SELECT * FROM daily_summary WHERE date=?", (date,)).fetchone()
        counts = con.execute(
            "SELECT sender,count(*) n FROM messages WHERE date=? AND sender!='system' GROUP BY sender",
            (date,)).fetchall()
        return {"date": date, "items": [dict(r) for r in rows[:limit]],
                "truncated": len(rows) > limit,
                "counts": {r["sender"]: r["n"] for r in counts},
                "summary": dict(summary) if summary else None}
    finally:
        con.close()


def timeline(month="", limit=120):
    """数据库驱动的关系时间线；按月聚合并保留可下钻日期。"""
    con = connect_ro()
    try:
        where, args = "", []
        if month:
            where, args = "WHERE d.date LIKE ?", [month + "%"]
        rows = con.execute(
            "SELECT d.date,d.narrative,d.mood_me,d.mood_her,d.events,d.tags,d.quotes,"
            "d.me_count,d.her_count FROM daily_summary d " + where +
            " ORDER BY d.date DESC LIMIT ?", args + [min(max(int(limit), 20), 500)]).fetchall()
        months = con.execute(
            "SELECT substr(date,1,7) month,count(*) days,sum(me_count+her_count) messages "
            "FROM daily_summary GROUP BY substr(date,1,7) ORDER BY month DESC").fetchall()
        chapters = []
        for m in months:
            ms = con.execute("SELECT narrative,mood_trend,growth,topics,key_days "
                             "FROM monthly_summary WHERE month=?", (m["month"],)).fetchone()
            item = dict(m)
            if ms:
                item.update(dict(ms))
            chapters.append(item)
        return {"items": [dict(r) for r in rows], "chapters": chapters}
    finally:
        con.close()


def today_hub():
    """首页所需的最新一天、近日趋势、同月同日回忆和待续线索。"""
    con = connect_ro()
    try:
        latest = con.execute("SELECT max(date) FROM messages WHERE sender!='system'").fetchone()[0]
        if not latest:
            return {"latest": "", "recent": [], "on_this_day": []}
        # 原始消息一导入就能显示真实条数；AI 摘要尚未生成时只把摘要字段留空。
        recent = con.execute(
            "SELECT m.date,COALESCE(d.narrative,'') narrative,"
            "COALESCE(d.mood_me,'') mood_me,COALESCE(d.mood_her,'') mood_her,"
            "COALESCE(d.events,'') events,COALESCE(d.tags,'') tags,"
            "sum(CASE WHEN m.sender='me' THEN 1 ELSE 0 END) me_count,"
            "sum(CASE WHEN m.sender='her' THEN 1 ELSE 0 END) her_count "
            "FROM messages m LEFT JOIN daily_summary d ON d.date=m.date "
            "WHERE m.sender!='system' GROUP BY m.date ORDER BY m.date DESC LIMIT 7"
        ).fetchall()
        mmdd = latest[5:]
        on_day = con.execute(
            "SELECT date,narrative,events,tags FROM daily_summary "
            "WHERE substr(date,6,5)=? AND date<>? ORDER BY date DESC LIMIT 6",
            (mmdd, latest)).fetchall()
        last_messages = con.execute(
            "SELECT id,dt,date,sender,type,content FROM messages "
            "WHERE sender!='system' ORDER BY ts DESC,id DESC LIMIT 8").fetchall()
        return {"latest": latest, "recent": [dict(r) for r in recent],
                "on_this_day": [dict(r) for r in on_day],
                "last_messages": [dict(r) for r in reversed(last_messages)]}
    finally:
        con.close()


def place_memories(limit=16):
    """从摘要和事件里提取经常出现的地点词，形成可下钻的记忆地点榜。"""
    suffix = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{2,12}(?:大学|学院|医院|公园|餐厅|饭店|酒店|机场|车站|商场|广场|图书馆|咖啡店|办公室|宿舍))")
    con = connect_ro()
    try:
        rows = con.execute("SELECT date,narrative,events FROM daily_summary ORDER BY date").fetchall()
        places = {}
        for r in rows:
            text = (r["narrative"] or "") + " " + (r["events"] or "")
            for name in suffix.findall(text):
                x = places.setdefault(name, {"name": name, "count": 0, "dates": [], "snippet": ""})
                x["count"] += 1
                if len(x["dates"]) < 8:
                    x["dates"].append(r["date"])
                if not x["snippet"]:
                    x["snippet"] = (r["narrative"] or "")[:100]
        return sorted(places.values(), key=lambda x: (-x["count"], x["name"]))[:limit]
    finally:
        con.close()


def mood_weather(days=30):
    """把每日摘要中的情绪与标签转换为轻量天气，结果仍可回到原始证据。"""
    con = connect_ro()
    try:
        rows = con.execute(
            "SELECT date,mood_me,mood_her,tags,me_count,her_count FROM daily_summary "
            "ORDER BY date DESC LIMIT ?", (min(max(int(days), 7), 120),)).fetchall()
        out = []
        for r in reversed(rows):
            t = " ".join(str(r[k] or "") for k in ("mood_me", "mood_her", "tags"))
            if re.search(r"争吵|生气|难过|焦虑|崩溃|低落|委屈", t):
                weather, score = "阵雨", 35
            elif re.search(r"甜蜜|开心|幸福|温暖|支持|亲密", t):
                weather, score = "晴", 88
            elif re.search(r"疲惫|忙碌|平淡|稳定", t):
                weather, score = "多云", 62
            else:
                weather, score = "微风", 70
            out.append({**dict(r), "weather": weather, "score": score})
        return out
    finally:
        con.close()


def meaningful_threads(days=21, limit=3):
    """找出近期最值得继续回应的伴侣原话，而不是机械展示最后几条消息。

    只做本地启发式排序：问题、情绪表达、共同计划、长消息，以及尚未得到
    实质回复的内容优先；每个日期最多一条，并返回原始消息 ID 便于下钻。
    """
    days = min(max(int(days or 21), 7), 90)
    limit = min(max(int(limit or 3), 1), 8)
    con = connect_ro()
    try:
        latest = con.execute(
            "SELECT max(date) FROM messages WHERE sender!='system'").fetchone()[0]
        if not latest:
            return []
        start = con.execute("SELECT date(?, ?)", (latest, f"-{days} day")).fetchone()[0]
        candidates = con.execute(
            "SELECT id,ts,dt,date,content FROM messages "
            "WHERE sender='her' AND type='text' AND date>=? "
            "AND length(trim(content)) BETWEEN 12 AND 500 "
            "AND content NOT LIKE 'http%' AND content NOT GLOB '[[]*[]]' "
            "ORDER BY ts DESC,id DESC LIMIT 360", (start,)).fetchall()
        out = []
        question_re = re.compile(
            r"[?？]|(?:^|[，。！\n])(?:为什么|怎么|怎么办|是不是|要不要|能不能)|可以吗|好吗|吗$")
        emotion_re = re.compile(r"难过|委屈|焦虑|害怕|担心|累|疲惫|想你|想哭|不开心|不舒服|谢谢|对不起")
        plan_re = re.compile(r"希望|期待|想要|计划|以后|下次|一起|旅行|见面|未来|毕业")
        for row in candidates:
            reply = con.execute(
                "SELECT id,ts,content FROM messages WHERE sender='me' AND ts>? "
                "ORDER BY ts,id LIMIT 1", (row["ts"],)).fetchone()
            content = (row["content"] or "").strip()
            is_question = bool(question_re.search(content))
            is_emotion = bool(emotion_re.search(content))
            is_plan = bool(plan_re.search(content))
            reply_gap = (reply["ts"] - row["ts"]) if reply else None
            reply_text = (reply["content"] or "").strip() if reply else ""
            unanswered = not reply or reply_gap > 12 * 3600
            light_reply = bool(reply and reply_gap <= 12 * 3600 and len(reply_text) < 6)
            score = min(len(content) / 45, 4)
            score += 4 if is_question else 0
            score += 5 if is_emotion else 0
            score += 3 if is_plan else 0
            score += 5 if unanswered else (2 if light_reply else 0)
            if score < 5:
                continue
            reasons = []
            if is_emotion:
                reasons.append("她在表达情绪")
            if is_question:
                reasons.append("她认真问了一个问题")
            if is_plan:
                reasons.append("里面有共同计划")
            if unanswered:
                reasons.append("之后 12 小时内没有你的回复")
            elif light_reply:
                reasons.append("当时只得到一句很短的回应")
            if not reasons:
                reasons.append("这是一段信息量较高的表达")
            out.append({"id": row["id"], "date": row["date"], "dt": row["dt"],
                        "content": content, "reason": "；".join(reasons),
                        "score": round(score, 1)})
        # 避免三条都来自同一天，也避免相似文案重复。
        chosen, seen_dates, seen_prefix = [], set(), set()
        for item in sorted(out, key=lambda x: x["score"], reverse=True):
            prefix = re.sub(r"\s+", "", item["content"])[:12]
            if item["date"] in seen_dates or prefix in seen_prefix:
                continue
            chosen.append(item); seen_dates.add(item["date"]); seen_prefix.add(prefix)
            if len(chosen) >= limit:
                break
        return chosen
    finally:
        con.close()


def connect_rw():
    return sqlite3.connect(DB)


# ---------- 摘要金字塔表结构 ----------

def ensure_schema(con):
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_summary(
        date TEXT PRIMARY KEY,
        me_count INTEGER, her_count INTEGER,
        narrative TEXT,          -- 详细叙述(3-6句)
        topics TEXT,             -- JSON list
        mood_me TEXT, mood_her TEXT,
        events TEXT,             -- JSON list
        her_reflection TEXT,     -- 她当天的深度复盘(可含原话)，无则空
        quotes TEXT,             -- JSON list [{who,text}]
        tags TEXT,               -- JSON list
        model TEXT, built_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS monthly_summary(
        month TEXT PRIMARY KEY,  -- 'YYYY-MM'
        narrative TEXT, mood_trend TEXT, growth TEXT,
        topics TEXT, key_days TEXT,   -- JSON
        model TEXT, built_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS yearly_summary(
        id INTEGER PRIMARY KEY CHECK (id=1),
        narrative TEXT, themes TEXT, month_index TEXT,  -- JSON
        model TEXT, built_at INTEGER
    )""")
    con.commit()


# ---------- 读取 ----------

def list_days():
    """所有有真人消息的日期，升序。"""
    con = connect_ro()
    rows = con.execute(
        "SELECT DISTINCT date FROM messages "
        "WHERE date!='' AND sender!='system' ORDER BY date").fetchall()
    con.close()
    return [r["date"] for r in rows]


def day_counts(date):
    con = connect_ro()
    r = con.execute(
        "SELECT sender, count(*) n FROM messages WHERE date=? GROUP BY sender",
        (date,)).fetchall()
    con.close()
    d = {row["sender"]: row["n"] for row in r}
    return d.get("me", 0), d.get("her", 0)


def serialize_day(date, max_chars=0):
    """把一天的对话序列化成 'HH:MM 我/她: 内容'，非文本折叠成占位符。
    返回 (文本, 是否截断)。max_chars>0 时超长截断。"""
    con = connect_ro()
    rows = con.execute(
        "SELECT dt, sender, type, content FROM messages "
        "WHERE date=? AND sender!='system' ORDER BY ts, id", (date,)).fetchall()
    con.close()
    lines = []
    for r in rows:
        who = SENDER_LABEL.get(r["sender"], r["sender"])
        hm = (r["dt"] or "")[11:16]
        c = r["content"] or ""
        if r["type"] != "text" and not c:
            c = f"[{r['type']}]"
        lines.append(f"{hm} {who}: {c}")
    text = "\n".join(lines)
    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, truncated


def chunk_day(date, chunk_chars):
    """把一天切成若干块(每块 ~chunk_chars 字)，用于日内 map-reduce。返回块文本列表。"""
    con = connect_ro()
    rows = con.execute(
        "SELECT dt, sender, type, content FROM messages "
        "WHERE date=? AND sender!='system' ORDER BY ts, id", (date,)).fetchall()
    con.close()
    chunks, cur, size = [], [], 0
    for r in rows:
        who = SENDER_LABEL.get(r["sender"], r["sender"])
        hm = (r["dt"] or "")[11:16]
        c = r["content"] or (f"[{r['type']}]" if r["type"] != "text" else "")
        line = f"{hm} {who}: {c}"
        cur.append(line)
        size += len(line) + 1
        if size >= chunk_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ---------- 给大模型当工具用的只读查询 ----------

def run_sql(sql, maxrows=60, cell_limit=240):
    s = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        return json.dumps({"error": "只允许 SELECT 查询"}, ensure_ascii=False)
    if ";" in s:
        return json.dumps({"error": "只允许单条语句"}, ensure_ascii=False)
    if FORBIDDEN.search(s):
        return json.dumps({"error": "查询含禁止的关键字"}, ensure_ascii=False)
    try:
        con = connect_ro()
        cur = con.execute(s)
        rows = cur.fetchmany(maxrows + 1)
        cols = [d[0] for d in cur.description]
        con.close()
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    truncated = len(rows) > maxrows
    out = []
    for r in rows[:maxrows]:
        d = {}
        for c in cols:
            v = r[c]
            if isinstance(v, str) and len(v) > cell_limit:
                v = v[:cell_limit] + "…"
            d[c] = v
        out.append(d)
    payload = {"rows": out, "row_count": len(out)}
    if truncated:
        payload["note"] = f"结果超过 {maxrows} 行已截断，请聚合或加 LIMIT"
    return json.dumps(payload, ensure_ascii=False)


def read_messages(date_from=None, date_to=None, keyword=None, sender=None,
                  min_len=0, limit=80):
    """深挖用：读原文，content 不截断(单条最多 500 字)，用于取金句/证据。"""
    where, args = ["sender!='system'"], []
    if date_from:
        where.append("date>=?"); args.append(date_from)
    if date_to:
        where.append("date<=?"); args.append(date_to)
    if keyword:
        where.append("content LIKE ?"); args.append(f"%{keyword}%")
    if sender:
        where.append("sender=?"); args.append(sender)
    if min_len:
        where.append("length(content)>=?"); args.append(int(min_len))
    sql = ("SELECT dt, sender, type, content FROM messages WHERE "
           + " AND ".join(where) + " ORDER BY ts, id LIMIT ?")
    args.append(int(limit) + 1)
    con = connect_ro()
    rows = con.execute(sql, args).fetchall()
    con.close()
    truncated = len(rows) > limit
    out = []
    for r in rows[:limit]:
        c = r["content"] or ""
        if len(c) > 500:
            c = c[:500] + "…"
        out.append({"dt": r["dt"], "who": SENDER_LABEL.get(r["sender"], r["sender"]),
                    "type": r["type"], "content": c})
    payload = {"rows": out, "row_count": len(out)}
    if truncated:
        payload["note"] = f"超过 {limit} 条已截断，缩小时间范围或加关键词"
    return json.dumps(payload, ensure_ascii=False)
