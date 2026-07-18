"""好玩功能 —— 和 Deep Research/关心中心刻意拉开区别：轻快、游戏化、大多数秒出结果。

设计原则：能用真实数据零成本算出来的，绝不调用 AI（即时、免费、可无限重来）；
只有需要"创意生成"的地方（关系小剧场）才调一次 Pro 模型。

  daily_draw()      时光信笺：随机抽一天的金句卡片（零AI）
  badges()          成就徽章墙：全部由真实数据现算，随聊天增长自然解锁（零AI）
  quote_wall(n)      弹幕回忆墙素材：随机抽 n 条金句（零AI）
  quiz_round(n)      猜猜看小测验：谁说的/哪个月，正确答案由真实数据保证（零AI）
  whimsical_take(k)  关系小剧场：如果我们是一首歌/一道菜/一种天气…（一次 Pro 调用）
"""
import datetime
import json
import random

import chatdb
import llm
import appdb

FAST = "deepseek-v4-pro"
MONTH_NAMES = ["一月", "二月", "三月", "四月", "五月", "六月", "七月",
               "八月", "九月", "十月", "十一月", "十二月"]

WHIMSICAL_KINDS = {
    "song": "如果你们的关系是一首歌，会是什么风格、什么名字？为什么？",
    "dish": "如果你们的关系是一道菜，会是什么菜、什么味道？为什么？",
    "weather": "如果用天气来形容你们最近的关系状态，会是什么天气？为什么？",
    "movie": "如果你们的故事被拍成一部电影，会是什么类型、大概叫什么名字？为什么？",
    "season": "如果你们的关系是一个季节，此刻正处在这个季节的哪个阶段？为什么？",
}


def _all_quotes():
    """展开 daily_summary.quotes 为 [{date, who, text}]，缓存一次查询结果。"""
    con = chatdb.connect_ro()
    rows = con.execute(
        "SELECT date, quotes FROM daily_summary WHERE quotes!='[]' AND quotes IS NOT NULL").fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            qs = json.loads(r["quotes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            qs = []
        for q in qs:
            text = (q.get("text") or "").strip()
            who = q.get("who") or ""
            if text and who:
                out.append({"date": r["date"], "who": who, "text": text})
    return out


def daily_draw():
    """时光信笺：随机抽一天，返回叙述 + 金句，像抽到一张卡片。零AI、可无限重抽。"""
    con = chatdb.connect_ro()
    row = con.execute(
        "SELECT date, narrative, mood_me, mood_her, tags, quotes FROM daily_summary "
        "WHERE quotes!='[]' AND quotes IS NOT NULL AND narrative!='' "
        "ORDER BY RANDOM() LIMIT 1").fetchone()
    if not row:
        con.close()
        return {"error": "还没有摘要数据"}
    me_n, her_n = con.execute(
        "SELECT sum(sender='me'), sum(sender='her') FROM messages WHERE date=?",
        (row["date"],)).fetchone()
    con.close()
    try:
        quotes = json.loads(row["quotes"] or "[]")
    except (TypeError, json.JSONDecodeError):
        quotes = []
    try:
        tags = json.loads(row["tags"] or "[]")
    except (TypeError, json.JSONDecodeError):
        tags = []
    days_ago = (datetime.date.today() - datetime.date.fromisoformat(row["date"])).days
    return {
        "date": row["date"], "days_ago": days_ago,
        "narrative": row["narrative"], "mood_me": row["mood_me"] or "",
        "mood_her": row["mood_her"] or "", "tags": tags,
        "quotes": quotes[:4], "me_count": me_n or 0, "her_count": her_n or 0,
    }


def badges():
    """成就徽章墙：全部由真实数据现算。随着每天新聊天，进度和解锁状态自动变化。"""
    con = chatdb.connect_ro()
    total, late = con.execute(
        "SELECT count(*), sum(hour BETWEEN 0 AND 5) FROM messages WHERE sender!='system'").fetchone()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM messages WHERE sender!='system' AND date!='' ORDER BY date")]
    start = dates[0] if dates else None
    calls = con.execute(
        "SELECT count(*) FROM messages WHERE type='通话'").fetchone()[0]
    non_text = con.execute(
        "SELECT count(*) FROM messages WHERE sender!='system' AND type!='text'").fetchone()[0]
    her_reflect = con.execute(
        "SELECT count(*) FROM daily_summary WHERE her_reflection!='' AND her_reflection IS NOT NULL"
    ).fetchone()[0]

    def tag_days(tag):
        return con.execute(
            "SELECT count(*) FROM daily_summary WHERE tags LIKE ?", (f"%{tag}%",)).fetchone()[0]

    sweet_days = tag_days("甜蜜") + tag_days("支持")
    growth_days = tag_days("成长")
    reconcile_days = tag_days("和好")
    con.close()

    longest = cur = 0
    prev = None
    for d in dates:
        day = datetime.date.fromisoformat(d)
        cur = cur + 1 if prev and day == prev + datetime.timedelta(days=1) else 1
        longest = max(longest, cur)
        prev = day
    days_since_start = (datetime.date.today() - datetime.date.fromisoformat(start)).days if start else 0

    def badge(icon, name, desc, value, target, unit=""):
        pct = min(100, round(value / target * 100)) if target else 100
        return {"icon": icon, "name": name, "desc": desc, "unlocked": value >= target,
                "value": value, "target": target, "unit": unit, "pct": pct,
                "progress": f"{value}/{target}{unit}" if value < target else "已达成"}

    defs = [
        badge("🎯", "十万条羁绊", "累计消息数破十万", total or 0, 100000, "条"),
        badge("🔥", "连聊不打烊", "最长连续聊天天数达 100 天", longest, 100, "天"),
        badge("🌙", "深夜观星人", "凌晨 0-6 点消息占比达 15%",
              round((late or 0) / max(total or 1, 1) * 100), 15, "%"),
        badge("💌", "认真听见彼此", "对方主动深度复盘的天数达 200 天", her_reflect, 200, "天"),
        badge("🕊️", "没有隔夜仇", "记录到的和好天数达 20 天", reconcile_days, 20, "天"),
        badge("😊", "表情包战神", "表情/图片/通话等非文字互动破 5000", non_text, 5000, "次"),
        badge("💕", "支持与温暖", "支持或温暖互动的天数达 150 天", sweet_days, 150, "天"),
        badge("☎️", "电波不断线", "累计通话记录破 500 次", calls, 500, "次"),
        badge("🌱", "一起成长", "成长标签的天数达 100 天", growth_days, 100, "天"),
        badge("📅", "相识一周年", "从相识那天起满 365 天", days_since_start, 365, "天"),
    ]
    return {"start": start, "unlocked": sum(1 for b in defs if b["unlocked"]),
            "total": len(defs), "badges": defs}


def quote_wall(n=36):
    """弹幕回忆墙素材：随机抽 n 条金句，供前端做滚动弹幕。零AI。"""
    pool = _all_quotes()
    if not pool:
        return {"quotes": []}
    random.shuffle(pool)
    return {"quotes": pool[:n]}


def quiz_round(n=6):
    """猜猜看小测验：谁说的 / 哪个月说的。正确答案直接来自真实数据，零AI、零出错风险。"""
    pool = _all_quotes()
    if len(pool) < 8:
        return {"questions": [], "error": "金句数据还不够出题"}
    months = sorted({q["date"][:7] for q in pool})
    picks = random.sample(pool, min(n, len(pool)))
    questions = []
    for i, q in enumerate(picks):
        if i % 2 == 0:
            partner = appdb.get_settings().get("partner_name", "对方")
            questions.append({
                "type": "who", "quote": q["text"], "date": q["date"],
                "options": ["我", partner], "answer": "我" if q["who"] == "我" else partner,
                "prompt": "这句话是谁说的？",
            })
        else:
            correct_month = q["date"][:7]
            wrong_pool = [m for m in months if m != correct_month]
            wrongs = random.sample(wrong_pool, min(3, len(wrong_pool)))
            opts = wrongs + [correct_month]
            random.shuffle(opts)
            label = lambda m: f"{m[:4]}年{MONTH_NAMES[int(m[5:7]) - 1]}"
            questions.append({
                "type": "month", "quote": q["text"], "date": q["date"],
                "options": [label(m) for m in opts], "answer": label(correct_month),
                "prompt": "这句话大概是哪个月说的？",
            })
    return {"questions": questions}


def whimsical_take(kind):
    """关系小剧场：唯一真正调 AI 的好玩功能，一次 Pro 短生成，轻松诙谐。"""
    if kind not in WHIMSICAL_KINDS:
        kind = "song"
    con = chatdb.connect_ro()
    yr = con.execute("SELECT narrative, themes FROM yearly_summary WHERE id=1").fetchone()
    recent = con.execute(
        "SELECT narrative, mood_me, mood_her, tags FROM daily_summary "
        "ORDER BY date DESC LIMIT 1").fetchone()
    con.close()
    context = ""
    if yr:
        context += f"关系全期脉络：{yr['narrative']}\n"
    if recent:
        context += f"最近状态：{recent['narrative']}（我：{recent['mood_me']}，对方：{recent['mood_her']}）"
    ans = llm.complete(
        "你是个有点文艺又爱开玩笑的朋友，帮两位重要联系人做一个轻松好玩的比喻小测评。"
        "根据提供的关系类型理解，不要擅自恋爱化。"
        "只根据给的真实背景发挥想象，风格俏皮、有画面感，别写成正经分析。"
        "严格 80-140 字，不要分点、不要标题，直接一段话给出比喻和理由。",
        f"{WHIMSICAL_KINDS[kind]}\n\n背景参考（不要逐字复述，只作为灵感）：\n{context}",
        temperature=0.9, max_tokens=400, model=FAST)
    return {"kind": kind, "prompt": WHIMSICAL_KINDS[kind], "text": ans.strip()}
