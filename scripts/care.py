"""关系提醒类 —— 让 AI 主动留意重要联系人的近期状态。

  warn   情绪预警：扫最近若干天，判断她状态、是否需要关心，给出可说的话
  brief  简报：把最近一天/一周写成一条关系简报
  radar  纪念日雷达：交往纪念、里程碑、那年今天(这条不需要联网)

用法：
  python scripts/care.py warn
  python scripts/care.py brief --period week
  python scripts/care.py radar
"""
import argparse
import datetime
import json
import os

import chatdb
import llm
import appdb

ROOT = os.environ.get("SHIGUANG_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARE_DIR = os.path.join(ROOT, "reports", "care")
NEG = {"低落", "焦虑", "吵架", "摩擦"}


def _recent_daily(days):
    con = chatdb.connect_ro()
    try:
        rows = con.execute(
            "SELECT date, narrative, mood_me, mood_her, tags, her_reflection "
            "FROM daily_summary ORDER BY date DESC LIMIT ?", (days,)).fetchall()
    except Exception:
        rows = []
    con.close()
    return list(reversed(rows))


def _fmt(rows):
    partner = appdb.get_settings().get("partner_name", "对方")
    out = []
    for d in rows:
        p = (f"{d['date']} [{','.join(json.loads(d['tags'] or '[]'))}] "
             f"{partner}:{d['mood_her']}｜{d['narrative']}")
        if d["her_reflection"]:
            p += f"｜{partner}的复盘：{d['her_reflection'][:120]}"
        out.append(p)
    return "\n".join(out)


def _need_pyramid():
    con = chatdb.connect_ro()
    try:
        n = con.execute("SELECT count(*) FROM daily_summary").fetchone()[0]
    except Exception:
        n = 0
    con.close()
    if n == 0:
        print("[-] 摘要金字塔还没建。先跑： python scripts/build_pyramid.py --layer daily")
        return True
    return False


def warning_data(days=14):
    """返回不调用模型的本地情绪信号，供网页和命令行复用。"""
    rows = _recent_daily(days)
    recent = []
    flags = []
    for d in rows:
        try:
            tags = json.loads(d["tags"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        hit = sorted(set(tags) & NEG)
        if hit:
            flags.append({"date": d["date"], "tags": hit})
        recent.append({
            "date": d["date"], "mood_her": d["mood_her"] or "",
            "tags": tags, "narrative": d["narrative"] or "",
        })
    signal = (f"近 {days} 天里有 {len(flags)} 天出现低落/焦虑/摩擦信号"
              if flags else f"近 {days} 天没有明显负面信号")
    return {"days": days, "signal": signal, "flags": flags, "recent": recent}


def generate_warning(days=14, save=True):
    """按需生成 AI 关心建议；不会在网页加载时自动把数据发给模型。"""
    if _need_pyramid():
        raise RuntimeError("摘要金字塔还没建")
    llm.require_cfg()
    rows = _recent_daily(days)
    local = warning_data(days)
    flag_dates = [x["date"] for x in local["flags"]]
    hint = (f"近 {days} 天里有 {len(flag_dates)} 天出现低落/焦虑/摩擦信号：{flag_dates}"
            if flag_dates else f"近 {days} 天没有明显负面信号。")
    settings = appdb.get_settings()
    partner = settings.get("partner_name", "对方")
    ans = llm.complete(
        f"你在帮用户留意重要联系人“{partner}”最近的状态。关系类型是"
        f"{settings.get('relationship_type','other')}，补充介绍：{settings.get('relationship_note','无')}。"
        "根据最近每天的摘要，判断对方最近状态如何、"
        "有没有需要关心的信号。若需要关心，给出 1-2 条具体真诚的关心方式，"
        "并给一句可以直接说的话。尊重关系边界、不擅自恋爱化、不夸张、别编。",
        f"信号提示：{hint}\n\n最近每日摘要：\n{_fmt(rows)}")
    if save:
        _save("情绪预警", f"# 情绪预警\n\n> {hint}\n\n{ans}\n")
    return {"title": "情绪预警", "hint": hint, "content": ans}


def generate_brief(period="week", save=True):
    if _need_pyramid():
        raise RuntimeError("摘要金字塔还没建")
    llm.require_cfg()
    days = 1 if period == "day" else 7
    rows = _recent_daily(days)
    if not rows:
        raise RuntimeError("还没有摘要数据")
    label = "今日" if period == "day" else "本周"
    partner = appdb.get_settings().get("partner_name", "对方")
    ans = llm.complete(
        f"把最近的关系情况写成一条{label}简报：{partner}的状态、双方聊了什么、"
        "有没有要留意的、最后一句温柔的提醒。简洁，像贴心助理的日报。",
        _fmt(rows))
    if save:
        _save(f"{label}简报", f"# {label}关系简报\n\n{ans}\n")
    return {"title": f"{label}关系简报", "content": ans}


def radar_data(within=60):
    """返回本地纪念日扫描结果，不调用模型。"""
    today = datetime.date.today()
    tmd = today.strftime("%m-%d")
    con = chatdb.connect_ro()
    start = con.execute(
        "SELECT min(date) FROM messages WHERE date!='' AND sender!='system'").fetchone()[0]
    same_day = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM messages WHERE substr(date,6,5)=? AND date<? "
        "AND sender!='system' ORDER BY date", (tmd, today.isoformat()))]
    miles = []
    try:
        for date, ev, tags, nar in con.execute(
            "SELECT date, events, tags, narrative FROM daily_summary "
            "WHERE tags LIKE '%里程碑%' OR tags LIKE '%纪念%' "
            "OR (events!='[]' AND events!='')"):
            try:
                evs = json.loads(ev or "[]")
            except (TypeError, json.JSONDecodeError):
                evs = []
            miles.append((date, "；".join(evs) if evs else (nar or "")[:30]))
    except Exception:
        pass
    con.close()

    items = []
    if start:
        occ = _next_occ(start[5:], today)
        if occ:
            items.append((occ, (occ - today).days, f"相识/交往纪念日（{start} 起）"))
    for date, desc in miles:
        occ = _next_occ(date[5:], today)
        if occ:
            items.append((occ, (occ - today).days, f"{desc}（源自 {date}）"))
    items = sorted([x for x in items if 0 <= x[1] <= within], key=lambda x: x[1])
    return {
        "today": today.isoformat(), "within": within, "same_day": same_day,
        "items": [{"date": occ.isoformat(), "days_left": left, "description": desc}
                  for occ, left, desc in items],
    }


def cmd_warn(days):
    try:
        result = generate_warning(days)
    except RuntimeError as e:
        print(f"[-] {e}")
        return
    print("\n===== 情绪预警 =====")
    print(f"（信号：{result['hint']}）\n")
    print(result["content"])


def cmd_brief(period):
    try:
        result = generate_brief(period)
    except RuntimeError as e:
        print(f"[-] {e}")
        return
    print(f"\n===== {result['title']} =====\n")
    print(result["content"])


def _next_occ(mmdd, today):
    try:
        m, d = int(mmdd[:2]), int(mmdd[3:])
        occ = datetime.date(today.year, m, d)
    except ValueError:
        return None
    if occ < today:
        try:
            occ = datetime.date(today.year + 1, m, d)
        except ValueError:
            return None
    return occ


def cmd_radar(within):
    """纪念日雷达。日期逻辑不需要联网；有配置时用 AI 加一句温柔提醒。"""
    data = radar_data(within)

    print("\n===== 纪念日雷达 =====")
    if data["same_day"]:
        print(f"\n【那年今天 · {data['today'][5:]}】历史上的今天你们也在聊：")
        for d in data["same_day"]:
            print(f"  · {d}")
    if data["items"]:
        print(f"\n【未来 {within} 天内的纪念日】")
        for item in data["items"]:
            occ, dleft, desc = item["date"], item["days_left"], item["description"]
            when = "就是今天！" if dleft == 0 else f"还有 {dleft} 天（{occ}）"
            print(f"  · {when}  {desc}")
    else:
        print(f"\n未来 {within} 天内没有检测到纪念日。")

    if llm.configured() and data["items"]:
        tip = llm.complete(
            "你是贴心助手。根据下面即将到来的纪念日，给男朋友一句温柔的提醒和一个小建议。简短。",
            "\n".join(f"{x['description']} — {x['date']}" for x in data["items"]))
        print(f"\n【提醒】{tip}")


def _save(name, md):
    os.makedirs(CARE_DIR, exist_ok=True)
    p = os.path.join(CARE_DIR, f"{name}.md")
    open(p, "w", encoding="utf-8").write(md)
    print(f"\n[+] 已保存：{p}")


def main():
    ap = argparse.ArgumentParser(description="主动关心")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("warn"); w.add_argument("--days", type=int, default=14)
    b = sub.add_parser("brief"); b.add_argument("--period", choices=["day", "week"], default="week")
    r = sub.add_parser("radar"); r.add_argument("--within", type=int, default=60)
    a = ap.parse_args()
    if a.cmd == "warn":
        cmd_warn(a.days)
    elif a.cmd == "brief":
        cmd_brief(a.period)
    elif a.cmd == "radar":
        cmd_radar(a.within)


if __name__ == "__main__":
    main()
