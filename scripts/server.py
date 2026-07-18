"""拾光 · 本地应用后端（跨平台，零额外依赖，用 Python 标准库）。

把现有大脑包成 HTTP 接口，浏览器界面通过 localhost 调用：
  GET  /                    应用界面
  GET  /api/stats           仪表盘数字
  POST /api/chat            AI 对话 agent（问它/让它做，默认用 Pro）
  GET  /api/reports         报告库（已生成的 PDF）
  POST /api/report          新建深度报告（后台队列，用 pro 求质量）
  GET  /api/jobs            报告任务进度
  GET  /api/download?name=  下载某份报告 PDF
  GET  /api/fun/draw        时光信笺（随机抽一天，零AI）
  GET  /api/fun/badges      成就徽章墙（真实数据现算，零AI）
  GET  /api/fun/quotes      弹幕回忆墙素材（零AI）
  GET  /api/fun/quiz        猜猜看小测验（零AI，正确答案来自真实数据）
  POST /api/fun/whimsical   关系小剧场（唯一调AI的好玩功能，默认用 Pro）

只监听 127.0.0.1，密钥留在后端不进浏览器，SQL 只读。
运行：python scripts/server.py    然后浏览器开 http://127.0.0.1:8756
"""
import json
import os
import queue
import argparse
import datetime
import sqlite3
import threading
import time
import urllib.parse
import webbrowser
import sys
import shutil
import uuid
import secrets
from http.server import BaseHTTPRequestHandler

FROZEN = bool(getattr(sys, "frozen", False))
EXE_ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_ROOT = getattr(sys, "_MEIPASS", EXE_ROOT)
PORTABLE_SEED = os.path.join(ASSET_ROOT, "_portable_seed")


def _portable_app_name():
    try:
        with open(os.path.join(ASSET_ROOT, "_edition.json"), encoding="utf-8") as f:
            edition = str(json.load(f).get("edition") or "")
        return "拾光情侣" if edition == "couple" else "拾光关系"
    except (OSError, ValueError):
        return "拾光"


def _prepare_portable_root():
    """消费者版本把可写数据放到 LocalAppData；开发者正式版仍使用工程旁数据。"""
    override = os.environ.get("SHIGUANG_APP_HOME", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return os.path.abspath(override)
    consumer_build = os.path.exists(os.path.join(ASSET_ROOT, "_edition.json"))
    if not FROZEN or not (os.path.isdir(PORTABLE_SEED) or consumer_build):
        return EXE_ROOT
    base = os.environ.get("SHIGUANG_PORTABLE_HOME")
    if not base:
        if sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", _portable_app_name())
        else:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), _portable_app_name())
    if not os.path.isdir(PORTABLE_SEED):
        os.makedirs(base, exist_ok=True)
        return base
    for current, _, files in os.walk(PORTABLE_SEED):
        rel_dir = os.path.relpath(current, PORTABLE_SEED)
        target_dir = base if rel_dir == "." else os.path.join(base, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            # 赠送版模型配置由 main() 直接从内嵌种子迁移到新用户的 DPAPI，
            # 不把带 key 的临时 JSON 明文落到 LocalAppData。
            if rel_dir.replace("\\", "/") == "config" and name == "ai_config.json":
                continue
            target = os.path.join(target_dir, name)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(current, name), target)
    return base


ROOT = _prepare_portable_root()
UI = os.path.join(ASSET_ROOT, "app", "ui", "index.html")
ONBOARDING_UI = os.path.join(ASSET_ROOT, "app", "ui", "onboarding.html")

import profile_store
import product
import chat_import
import wechat_native
import onboarding_jobs
from runtime_ports import (
    ExclusiveThreadingHTTPServer,
    candidate_ports,
    same_product_running,
)
from runtime_lifecycle import BrowserLifecycle

PRODUCT = product.load(ASSET_ROOT)
PROFILES = profile_store.ProfileStore(ROOT)
ACTIVE_PROFILE = PROFILES.active()
if (ACTIVE_PROFILE and not os.environ.get("SHIGUANG_EDITION") and
        not os.path.exists(os.path.join(ASSET_ROOT, "_edition.json"))):
    PRODUCT = dict(product.EDITIONS.get(ACTIVE_PROFILE.get("edition"), PRODUCT))
PROFILE_ROOT = ACTIVE_PROFILE["data_root"] if ACTIVE_PROFILE else os.path.join(ROOT, "profiles", "_empty")
os.makedirs(PROFILE_ROOT, exist_ok=True)
# 现有能力模块只认识 SHIGUANG_ROOT；这里把它定义为当前关系档案的数据根。
os.environ["SHIGUANG_ROOT"] = PROFILE_ROOT
REPORTS_DIR = os.path.join(PROFILE_ROOT, "reports")
CARE_CACHE_DIR = os.path.join(REPORTS_DIR, "care")

import chatdb
import care
import appdb
import fun
import llm
PORT = 8756
FALLBACK_PORT = 12756
CONSUMER_BUILD = FROZEN and os.path.exists(os.path.join(ASSET_ROOT, "_edition.json"))
APP_LIFECYCLE = None
ONBOARDING_JOBS = onboarding_jobs.JobRegistry()
APP_TOKEN = secrets.token_urlsafe(32)
MAX_REQUEST_BYTES = 2 * 1024 * 1024
FAST = "deepseek-v4-pro"     # 所有 AI 功能统一默认使用 Pro


def _request_is_authorized(host, origin, cookie, token):
    """只接受本机同源浏览器且必须带本次进程生成的会话 Cookie。"""
    try:
        host_parts = urllib.parse.urlsplit("//" + str(host or ""))
        if (host_parts.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            return False
        if origin:
            origin_parts = urllib.parse.urlsplit(str(origin))
            if (origin_parts.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
                return False
            if origin_parts.port != host_parts.port:
                return False
        cookies = {}
        for part in str(cookie or "").split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                cookies[key] = value
        return secrets.compare_digest(cookies.get("shiguang_session", ""), token)
    except (TypeError, ValueError):
        return False


def _parse_content_length(value, max_bytes=MAX_REQUEST_BYTES):
    try:
        size = int(str(value))
    except (TypeError, ValueError):
        raise ValueError("Content-Length 无效")
    if size < 0:
        raise ValueError("Content-Length 不能为负数")
    if size > max_bytes:
        raise ValueError("请求内容过大")
    return size

CHAT_SYS = """你是“拾光”应用里的关系回顾助手，既能普通聊天，也能回顾和分析用户与一个重要联系人的聊天记录。
你有 search_chat（检索原文或摘要）、run_sql（结构化统计）和 start_report（Deep Research PDF）三个工具。

数据库有原始消息表和三层“摘要金字塔”（AI 已为每天/每月/全年建好摘要）。
按问题选择最便宜且可靠的路径：
- 普通寒暄、建议或不涉及历史数据的问题：直接回答，不要查库。
- 模糊记忆、情绪、话题、事件和关系变化：先用 search_chat 的 summaries 定位日期。
- 要找某句话、关键词或真实证据：用 search_chat 的 messages 查原文。
- 数量、频率、排行、时间分布等统计问题：用 run_sql 做只读 SELECT。
- 需要覆盖全时段并生成报告：调用 start_report。
- 摘要定位后若答案需要证据，再查相关日期的原文；不要把摘要中的转述冒充原话。

- messages(id, ts, dt, date, hour, weekday, sender['me'=用户/'her'=对方/'system'], type, content)
- daily_summary(date, me_count, her_count, narrative, topics, mood_me, mood_her, events,
    her_reflection, quotes, tags)  tags 如 日常/甜蜜/想念/复盘/成长/吵架/摩擦/和好/低落/焦虑/里程碑/纪念
- monthly_summary(month'YYYY-MM', narrative, mood_trend, growth, topics, key_days)
- yearly_summary(id=1, narrative, themes, month_index)
SQL 只能 SELECT；JSON 字段(tags/topics)用 LIKE 匹配；统计某类日子例：
  SELECT date,mood_her,narrative FROM daily_summary WHERE tags LIKE '%吵架%'。

用中文，语气自然、尊重关系边界。回答具体历史问题时引用真实数据（带日期、谁说的）。没有调用工具时，不要假装自己查过聊天记录。
当用户想要“一份报告/深度分析/复盘”这类需要通篇研究的东西时，调用 start_report，
然后告诉用户已在后台生成、完成后可在“报告库”查看——不要自己在对话里硬写长报告。"""

CHAT_TOOLS = [
    {"type": "function", "function": {
        "name": "search_chat",
        "description": "检索聊天原文或每日摘要。模糊事件先查 summaries，真实原话查 messages。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词、事件或模糊记忆"},
            "scope": {"type": "string", "enum": ["summaries", "messages"],
                      "description": "summaries=情绪事件摘要；messages=原始消息"},
            "sender": {"type": "string", "enum": ["", "me", "her"]},
            "date_from": {"type": "string", "description": "可选 YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "可选 YYYY-MM-DD"},
            "mode": {"type": "string", "enum": ["keyword", "exact"],
                     "description": "原文检索模式，默认 keyword"}},
            "required": ["query", "scope"]}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "对聊天数据库执行只读 SELECT，回答关于聊天的具体问题。优先查 daily_summary 等摘要表。",
        "parameters": {"type": "object",
                       "properties": {"sql": {"type": "string", "description": "一条 SELECT 语句"}},
                       "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "start_report",
        "description": "当用户想要一份深度分析报告(需通篇研究全库)时调用，后台生成 PDF。",
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "description": "报告主题，也用作标题"},
                                      "angle": {"type": "string", "description": "研究视角/要考察什么"}},
                       "required": ["topic"]}}},
]

# ---------- 报告后台队列 ----------
JOBQ = queue.Queue()


def _enqueue_report(topic, angle, mode="deep"):
    jid = "job_" + uuid.uuid4().hex[:12]
    topic = str(topic or "分析报告")[:100]
    angle = str(angle or "全面深入地考察这个主题")[:1200]
    appdb.create_report_job(jid, topic, angle, mode)
    JOBQ.put(jid)
    return jid


def _worker():
    while True:
        jid = JOBQ.get()
        job = appdb.get_report_job(jid)
        if not job or job["status"] == "已取消":
            continue
        appdb.update_report_job(jid, status="生成中", progress=10, error=None)
        try:
            import deep_research
            appdb.update_report_job(jid, progress=25)
            deep_research.research(job["topic"], job["angle"],
                                   out_prefix=job["topic"], progress=False)
            latest = appdb.get_report_job(jid)
            if latest and latest["status"] == "已取消":
                continue
            appdb.update_report_job(jid, path=job["topic"] + ".pdf",
                                    status="已完成", progress=100)
        except Exception as e:
            appdb.update_report_job(jid, status="失败", progress=0, error=str(e)[:500])


# ---------- 业务 ----------

def _excluded(item):
    """判断一条搜索证据是否落在用户设定的 AI 排除范围内。"""
    date = str(item.get("date") or item.get("dt") or "")[:10]
    text = " ".join(str(item.get(k) or "") for k in
                    ("content", "narrative", "events", "tags", "mood_me", "mood_her"))
    for rule in appdb.list_exclusions(active_only=True):
        start, end = rule.get("date_from", ""), rule.get("date_to", "")
        if start and date and start <= date <= (end or start):
            return True
        if rule.get("keyword") and rule["keyword"] in text:
            return True
    return False


def _privacy_filter(result):
    items = result.get("items", [])
    kept = [x for x in items if not _excluded(x)]
    result["items"] = kept
    result["privacy_hidden"] = len(items) - len(kept)
    return result

def _search_chat_tool(a):
    scope = a.get("scope", "summaries")
    common = {"query": a.get("query", ""), "date_from": a.get("date_from", ""),
              "date_to": a.get("date_to", ""), "page": 1, "page_size": 20}
    if scope == "messages":
        result = chatdb.search_messages(
            mode=a.get("mode", "keyword"), sender=a.get("sender", ""),
            sort="relevance", msg_type="", **common)
    else:
        result = chatdb.search_summaries(**common)
    return json.dumps(_privacy_filter(result), ensure_ascii=False)


def _chat_system(conversation_id=""):
    settings = appdb.get_settings()
    pins = appdb.list_pins(20)
    feedback = [f for f in appdb.list_feedback(30) if f.get("correction")][:10]
    extra = [
        f"用户称呼：{settings.get('user_name','我')}；对方称呼：{settings.get('partner_name','对方')}；"
        f"关系类型：{settings.get('relationship_type','other')}；用户介绍：{settings.get('relationship_note','')}。",
        "引用历史时，尽量用 [YYYY-MM-DD] 标注日期，界面会把日期变成可点击证据。",
    ]
    if pins:
        extra.append("用户置顶的长期记忆：\n" + "\n".join("- " + p["content"][:500] for p in pins))
    if feedback:
        extra.append("用户对既往分析的纠正（以后以此为准）：\n" +
                     "\n".join("- " + f["correction"][:500] for f in feedback))
    if appdb.list_exclusions(active_only=True):
        extra.append("用户设置了 AI 隐私排除范围。search_chat 会自动隐藏；不要绕过或猜测被隐藏内容。")
    if conversation_id:
        ctx = appdb.conversation_context(conversation_id, history_limit=1)
        if ctx.get("summary"):
            extra.append("本次普通对话较早内容的滚动摘要：\n" + ctx["summary"])
    return CHAT_SYS + "\n\n" + "\n".join(extra)


def do_chat(message, history, conversation_id=""):
    trace = []
    def search_tool(args):
        raw = _search_chat_tool(args)
        try:
            data = json.loads(raw)
            evidence = [{"id": x.get("id"), "date": x.get("date"),
                         "snippet": (x.get("content") or x.get("narrative") or "")[:100]}
                        for x in data.get("items", [])[:8]]
            trace.append({"name": "evidence", "items": evidence,
                          "privacy_hidden": data.get("privacy_hidden", 0)})
        except Exception:
            pass
        return raw
    def sql_tool(args):
        if appdb.list_exclusions(active_only=True):
            return json.dumps({"error": "隐私排除已开启，结构化 SQL 统计暂不调用，以免把排除内容计入结果；请用 search_chat。"}, ensure_ascii=False)
        return chatdb.run_sql(args.get("sql", ""))
    tool_impl = {
        "search_chat": search_tool,
        "run_sql": sql_tool,
        "start_report": lambda a: json.dumps(
            {"job_id": _enqueue_report(a.get("topic", "分析报告"), a.get("angle", "")),
             "note": "已加入后台生成队列，完成后在报告库查看"}, ensure_ascii=False),
    }
    def on_tool(name, args):
        trace.append({"name": name, "args": args})
    reply = llm.agent_loop(_chat_system(conversation_id), message, CHAT_TOOLS, tool_impl,
                           model=FAST, max_steps=10, history=history, on_tool=on_tool)
    return reply, trace


def _refresh_conversation_summary(conversation_id):
    """长对话滚动压缩，避免普通聊天只记住最后十几轮。"""
    try:
        data = appdb.get_conversation(conversation_id, limit=80)
        if not data or len(data["messages"]) < 24:
            return
        conv = data["conversation"]
        if conv.get("summary_updated_at", 0) and time.time() - conv["summary_updated_at"] < 900:
            return
        transcript = "\n".join(f"{m['role']}: {m['content'][:1200]}" for m in data["messages"][:-12])
        old = conv.get("summary", "")
        summary = llm.complete(
            "把普通 AI 对话压成可延续的长期记忆。保留用户偏好、已作决定、未完成事项和关键事实；不要虚构。用中文短分条。",
            ("已有摘要：\n" + old + "\n\n新增较早对话：\n" + transcript)[-24000:],
            temperature=0.2, max_tokens=1200, model=FAST)
        if summary:
            appdb.set_conversation_summary(conversation_id, summary)
    except Exception:
        pass


def do_stats():
    con = chatdb.connect_ro()

    def one(sql, *a):
        try:
            return con.execute(sql, a).fetchone()
        except Exception:
            return None
    total = one("SELECT count(*) FROM messages WHERE sender!='system'")
    me = one("SELECT count(*) FROM messages WHERE sender='me'")
    her = one("SELECT count(*) FROM messages WHERE sender='her'")
    span = one("SELECT min(date), max(date) FROM messages WHERE date!=''")
    days = one("SELECT count(*) FROM daily_summary")
    con.close()
    return {"total": total[0] if total else 0,
            "me": me[0] if me else 0, "her": her[0] if her else 0,
            "start": span[0] if span else "", "end": span[1] if span else "",
            "days": days[0] if days else 0}


def _read_local_json(name):
    path = os.path.join(ROOT, "data", "clean", name)
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def do_dashboard():
    """仪表盘所需的聚合数据；只在本地 SQLite/JSON 上计算。"""
    con = chatdb.connect_ro()
    try:
        total, me, her, text_chars = con.execute(
            "SELECT count(*),sum(sender='me'),sum(sender='her'),"
            "sum(CASE WHEN type='text' THEN length(content) ELSE 0 END) "
            "FROM messages WHERE sender!='system'").fetchone()
        start, end = con.execute(
            "SELECT min(date),max(date) FROM messages WHERE sender!='system' AND date!=''").fetchone()
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM messages WHERE sender!='system' AND date!='' ORDER BY date")]
        monthly = [dict(r) for r in con.execute(
            "SELECT substr(date,1,7) month,count(*) total,sum(sender='me') me,sum(sender='her') her "
            "FROM messages WHERE sender!='system' AND date!='' GROUP BY month ORDER BY month")]
        hourly_raw = {r[0]: r[1] for r in con.execute(
            "SELECT hour,count(*) FROM messages WHERE sender!='system' AND hour IS NOT NULL GROUP BY hour")}
        weekday_raw = {r[0]: r[1] for r in con.execute(
            "SELECT weekday,count(*) FROM messages WHERE sender!='system' AND weekday IS NOT NULL GROUP BY weekday")}
        types = [dict(r) for r in con.execute(
            "SELECT type,count(*) count FROM messages WHERE sender!='system' "
            "GROUP BY type ORDER BY count DESC LIMIT 7")]
        top_days = [dict(r) for r in con.execute(
            "SELECT date,count(*) count FROM messages WHERE sender!='system' AND date!='' "
            "GROUP BY date ORDER BY count DESC LIMIT 6")]
        late_night = con.execute(
            "SELECT count(*) FROM messages WHERE sender!='system' AND hour BETWEEN 0 AND 5").fetchone()[0]
        latest_month = con.execute(
            "SELECT month,narrative,mood_trend,growth,topics FROM monthly_summary "
            "ORDER BY month DESC LIMIT 1").fetchone()
        latest_day = con.execute(
            "SELECT date,narrative,mood_me,mood_her,tags FROM daily_summary "
            "ORDER BY date DESC LIMIT 1").fetchone()
        tag_counts = {}
        for row in con.execute("SELECT tags FROM daily_summary"):
            try:
                tags = json.loads(row[0] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    finally:
        con.close()

    longest = current = 0
    previous = None
    run = 0
    for value in dates:
        day = datetime.date.fromisoformat(value)
        run = run + 1 if previous and day == previous + datetime.timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    current = run if dates else 0
    calls = _read_local_json("gf_calls.json")
    intimacy = _read_local_json("gf_intimacy.json")
    return {
        "total": total or 0, "me": me or 0, "her": her or 0,
        "start": start or "", "end": end or "", "active_days": len(dates),
        "longest_streak": longest, "current_streak": current,
        "avg_per_day": round((total or 0) / max(len(dates), 1)),
        "late_night_ratio": round((late_night or 0) / max(total or 0, 1) * 100, 1),
        "text_chars": text_chars or 0,
        "monthly": monthly,
        "hourly": [{"hour": h, "count": hourly_raw.get(h, 0)} for h in range(24)],
        "weekday": [{"weekday": w, "count": weekday_raw.get(w, 0)} for w in range(7)],
        "types": types, "top_days": top_days,
        "latest_month": dict(latest_month) if latest_month else None,
        "latest_day": dict(latest_day) if latest_day else None,
        "top_tags": [{"tag": k, "count": v} for k, v in
                     sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]],
        "calls": {k: calls.get(k) for k in
                  ("total_calls", "total_hm", "avg_hm", "longest_hm", "longest")},
        "sweet": {"total": intimacy.get("total_sweet_words", 0),
                  "top": (intimacy.get("top") or [])[:6]},
    }


def list_reports():
    out = []
    os.makedirs(REPORTS_DIR, exist_ok=True)
    for f in os.listdir(REPORTS_DIR):
        if f.endswith(".pdf"):
            p = os.path.join(REPORTS_DIR, f)
            out.append({"name": f, "size": os.path.getsize(p) // 1024,
                        "mtime": os.path.getmtime(p)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def care_snapshot(days=14, within=60):
    cached = {}
    for key, filename in (("warning", "情绪预警.md"), ("brief", "本周简报.md")):
        path = os.path.join(CARE_CACHE_DIR, filename)
        if os.path.exists(path):
            try:
                cached[key] = open(path, encoding="utf-8").read()[:20000]
            except OSError:
                cached[key] = ""
    return {
        "warning": care.warning_data(days),
        "radar": care.radar_data(within),
        "cached": cached,
        "configured": llm.configured(),
    }


# ---------- 普通用户首次使用与多关系档案 ----------

ANALYSIS_STATE = {"running": False, "error": "", "started_at": 0}


def _profile_ai_configured(item):
    if not item:
        return False
    root = item["data_root"]
    cfg = os.path.join(root, "config", "ai_config.json")
    secret = os.path.join(root, "config", "ai_secret.dat")
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return bool(data.get("base_url") and data.get("model") and os.path.exists(secret))


def _analysis_counts(item):
    result = {"days_total": 0, "days_done": 0, "months_done": 0, "year_done": 0}
    if not item:
        return result
    path = os.path.join(item["data_root"], "data", "clean", "chat.db")
    if not os.path.exists(path):
        return result
    try:
        con = sqlite3.connect(path)
        result["days_total"] = con.execute(
            "SELECT count(DISTINCT date) FROM messages WHERE date!=''"
        ).fetchone()[0]
        for key, table in (("days_done", "daily_summary"), ("months_done", "monthly_summary")):
            try:
                result[key] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass
        try:
            result["year_done"] = con.execute("SELECT count(*) FROM yearly_summary").fetchone()[0]
        except sqlite3.Error:
            pass
        con.close()
    except sqlite3.Error:
        pass
    return result


def _public_profile(item):
    if not item:
        return None
    result = dict(item)
    result.pop("data_root", None)
    result["ai_configured"] = _profile_ai_configured(item)
    result["analysis"] = _analysis_counts(item)
    return result


def _onboarding_state():
    active = PROFILES.active()
    profiles = PROFILES.list_profiles()
    return {
        "product": PRODUCT,
        "active": _public_profile(active),
        "profiles": [_public_profile(p) for p in profiles],
        "needs_onboarding": not bool(active and active.get("import_status") in ("imported", "analyzing", "ready")),
        "analysis_running": bool(ANALYSIS_STATE["running"]),
        "analysis_error": ANALYSIS_STATE["error"],
    }


def _start_wechat_import(profile_id, account, contact_id):
    """后台导入聊天，让浏览器可以持续轮询真实进度。"""
    item = PROFILES.get(str(profile_id or PROFILES.active_id()))
    if not item:
        raise ValueError("请先创建关系档案")
    contact_id = str(contact_id or "").strip()
    if not contact_id:
        raise ValueError("请选择一个联系人")

    def operation(update):
        processed = 0

        def reading_progress(**event):
            nonlocal processed
            current = max(0, int(event.get("current") or 0))
            total = max(1, int(event.get("total") or 1))
            processed += max(0, int(event.get("rows") or 0))
            update(
                progress=8 + int(60 * min(current, total) / total),
                stage="reading", processed=processed,
                message=f"正在读取聊天分片 {current}/{total}",
            )

        def writing_progress(**event):
            current = max(0, int(event.get("current") or 0))
            total = max(1, int(event.get("total") or 1))
            update(
                progress=72 + int(22 * min(current, total) / total),
                stage="writing", processed=max(processed, current),
                message="正在建立独立的本地聊天库",
            )

        update(progress=5, stage="reading", message="正在确认聊天分片")
        messages = wechat_native.fetch_contact_messages(
            str(account or ""), contact_id, progress=reading_progress,
        )
        update(progress=70, stage="writing", processed=len(messages),
               message=f"已读取 {len(messages):,} 条，正在整理")
        info = chat_import.write_chat_db(
            item["data_root"], messages, progress=writing_progress,
        )
        updated = PROFILES.update(
            item["id"], contact_id=contact_id, import_source="wechat-native",
            import_status="imported", message_count=info["message_count"],
            date_from=info["date_from"], date_to=info["date_to"],
        )
        update(progress=98, stage="finalizing", processed=info["message_count"],
               message="正在保存关系档案")
        return {"profile": _public_profile(updated), "import": info}

    return ONBOARDING_JOBS.start("wechat-import", operation)


def _start_wechat_prepare(account="", explicit_data_dir=None):
    """后台准备微信密钥并筛出真正有聊天记录的联系人。"""
    def operation(update):
        def key_progress(stage):
            stages = {
                "scanning_dll": (18, "正在检查微信程序信息"),
                "scanning_memory": (38, "正在安全读取本机微信密钥"),
            }
            pct, message = stages.get(str(stage), (12, "正在准备微信聊天数据"))
            update(progress=pct, stage=str(stage), message=message)

        update(progress=5, stage="detecting", message="正在查找微信和聊天数据")
        prepared = wechat_native.prepare(
            account=str(account or ""), explicit_data_dir=explicit_data_dir,
            reset=True, progress=key_progress,
        )
        if not prepared.get("connected"):
            raise RuntimeError(prepared.get("message") or "微信聊天数据没有准备完成")
        selected_account = prepared.get("default_account") or str(account or "")
        update(progress=62, stage="contacts", message="正在筛选有聊天记录的联系人")

        def contact_progress(**event):
            current = max(0, int(event.get("current") or 0))
            total = max(1, int(event.get("total") or 1))
            update(
                progress=62 + int(33 * min(current, total) / total),
                stage="contacts",
                message=f"正在检查聊天分片 {current}/{total}",
            )

        contacts = wechat_native.list_contacts(
            selected_account, progress=contact_progress,
        )
        if contacts.get("status") != "success":
            raise RuntimeError(contacts.get("message") or "联系人读取失败")
        update(progress=97, stage="contacts", message="联系人已经准备好")
        return {**prepared, **contacts, "connected": True, "stage": "ready"}

    return ONBOARDING_JOBS.start("wechat-prepare", operation)


def _start_json_import(profile_id, path):
    item = PROFILES.get(str(profile_id or PROFILES.active_id()))
    if not item:
        raise ValueError("请先创建关系档案")

    def operation(update):
        update(progress=8, stage="reading", message="正在读取 JSON 聊天文件")
        messages = chat_import.read_exported_json(path)
        update(progress=35, stage="writing", processed=len(messages),
               message=f"已识别 {len(messages):,} 条，正在建立本地聊天库")

        def writing_progress(**event):
            current = max(0, int(event.get("current") or 0))
            total = max(1, int(event.get("total") or 1))
            update(
                progress=35 + int(58 * min(current, total) / total),
                stage="writing", processed=current,
                message="正在写入独立的本地聊天库",
            )

        info = chat_import.write_chat_db(
            item["data_root"], messages, progress=writing_progress,
        )
        updated = PROFILES.update(
            item["id"], import_source="json", import_status="imported",
            message_count=info["message_count"], date_from=info["date_from"],
            date_to=info["date_to"],
        )
        update(progress=98, stage="finalizing", processed=info["message_count"],
               message="正在保存关系档案")
        return {"profile": _public_profile(updated), "import": info}

    return ONBOARDING_JOBS.start("json-import", operation)


def _save_profile_ai(item, api_key, base_url="https://api.deepseek.com",
                     model="deepseek-v4-pro"):
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("请粘贴 API Key")
    cfg_dir = os.path.join(item["data_root"], "config")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "ai_config.json"), "w", encoding="utf-8") as f:
        json.dump({"base_url": base_url.rstrip("/"), "model": model}, f,
                  ensure_ascii=False, indent=2)
    token = llm._dpapi(api_key) if os.name == "nt" else api_key
    secret = os.path.join(cfg_dir, "ai_secret.dat")
    with open(secret, "w", encoding="ascii" if os.name == "nt" else "utf-8") as f:
        f.write(token)


def _start_initial_analysis():
    """首次导入后自动建完整摘要金字塔；中断后重启会从未完成日期继续。"""
    active = PROFILES.active()
    if not active or active.get("import_status") not in ("imported", "analyzing"):
        return False
    if not _profile_ai_configured(active) or ANALYSIS_STATE["running"]:
        return False

    def run():
        ANALYSIS_STATE.update(running=True, error="", started_at=int(time.time()))
        PROFILES.update(active["id"], import_status="analyzing")
        try:
            import build_pyramid
            llm.require_cfg()
            dates = chatdb.list_days()
            build_pyramid.build_daily(dates, rebuild=False, dry_run=False)
            build_pyramid.build_monthly(rebuild=False, dry_run=False)
            build_pyramid.build_yearly(dry_run=False)
            chatdb.ensure_search_indexes()
            PROFILES.update(active["id"], import_status="ready")
        except Exception as exc:
            ANALYSIS_STATE["error"] = str(exc)[:500]
            PROFILES.update(active["id"], import_status="imported")
        finally:
            ANALYSIS_STATE["running"] = False

    threading.Thread(target=run, daemon=True, name="initial-analysis").start()
    return True


def _restart_process():
    def go():
        time.sleep(0.8)
        args = ([sys.executable, *sys.argv[1:]] if FROZEN else
                [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
        os.execv(sys.executable, args)
    threading.Thread(target=go, daemon=True).start()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header(
                "Set-Cookie",
                f"shiguang_session={APP_TOKEN}; Path=/; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return _request_is_authorized(
            self.headers.get("Host", ""), self.headers.get("Origin", ""),
            self.headers.get("Cookie", ""), APP_TOKEN,
        )

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.startswith("/api/") and u.path != "/api/product" and not self._authorized():
            self._send(403, {"error": "本地会话验证失败，请从拾光打开的页面使用"})
            return
        if u.path in ("/", "/index.html"):
            state = _onboarding_state()
            page = ONBOARDING_UI if state["needs_onboarding"] and os.path.exists(ONBOARDING_UI) else UI
            if os.path.exists(page):
                self._send(200, open(page, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(200, b"<h1>UI missing</h1>", "text/html; charset=utf-8")
        elif u.path == "/onboarding.html":
            if os.path.exists(ONBOARDING_UI):
                self._send(200, open(ONBOARDING_UI, "rb").read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "onboarding UI missing"})
        elif u.path == "/api/product":
            self._send(200, PRODUCT)
        elif u.path == "/api/app/heartbeat":
            if APP_LIFECYCLE:
                APP_LIFECYCLE.touch()
            self._send(200, {"ok": True})
        elif u.path in ("/api/onboarding/status", "/api/profiles"):
            self._send(200, _onboarding_state())
        elif u.path == "/api/onboarding/wechat":
            self._send(200, wechat_native.status())
        elif u.path == "/api/onboarding/contacts":
            account = q.get("account", [""])[0]
            result = wechat_native.list_contacts(account)
            self._send(200 if result.get("status") == "success" else 400, result)
        elif u.path == "/api/onboarding/analysis":
            self._send(200, _onboarding_state())
        elif u.path.startswith("/api/onboarding/jobs/"):
            job_id = u.path.rsplit("/", 1)[-1]
            job = ONBOARDING_JOBS.get(job_id)
            if job:
                self._send(200, job)
            else:
                self._send(404, {"error": "这个操作不存在或已经过期，请重试"})
        elif u.path == "/api/stats":
            self._send(200, do_stats())
        elif u.path == "/api/dashboard":
            self._send(200, do_dashboard())
        elif u.path == "/api/today":
            data = chatdb.today_hub()
            data["weather"] = chatdb.mood_weather(14)
            data["settings"] = appdb.get_settings()
            data["threads"] = chatdb.meaningful_threads(21, 3)
            warning = care.warning_data(14)
            radar = care.radar_data(90)
            letters = appdb.list_letters()
            couple = appdb.list_couple_prompts()
            actions = []
            if data["threads"]:
                item = data["threads"][0]
                actions.append({"kind": "message", "id": item["id"],
                                "title": "接住对方最近的一句话",
                                "detail": f"“{item['content'][:72]}” · {item['reason']}"})
            if warning.get("flags"):
                item = warning["flags"][-1]
                actions.append({"kind": "date", "date": item["date"],
                                "title": "看看最近的情绪信号",
                                "detail": " / ".join(item.get("tags") or [])})
            if radar.get("items"):
                item = radar["items"][0]
                actions.append({"kind": "radar", "date": item["date"],
                                "title": f"{item['days_left']} 天后的纪念日",
                                "detail": item["description"]})
            incomplete = [x for x in couple if not x.get("answer_me") or not x.get("answer_partner")]
            if incomplete:
                actions.append({"kind": "fun", "title": "补完双人答案",
                                "detail": incomplete[0]["question"]})
            unlocking = [x for x in letters if x.get("locked")]
            if unlocking:
                actions.append({"kind": "letter", "title": "有一封未来信正在等待",
                                "detail": f"{unlocking[0]['unlock_date']} 解锁 · {unlocking[0]['title']}"})
            data["action_items"] = actions[:4]
            self._send(200, data)
        elif u.path == "/api/timeline":
            self._send(200, chatdb.timeline((q.get("month") or [""])[0]))
        elif u.path == "/api/day":
            self._send(200, chatdb.get_day_detail((q.get("date") or [""])[0]))
        elif u.path == "/api/message-context":
            try:
                self._send(200, chatdb.get_message_context(
                    int((q.get("id") or [0])[0]), int((q.get("radius") or [8])[0])))
            except ValueError:
                self._send(400, {"error": "invalid message id"})
        elif u.path == "/api/mood-weather":
            self._send(200, {"items": chatdb.mood_weather((q.get("days") or [30])[0])})
        elif u.path == "/api/places":
            self._send(200, {"items": chatdb.place_memories()})
        elif u.path == "/api/reports":
            self._send(200, {"reports": list_reports(), "configured": llm.configured()})
        elif u.path == "/api/jobs":
            self._send(200, {"jobs": appdb.list_report_jobs()})
        elif u.path == "/api/settings":
            self._send(200, {"settings": appdb.get_settings(), "ai": llm.public_cfg()})
        elif u.path == "/api/privacy-exclusions":
            self._send(200, {"items": appdb.list_exclusions()})
        elif u.path == "/api/feedback":
            self._send(200, {"items": appdb.list_feedback()})
        elif u.path == "/api/pins":
            self._send(200, {"items": appdb.list_pins()})
        elif u.path == "/api/future-letters":
            self._send(200, {"items": appdb.list_letters()})
        elif u.path == "/api/saved-searches":
            self._send(200, {"items": appdb.list_saved_searches()})
        elif u.path == "/api/couple-prompts":
            self._send(200, {"items": appdb.list_couple_prompts()})
        elif u.path == "/api/conversations":
            self._send(200, {"conversations": appdb.list_conversations()})
        elif u.path.startswith("/api/conversations/"):
            cid = u.path.rsplit("/", 1)[-1]
            data = appdb.get_conversation(cid)
            self._send(200, data) if data else self._send(404, {"error": "conversation not found"})
        elif u.path == "/api/care":
            try:
                days = min(max(int((q.get("days") or [14])[0]), 1), 60)
                within = min(max(int((q.get("within") or [60])[0]), 1), 366)
            except ValueError:
                days, within = 14, 60
            self._send(200, care_snapshot(days, within))
        elif u.path == "/api/search/meta":
            self._send(200, chatdb.search_meta())
        elif u.path == "/api/fun/draw":
            try:
                self._send(200, fun.daily_draw())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/api/fun/badges":
            try:
                self._send(200, fun.badges())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/api/fun/quotes":
            try:
                n = min(max(int((q.get("n") or [36])[0]), 6), 100)
                self._send(200, fun.quote_wall(n))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/api/fun/quiz":
            try:
                n = min(max(int((q.get("n") or [6])[0]), 3), 10)
                self._send(200, fun.quiz_round(n))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/api/messages":
            try:
                result = chatdb.search_messages(
                    query=(q.get("q") or [""])[0],
                    mode=(q.get("mode") or ["keyword"])[0],
                    sender=(q.get("sender") or [""])[0],
                    date_from=(q.get("from") or [""])[0],
                    date_to=(q.get("to") or [""])[0],
                    msg_type=(q.get("type") or [""])[0],
                    page=int((q.get("page") or [1])[0]),
                    page_size=int((q.get("page_size") or [50])[0]),
                    sort=(q.get("sort") or ["recent"])[0],
                )
                self._send(200, result)
            except (ValueError, sqlite3.Error) as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/api/search/summaries":
            try:
                result = chatdb.search_summaries(
                    query=(q.get("q") or [""])[0],
                    date_from=(q.get("from") or [""])[0],
                    date_to=(q.get("to") or [""])[0],
                    page=int((q.get("page") or [1])[0]),
                    page_size=int((q.get("page_size") or [30])[0]),
                )
                self._send(200, result)
            except (ValueError, sqlite3.Error) as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/api/download":
            name = os.path.basename((q.get("name") or [""])[0])
            p = os.path.join(REPORTS_DIR, name)
            if name.endswith(".pdf") and os.path.exists(p):
                self._send(200, open(p, "rb").read(), "application/pdf")
            else:
                self._send(404, {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self._authorized():
            self._send(403, {"error": "本地会话验证失败，请从拾光打开的页面使用"})
            return
        try:
            n = _parse_content_length(self.headers.get("Content-Length", "0"))
            self.connection.settimeout(15)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TimeoutError, OSError) as e:
            self._send(400, {"error": str(e) or "请求读取失败"})
            return
        except json.JSONDecodeError:
            self._send(400, {"error": "JSON 请求格式无效"})
            return
        try:
            if u.path == "/api/app/close":
                self._send(200, {"closing": bool(APP_LIFECYCLE)})
                if APP_LIFECYCLE:
                    APP_LIFECYCLE.request_close()
            elif u.path == "/api/onboarding/helper":
                job_id = _start_wechat_prepare(str(body.get("account") or ""))
                self._send(202, {"job_id": job_id})
            elif u.path == "/api/onboarding/wechat-folder":
                path = wechat_native.choose_data_dir()
                if not path:
                    self._send(200, {"cancelled": True})
                else:
                    job_id = _start_wechat_prepare(
                        str(body.get("account") or ""), explicit_data_dir=path,
                    )
                    self._send(202, {"job_id": job_id})
            elif u.path == "/api/onboarding/deepseek":
                url = "https://platform.deepseek.com/api_keys"
                webbrowser.open(url)
                self._send(200, {"opened": True, "url": url})
            elif u.path == "/api/onboarding/profile":
                edition = PRODUCT["id"]
                relation_type = str(body.get("relation_type") or PRODUCT["default_relation"])
                display_name = str(body.get("display_name", "")).strip()
                profile_id = str(body.get("profile_id", "")).strip()
                item = PROFILES.get(profile_id) if profile_id else None
                if not item:
                    item = next((p for p in PROFILES.list_profiles()
                                 if p.get("edition") == edition
                                 and p.get("import_status") == "new"
                                 and p.get("display_name") == display_name
                                 and not p.get("contact_id")), None)
                if item and item.get("import_status") == "new" and item.get("edition") == edition:
                    item = PROFILES.update(
                        item["id"], display_name=display_name,
                        relation_type=relation_type,
                        relation_note=str(body.get("relation_note", "")),
                        contact_id=str(body.get("contact_id", "")),
                    )
                else:
                    item = PROFILES.create(
                        display_name, relation_type,
                        str(body.get("relation_note", "")), edition,
                        str(body.get("contact_id", "")),
                    )
                self._send(200, {"profile": _public_profile(item)})
            elif u.path == "/api/onboarding/import-wechat":
                job_id = _start_wechat_import(
                    str(body.get("profile_id") or PROFILES.active_id()),
                    str(body.get("account", "")),
                    str(body.get("contact_id", "")),
                )
                self._send(202, {"job_id": job_id})
            elif u.path == "/api/onboarding/import-json":
                path = chat_import.choose_json_file()
                if not path:
                    self._send(200, {"cancelled": True})
                    return
                job_id = _start_json_import(
                    str(body.get("profile_id") or PROFILES.active_id()), path,
                )
                self._send(202, {"job_id": job_id})
            elif u.path == "/api/onboarding/ai":
                item = PROFILES.get(str(body.get("profile_id") or PROFILES.active_id()))
                if not item:
                    raise ValueError("请先创建关系档案")
                base_url = str(body.get("base_url") or "https://api.deepseek.com").rstrip("/")
                model = str(body.get("model") or "deepseek-v4-pro")
                key = str(body.get("api_key") or "").strip()
                if not key:
                    raise ValueError("请粘贴 DeepSeek API Key")
                response = llm.requests.post(
                    base_url + "/chat/completions", timeout=30,
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": "只回复：连接成功"}],
                          "max_tokens": 12, "stream": False},
                )
                if response.status_code >= 400:
                    raise ValueError("API Key 验证失败，请确认已经创建密钥并且账户有可用额度")
                _save_profile_ai(item, key, base_url, model)
                PROFILES.set_active(item["id"])
                self._send(200, {"ok": True, "profile": _public_profile(PROFILES.get(item["id"])),
                                 "restart": True})
                _restart_process()
            elif u.path == "/api/profiles/activate":
                item = PROFILES.set_active(str(body.get("profile_id", "")))
                self._send(200, {"ok": True, "profile": _public_profile(item), "restart": True})
                _restart_process()
            elif u.path == "/api/chat":
                message = str(body.get("message", "")).strip()
                if not message:
                    self._send(400, {"error": "message is required"})
                    return
                cid = str(body.get("conversation_id", ""))
                if not appdb.exists(cid):
                    cid = appdb.create_conversation()
                history = appdb.recent_history(cid, limit=12)
                appdb.add_message(cid, "user", message)
                reply, trace = do_chat(message, history, cid)
                appdb.add_message(cid, "assistant", reply, model=FAST, meta={"tools": trace})
                threading.Thread(target=_refresh_conversation_summary, args=(cid,), daemon=True).start()
                self._send(200, {"reply": reply, "conversation_id": cid, "tools": trace})
            elif u.path == "/api/conversations":
                cid = appdb.create_conversation(str(body.get("title", "新对话"))[:64])
                self._send(200, {"conversation_id": cid})
            elif u.path == "/api/report":
                mode = body.get("mode", "deep")
                if mode != "deep":
                    self._send(400, {"error": "unsupported research mode"})
                    return
                jid = _enqueue_report(body.get("topic", "分析报告"), body.get("angle", ""), mode)
                self._send(200, {"job_id": jid})
            elif u.path == "/api/yearbook":
                settings = appdb.get_settings()
                year = str(body.get("year") or "这一年")[:20]
                topic = f"{settings.get('user_name','我')}和{settings.get('partner_name','对方')}的{year}关系年鉴"
                angle = "按月份梳理关系章节，覆盖高光、困难、共同成长、代表原话、数据统计与给未来的信。"
                self._send(200, {"job_id": _enqueue_report(topic, angle, "deep")})
            elif u.path == "/api/settings":
                self._send(200, {"settings": appdb.update_settings(body)})
            elif u.path == "/api/settings/ai":
                key = body.get("api_key")
                self._send(200, {"ai": llm.save_cfg(str(body.get("base_url", "")),
                                                      str(body.get("model", "")),
                                                      None if key is None else str(key))})
            elif u.path == "/api/privacy-exclusions":
                item_id = appdb.add_exclusion(str(body.get("date_from", "")),
                                              str(body.get("date_to", "")),
                                              str(body.get("keyword", "")),
                                              str(body.get("reason", "")))
                self._send(200, {"id": item_id})
            elif u.path == "/api/feedback":
                item_id = appdb.add_feedback(str(body.get("source_type", "general")),
                                             str(body.get("source_id", "")),
                                             str(body.get("verdict", "correct")),
                                             str(body.get("correction", "")))
                self._send(200, {"id": item_id})
            elif u.path == "/api/pins":
                item_id = appdb.pin_memory(str(body.get("content", "")),
                                           str(body.get("conversation_id", "")),
                                           body.get("message_id"))
                self._send(200, {"id": item_id})
            elif u.path == "/api/future-letters":
                item_id = appdb.create_letter(str(body.get("title", "给未来的我们")),
                                              str(body.get("content", "")),
                                              str(body.get("unlock_date", "")))
                self._send(200, {"id": item_id})
            elif u.path == "/api/saved-searches":
                item_id = appdb.save_search(str(body.get("name", "保存的搜索")), body.get("query", {}))
                self._send(200, {"id": item_id})
            elif u.path == "/api/couple-prompts":
                item = appdb.save_couple_answer(str(body.get("id", "weekly")),
                                                str(body.get("question", "这周最想感谢对方什么？")),
                                                str(body.get("side", "me")),
                                                str(body.get("answer", "")))
                self._send(200, item)
            elif u.path.startswith("/api/jobs/"):
                jid = u.path.rsplit("/", 1)[-1]
                job = appdb.get_report_job(jid)
                if not job:
                    self._send(404, {"error": "job not found"})
                elif body.get("action") == "retry":
                    appdb.update_report_job(jid, status="排队中", progress=0, error=None)
                    JOBQ.put(jid)
                    self._send(200, {"ok": True})
                elif body.get("action") == "cancel":
                    appdb.update_report_job(jid, status="已取消", progress=0)
                    self._send(200, {"ok": True})
                else:
                    self._send(400, {"error": "unknown action"})
            elif u.path == "/api/fun/whimsical":
                kind = str(body.get("kind", "song"))
                self._send(200, fun.whimsical_take(kind))
            elif u.path == "/api/care":
                action = body.get("action", "warning")
                if action == "warning":
                    days = min(max(int(body.get("days", 14)), 1), 60)
                    self._send(200, care.generate_warning(days))
                elif action == "brief":
                    period = body.get("period", "week")
                    if period not in ("day", "week"):
                        raise ValueError("period must be day or week")
                    self._send(200, care.generate_brief(period))
                else:
                    self._send(400, {"error": "unknown care action"})
            else:
                self._send(404, {"error": "not found"})
        except SystemExit as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if not self._authorized():
            self._send(403, {"error": "本地会话验证失败，请从拾光打开的页面使用"})
            return
        if u.path.startswith("/api/conversations/"):
            cid = u.path.rsplit("/", 1)[-1]
            if appdb.delete_conversation(cid):
                self._send(200, {"deleted": True})
            else:
                self._send(404, {"error": "conversation not found"})
        elif u.path.startswith("/api/privacy-exclusions/"):
            self._send(200, {"deleted": appdb.delete_exclusion(u.path.rsplit("/", 1)[-1])})
        elif u.path.startswith("/api/pins/"):
            self._send(200, {"deleted": appdb.delete_pin(u.path.rsplit("/", 1)[-1])})
        elif u.path.startswith("/api/future-letters/"):
            self._send(200, {"deleted": appdb.delete_letter(u.path.rsplit("/", 1)[-1])})
        elif u.path.startswith("/api/saved-searches/"):
            self._send(200, {"deleted": appdb.delete_saved_search(u.path.rsplit("/", 1)[-1])})
        else:
            self._send(404, {"error": "not found"})


def main():
    global APP_LIFECYCLE
    ap = argparse.ArgumentParser(description="拾光本地应用后端")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true", help="服务就绪后自动打开浏览器")
    args = ap.parse_args()
    try:
        state = chatdb.ensure_search_indexes()
        print(f"聊天全文索引已就绪：{state['messages']} 条")
    except Exception as e:
        print(f"[提示] 全文索引初始化失败，将保留其它功能：{e}")
    appdb.init()
    if FROZEN and os.path.isdir(PORTABLE_SEED) and not llm.configured():
        bootstrap_cfg = os.path.join(PORTABLE_SEED, "config", "ai_config.json")
        try:
            with open(bootstrap_cfg, encoding="utf-8") as f:
                bootstrap = json.load(f)
            bootstrap_key = str(bootstrap.get("api_key", "") or "").strip()
            if bootstrap_key:
                llm.save_cfg(str(bootstrap.get("base_url", "")),
                             str(bootstrap.get("model", "")), bootstrap_key)
        except Exception:
            pass
    llm.migrate_legacy_secret()
    for job_id in appdb.recover_report_jobs():
        JOBQ.put(job_id)
    threading.Thread(target=_worker, daemon=True).start()
    candidates = candidate_ports(
        args.port,
        consumer=CONSUMER_BUILD,
        edition=str(PRODUCT.get("id") or "general"),
    )
    server = None
    url = ""
    for candidate in candidates:
        url = f"http://127.0.0.1:{candidate}"
        try:
            server = ExclusiveThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            if candidate != args.port:
                print(f"[提示] 端口 {args.port} 被系统保留，已自动改用 {candidate}。")
            break
        except OSError as e:
            code = getattr(e, "winerror", None) or getattr(e, "errno", None)
            # 同一构建重复双击时复用；旧构建占用端口时继续尝试备用端口。
            if code in (48, 98, 10048):
                if CONSUMER_BUILD:
                    if same_product_running(
                            candidate,
                            PRODUCT.get("id"),
                            expected_build_id=PRODUCT.get("build_id")):
                        webbrowser.open(url)
                        return
                    continue
                webbrowser.open(url)
                return
            # Hyper-V / Windows 虚拟机会动态保留端口段，默认端口不可用时自动降级。
            if code == 10013 and candidate != candidates[-1]:
                continue
            raise
    if server is None:
        raise RuntimeError("没有可用的本地端口")
    if FROZEN:
        APP_LIFECYCLE = BrowserLifecycle(server.shutdown)
        # 浏览器若根本没打开或被系统直接结束，也不会留下永久占端口的后台进程。
        APP_LIFECYCLE.touch()
    server.daemon_threads = True
    print(f"{PRODUCT['name']}后端已启动： {url}")
    print("浏览器打开上面的地址即可使用。Ctrl+C 停止。")
    if not llm.configured():
        print("[提示] 还没配置模型接口(config/ai_config.json)，对话和报告功能不可用。")
    if args.open or FROZEN:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    # 新导入档案在用户完成 Key 配置并重启后自动开始全量分析；已有日期可断点续跑。
    threading.Timer(1.0, _start_initial_analysis).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n拾光已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
