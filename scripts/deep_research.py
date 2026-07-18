"""深度研究引擎 —— 在摘要金字塔上做"逐月扫全每一天 → 深挖原话 → 成文 → 自查"。

严格遵守：做报告时**每个月都扫、每一天的摘要都看**，不跳月、不只挑关键月份。
金字塔让这件事扫得起：一次只把一个月的日摘要喂进模型，边扫边攒发现。

可编程调用 research(...)，也可命令行对任意主题出报告：
    python scripts/deep_research.py --topic "我们的争吵模式" --angle "分析触发点/化解方式/是否重复"
"""
import argparse
import json
import os
import time

import chatdb
import appdb
import llm
import report_pdf

ROOT = os.environ.get("SHIGUANG_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")
MAX_DEEPDIVE_DAYS = 16   # 深挖阶段最多回原文读几天


def _load_pyramid():
    con = chatdb.connect_ro()
    daily = con.execute(
        "SELECT date, narrative, mood_me, mood_her, tags, events, her_reflection, quotes "
        "FROM daily_summary ORDER BY date").fetchall()
    monthly = con.execute(
        "SELECT month, narrative, mood_trend, growth, key_days FROM monthly_summary "
        "ORDER BY month").fetchall()
    yearly = con.execute("SELECT narrative, themes, month_index FROM yearly_summary "
                         "WHERE id=1").fetchone()
    con.close()
    return daily, monthly, yearly


def _overview_text(yearly, monthly):
    if yearly:
        return (f"全期脉络：{yearly['narrative']}\n"
                f"大主题：{yearly['themes']}\n各月索引：{yearly['month_index']}")
    return "各月主线：\n" + "\n".join(
        f"{m['month']}：{m['narrative']}" for m in monthly)


def research(topic, angle, day_tags=None, focus=None, critique=True,
             out_prefix=None, pdf=True, subtitle="", dedication="",
             progress=True):
    llm.require_cfg()
    daily, monthly, yearly = _load_pyramid()
    exclusions = appdb.list_exclusions(active_only=True)
    def allowed_day(row):
        date = row["date"]
        text = " ".join(str(row[k] or "") for k in
                        ("narrative", "mood_me", "mood_her", "tags", "events", "her_reflection"))
        for rule in exclusions:
            start, end = rule.get("date_from", ""), rule.get("date_to", "")
            if start and start <= date <= (end or start):
                return False
            if rule.get("keyword") and rule["keyword"] in text:
                return False
        return True
    daily = [d for d in daily if allowed_day(d)]
    if not daily:
        raise SystemExit("[-] 摘要金字塔还没建。先跑： python scripts/build_pyramid.py --layer all")

    def log(*a):
        if progress:
            print(*a, flush=True)

    span = f"{daily[0]['date']} ~ {daily[-1]['date']}"
    overview = _overview_text(yearly, monthly)

    # ① 规划
    plan = llm.complete(
        "你是关系研究者。根据研究主题和全期概览，列出 3-6 个要考察的维度/子问题，"
        "简短分条。只输出维度清单。",
        f"研究主题：{topic}\n研究视角：{angle}\n\n全期概览：\n{overview}") or ""
    log(f"[规划] 维度：\n{plan}\n")

    # ② 逐月扫全每一天
    months = sorted(set(d["date"][:7] for d in daily))
    findings, key_dates = [], []
    for mi, m in enumerate(months, 1):
        days = [d for d in daily if d["date"].startswith(m)]
        if day_tags:
            days = [d for d in days
                    if set(json.loads(d["tags"] or "[]")) & set(day_tags)]
        if not days:
            continue
        rows = []
        for d in days:
            piece = (f"{d['date']} [{','.join(json.loads(d['tags'] or '[]'))}] "
                     f"我情绪:{d['mood_me']} 对方情绪:{d['mood_her']} ｜{d['narrative']}")
            if d["her_reflection"]:
                piece += f" ｜对方的复盘：{d['her_reflection'][:150]}"
            rows.append(piece)
        try:
            res = llm.complete_json(
                f"你在研究「{topic}」。视角：{angle}\n研究维度：\n{plan}\n\n"
                "给你某个月每天的摘要，请就本主题提炼**本月**的发现，输出 JSON："
                '{"findings":"本月与主题相关的观察(具体，可引日期)","key_dates":["最该回看原文的1-4天YYYY-MM-DD"]}',
                f"月份：{m}\n\n每日摘要：\n" + "\n".join(rows)) or {}
            f = res.get("findings", "")
            kd = [x for x in res.get("key_dates", []) if isinstance(x, str)]
        except Exception as e:
            mm = next((x for x in monthly if x["month"] == m), None)
            f = (mm["narrative"] if mm else "") + f"（本月AI提炼失败，用月度摘要兜底：{str(e)[:30]}）"
            kd = []
        if f:
            findings.append(f"【{m}】{f}")
        key_dates += kd
        log(f"[扫描] {mi}/{len(months)} {m}  提炼 {len(f)} 字")

    # ③ 深挖：回原文取金句
    seen, quotes_block = set(), []
    for d in key_dates:
        if d in seen or len(seen) >= MAX_DEEPDIVE_DAYS:
            continue
        seen.add(d)
        raw = json.loads(chatdb.read_messages(date_from=d, date_to=d, limit=60))
        if exclusions:
            raw["rows"] = [r for r in raw.get("rows", []) if not any(
                rule.get("keyword") and rule["keyword"] in (r.get("content") or "")
                for rule in exclusions)]
        text = "\n".join(f"{r['dt'][11:16]} {r['who']}: {r['content']}"
                         for r in raw.get("rows", []))
        if text:
            quotes_block.append(f"—— {d} ——\n{text[:2500]}")
    log(f"[深挖] 回看 {len(seen)} 天原文取证")

    # ④ 成文
    writer_sys = (
        "你是一位细腻、诚实的关系观察者，为用户写一份深度报告(Markdown)。"
        "要求：# 一级标题作为报告名；分 4-6 个 ## 小节，每节有观点也有证据；"
        "引用真实原话时用 > 引用块，并注明日期和是谁说的；"
        "结尾一节写坦诚的总结或建议。基于给到的材料写，不要编造。")
    try:
        report = llm.complete(
            writer_sys,
            f"报告主题：{topic}\n视角：{angle}\n时间跨度：{span}\n\n"
            f"研究维度：\n{plan}\n\n逐月发现：\n" + "\n\n".join(findings)
            + "\n\n可引用的原文片段：\n" + "\n\n".join(quotes_block),
            temperature=0.6, max_tokens=8000) or ""
    except Exception as e:
        log(f"[成文] 综合环节失败({str(e)[:40]})，改用逐月发现兜底成文")
        report = ""
    if not report:
        report = (f"# {topic}\n\n> 说明：AI 综合环节未完成，以下为逐月发现的汇总。\n\n"
                  + "\n\n".join(f"## {f.split('】')[0].lstrip('【')}\n{f.split('】', 1)[-1]}"
                                for f in findings))

    # ⑤ 自查补漏
    if critique and report:
        try:
            issues = llm.complete(
                "你是严格的审稿人。指出这份报告哪些结论缺证据、哪个时间段/维度没覆盖到、"
                "哪里空泛。只列问题，简短分条；若确实扎实就回复“无”。", report) or ""
            if issues.strip() and issues.strip() != "无":
                log(f"[自查] 发现待补：\n{issues}\n")
                report = llm.complete(
                    writer_sys + " 现在根据审稿意见修订，补齐证据、覆盖遗漏，保持体例。",
                    f"审稿意见：\n{issues}\n\n原报告：\n{report}\n\n"
                    f"可补充引用的原文：\n" + "\n\n".join(quotes_block),
                    temperature=0.5, max_tokens=8000) or report
        except Exception as e:
            log(f"[自查] 跳过({str(e)[:40]})")

    # 落盘
    prefix = out_prefix or topic
    os.makedirs(REPORTS_DIR, exist_ok=True)
    md_path = os.path.join(REPORTS_DIR, f"{prefix}.md")
    open(md_path, "w", encoding="utf-8").write(report)
    log(f"[+] Markdown：{md_path}")
    if pdf:
        pdf_path = os.path.join(REPORTS_DIR, f"{prefix}.pdf")
        dr = span.replace("-", ".").replace(" ~ ", " — ")
        report_pdf.render(report, pdf_path, title=prefix, subtitle=subtitle,
                          date_range=dr, dedication=dedication, footer=prefix)
        log(f"[+] PDF：{pdf_path}  {os.path.getsize(pdf_path)/1024:.0f} KB")
    return md_path


def main():
    ap = argparse.ArgumentParser(description="对任意主题做深度研究报告")
    ap.add_argument("--topic", required=True, help="研究主题，也用作文件名")
    ap.add_argument("--angle", default="全面、深入地考察这个主题", help="研究视角/要点")
    ap.add_argument("--tags", help="只看带这些标签的日子(逗号分隔，如 吵架,摩擦)")
    ap.add_argument("--no-critique", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--subtitle", default="")
    a = ap.parse_args()
    research(a.topic, a.angle,
             day_tags=[t.strip() for t in a.tags.split(",")] if a.tags else None,
             critique=not a.no_critique, pdf=not a.no_pdf, subtitle=a.subtitle)


if __name__ == "__main__":
    main()
