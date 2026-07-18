"""摘要金字塔生成器 —— 整个 AI-native 功能的地基。

  第1层 每日详细摘要 daily_summary   (由当天原文总结；超长的日子先日内分块)
  第2层 每月摘要     monthly_summary  (由当月的日摘要汇总)
  第3层 全年索引     yearly_summary   (由各月摘要汇总)

一次性建好，之后增量补新的一天即可。用法：
  python scripts/build_pyramid.py --dry-run            # 离线预演，不花钱
  python scripts/build_pyramid.py --layer daily        # 建每日摘要(增量)
  python scripts/build_pyramid.py --layer daily --limit 3   # 先试跑 3 天
  python scripts/build_pyramid.py --layer all          # 一次建三层
  python scripts/build_pyramid.py --layer daily --rebuild   # 全部重建
"""
import argparse
import json
import time

import chatdb
import llm

DAY_CHUNK_CHARS = 16000    # 单日超过此长度就先日内分块浓缩
TAGS = ("日常", "支持", "分享", "幽默", "想念", "复盘", "成长", "分歧", "摩擦", "和解",
        "低落", "焦虑", "重要决定", "里程碑", "纪念", "计划", "关心", "合作")

DAILY_SYS = f"""你在为用户“我”和一位重要联系人“对方”的聊天记录建立每日档案。
给你某一天的对话，请输出**详细**的当天摘要，JSON 格式。要求：
- narrative：3-6 句，讲清这天的来龙去脉与氛围，要具体、不要简练。
- topics：当天聊到的主要话题(数组)。
- mood_me / mood_her：各自的情绪基调，一句话；字段名 her 仅代表“对方”，不限定性别。
- events：重要事件或关系节点(见面、决定、分歧和解、合作、里程碑等)，无则空数组。
- her_reflection：如果对方当天有较深的自我复盘/思考，详细记下并尽量带上对方原话；没有则空字符串。
- quotes：2-4 句最有代表性的原话，形如 [{{"who":"对方","text":"..."}}]。
- tags：从这个集合里选(可多选)：{"、".join(TAGS)}。
只输出 JSON，不要多余文字。"""

MONTHLY_SYS = """你在汇总用户与一位重要联系人某一个月的互动。给你这个月每天的摘要，请输出月度总结 JSON：
- narrative：这个月的主线、发生了什么、氛围如何(几句，具体)。
- mood_trend：这个月两人情绪的走势。
- growth：这个月里对方、用户或双方互动方式的成长/变化，若无明显变化可简述。
- topics：这个月反复出现的话题(数组)。
- key_days：这个月最关键的几天，形如 [{"date":"YYYY-MM-DD","why":"为什么重要"}]。
只输出 JSON。"""

YEARLY_SYS = """你在为用户与一位重要联系人做全期互动总览。给你每个月的月度总结，请输出 JSON：
- narrative：这段关系至今的脉络与主旋律(一段话)，不要默认它是恋爱关系。
- themes：贯穿始终的几个大主题(数组)。
- month_index：每个月一句话索引，形如 [{"month":"YYYY-MM","line":"这个月一句话"}]。
只输出 JSON。"""


def _relation_context():
    try:
        import os
        root = os.environ.get("SHIGUANG_ROOT", "")
        with open(os.path.join(root, "profile.json"), encoding="utf-8") as f:
            p = json.load(f)
        return (f"对方称呼：{p.get('display_name') or '对方'}；"
                f"关系类型：{p.get('relation_type') or 'other'}；"
                f"用户补充：{p.get('relation_note') or '无'}。请严格按这个真实关系理解，不擅自恋爱化。")
    except (OSError, ValueError):
        return "对方称呼：对方；关系类型未说明。不要擅自恋爱化。"


def _store_daily(con, date, me_n, her_n, data, model):
    con.execute(
        "INSERT OR REPLACE INTO daily_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date, me_n, her_n,
         data.get("narrative", ""),
         json.dumps(data.get("topics", []), ensure_ascii=False),
         data.get("mood_me", ""), data.get("mood_her", ""),
         json.dumps(data.get("events", []), ensure_ascii=False),
         data.get("her_reflection", "") or "",
         json.dumps(data.get("quotes", []), ensure_ascii=False),
         json.dumps(data.get("tags", []), ensure_ascii=False),
         model, int(time.time())))


def _local_fallback(date, me_n, her_n, reason):
    """AI 摘要失败(如内容审核)时的本地占位：存当天几条较长原话，不中断建塔。"""
    con = chatdb.connect_ro()
    rows = con.execute(
        "SELECT sender, content FROM messages WHERE date=? AND type='text' "
        "AND sender!='system' AND length(content)>=6 ORDER BY length(content) DESC LIMIT 5",
        (date,)).fetchall()
    con.close()
    quotes = [{"who": chatdb.SENDER_LABEL.get(r["sender"], r["sender"]),
               "text": (r["content"] or "")[:200]} for r in rows]
    return {"narrative": f"（本日约 {me_n + her_n} 条消息；AI 摘要未生成：{reason[:50]}。下为当天较长的几条原话。）",
            "topics": [], "mood_me": "", "mood_her": "", "events": [],
            "her_reflection": "", "quotes": quotes, "tags": ["未生成"]}


def build_daily(dates, rebuild, dry_run):
    con = chatdb.connect_rw()
    chatdb.ensure_schema(con)
    done = set(r[0] for r in con.execute("SELECT date FROM daily_summary"))
    todo = [d for d in dates if rebuild or d not in done]
    print(f"[每日] 共 {len(dates)} 天，已建 {len(done)} 天，本次待建 {len(todo)} 天")
    if dry_run:
        if todo:
            sample = todo[len(todo) // 2]
            txt, _ = chatdb.serialize_day(sample, max_chars=1200)
            print(f"  预演样例 {sample}:\n" + "\n".join("    " + l for l in txt.splitlines()[:8]))
        est = len(todo)
        print(f"  预估：约 {est} 次模型调用（每天一次，超长日多 1-2 次分块调用）")
        con.close()
        return
    model = llm.load_cfg()[2]
    fallbacks = []
    for i, date in enumerate(todo, 1):
        me_n, her_n = chatdb.day_counts(date)
        try:
            text, _ = chatdb.serialize_day(date)
            if len(text) > DAY_CHUNK_CHARS:
                briefs = []
                for ci, ch in enumerate(chatdb.chunk_day(date, DAY_CHUNK_CHARS), 1):
                    try:
                        b = llm.complete(
                            "把下面这段对话浓缩成要点(保留话题、情绪、重要的话和原话)，300 字内。",
                            ch, temperature=0.2, max_tokens=800)
                    except Exception:
                        b = ch[:1500]
                    briefs.append(f"[第{ci}段要点] {b}")
                source = "（当天对话较长，以下是分段要点）\n" + "\n".join(briefs)
            else:
                source = text
            data = llm.complete_json(DAILY_SYS + "\n" + _relation_context(),
                                     f"日期：{date}\n\n对话：\n{source}") or {}
            if not data.get("narrative"):
                raise ValueError("模型未返回有效摘要")
        except Exception as e:
            data = _local_fallback(date, me_n, her_n, str(e))
            fallbacks.append(date)
        _store_daily(con, date, me_n, her_n, data, model)
        con.commit()
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  {date}  {data.get('narrative','')[:36]}", flush=True)
    con.close()
    tail = f"（{len(fallbacks)} 天因内容审核/错误用了本地占位：{fallbacks}）" if fallbacks else ""
    print("[每日] 完成" + tail)


def build_monthly(rebuild, dry_run):
    con = chatdb.connect_rw()
    chatdb.ensure_schema(con)
    months = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM daily_summary ORDER BY m")]
    done = set(r[0] for r in con.execute("SELECT month FROM monthly_summary"))
    todo = [m for m in months if rebuild or m not in done]
    print(f"[每月] 共 {len(months)} 月，本次待建 {len(todo)} 月")
    if dry_run:
        con.close(); return
    for m in todo:
        rows = con.execute(
            "SELECT date, narrative, tags, events, her_reflection FROM daily_summary "
            "WHERE substr(date,1,7)=? ORDER BY date", (m,)).fetchall()
        lines = []
        for d, nar, tags, ev, refl in rows:
            piece = f"{d} [{','.join(json.loads(tags or '[]'))}] {nar}"
            if refl:
                piece += f" ｜对方的复盘：{refl[:120]}"
            lines.append(piece)
        data = llm.complete_json(MONTHLY_SYS + "\n" + _relation_context(),
                                 f"月份：{m}\n\n每日摘要：\n" + "\n".join(lines)) or {}
        con.execute(
            "INSERT OR REPLACE INTO monthly_summary VALUES (?,?,?,?,?,?,?,?)",
            (m, data.get("narrative", ""), data.get("mood_trend", ""),
             data.get("growth", ""),
             json.dumps(data.get("topics", []), ensure_ascii=False),
             json.dumps(data.get("key_days", []), ensure_ascii=False),
             llm.load_cfg()[2], int(time.time())))
        con.commit()
        print(f"  {m}  {data.get('narrative','')[:40]}")
    con.close()
    print("[每月] 完成")


def build_yearly(dry_run):
    con = chatdb.connect_rw()
    chatdb.ensure_schema(con)
    rows = con.execute(
        "SELECT month, narrative, growth FROM monthly_summary ORDER BY month").fetchall()
    print(f"[全年] 汇总 {len(rows)} 个月")
    if dry_run or not rows:
        con.close(); return
    src = "\n".join(f"{m}：{nar} ｜成长：{g}" for m, nar, g in rows)
    data = llm.complete_json(YEARLY_SYS + "\n" + _relation_context(), "各月总结：\n" + src) or {}
    con.execute(
        "INSERT OR REPLACE INTO yearly_summary VALUES (1,?,?,?,?,?)",
        (data.get("narrative", ""),
         json.dumps(data.get("themes", []), ensure_ascii=False),
         json.dumps(data.get("month_index", []), ensure_ascii=False),
         llm.load_cfg()[2], int(time.time())))
    con.commit()
    con.close()
    print("[全年] 完成")


def main():
    ap = argparse.ArgumentParser(description="建立摘要金字塔")
    ap.add_argument("--layer", choices=["daily", "monthly", "yearly", "all"],
                    default="daily")
    ap.add_argument("--dry-run", action="store_true", help="离线预演，不调模型")
    ap.add_argument("--rebuild", action="store_true", help="重建(忽略已有)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 天(调试)")
    ap.add_argument("--from", dest="dfrom", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="dto", help="结束日期 YYYY-MM-DD")
    args = ap.parse_args()

    if not args.dry_run:
        llm.require_cfg()

    dates = chatdb.list_days()
    if args.dfrom:
        dates = [d for d in dates if d >= args.dfrom]
    if args.dto:
        dates = [d for d in dates if d <= args.dto]
    if args.limit:
        dates = dates[:args.limit]

    if args.layer in ("daily", "all"):
        build_daily(dates, args.rebuild, args.dry_run)
    if args.layer in ("monthly", "all"):
        build_monthly(args.rebuild, args.dry_run)
    if args.layer in ("yearly", "all"):
        build_yearly(args.dry_run)


if __name__ == "__main__":
    main()
