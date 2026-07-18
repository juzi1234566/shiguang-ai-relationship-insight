"""Create a fully synthetic relationship profile for screenshots and review."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from chat_import import write_chat_db
from profile_store import ProfileStore


CN = timezone(timedelta(hours=8))
PAIR_LINES = [
    ("me", "今天下班后要不要一起去散步？"),
    ("her", "好呀，正好想和你聊聊最近的计划。"),
    ("me", "上次你推荐的书我看完了，最喜欢第三章。"),
    ("her", "我就知道你会喜欢！周末交换一下读书笔记吧。"),
    ("me", "明天降温，记得带外套。"),
    ("her", "收到，你也别忙到忘记吃饭。"),
]


def build_messages(days=14):
    now = datetime.now(CN).replace(minute=0, second=0, microsecond=0)
    messages = []
    for offset in range(days - 1, -1, -1):
        day = now - timedelta(days=offset)
        for index, (sender, content) in enumerate(PAIR_LINES):
            stamp = day.replace(hour=9 + index * 2)
            messages.append({
                "ts": int(stamp.timestamp()), "sender": sender,
                "type": "text", "content": content,
            })
    return messages


def seed_summaries(db_path: Path, days=14):
    now = datetime.now(CN)
    con = sqlite3.connect(db_path)
    try:
        for offset in range(days - 1, -1, -1):
            date = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            narrative = (
                "两个人围绕工作节奏、读书和周末安排保持轻松交流，"
                "既分享日常，也会主动提醒对方照顾自己。"
            )
            con.execute(
                """INSERT OR REPLACE INTO daily_summary(
                    date,me_count,her_count,narrative,topics,mood_me,mood_her,
                    events,her_reflection,quotes,tags,model,built_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date, 3, 3, narrative,
                 json.dumps(["日常分享", "读书", "周末计划"], ensure_ascii=False),
                 "平稳、关心", "轻松、期待",
                 json.dumps(["约定周末交换读书笔记"], ensure_ascii=False),
                 "愿意回应计划，也会表达对对方状态的关心。",
                 json.dumps(["周末交换一下读书笔记吧"], ensure_ascii=False),
                 json.dumps(["日常", "支持", "成长"], ensure_ascii=False),
                 "synthetic-demo", int(now.timestamp())),
            )
        month = now.strftime("%Y-%m")
        con.execute(
            """INSERT OR REPLACE INTO monthly_summary(
                month,narrative,mood_trend,growth,topics,key_days,model,built_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (month, "这个月的互动稳定而有来有往，共同兴趣让交流更具体。",
             "整体平稳，期待感逐步增加", "从泛泛问候转向共同计划",
             json.dumps(["阅读", "生活安排", "互相照顾"], ensure_ascii=False),
             json.dumps([now.strftime("%Y-%m-%d")], ensure_ascii=False),
             "synthetic-demo", int(now.timestamp())),
        )
        con.execute(
            """INSERT OR REPLACE INTO yearly_summary(
                id,narrative,themes,month_index,model,built_at
            ) VALUES(1,?,?,?,?,?)""",
            ("一段由日常关心、共同兴趣和小计划组成的稳定关系。",
             json.dumps(["支持", "分享", "共同成长"], ensure_ascii=False),
             json.dumps({month: "稳定交流与共同计划"}, ensure_ascii=False),
             "synthetic-demo", int(now.timestamp())),
        )
        con.commit()
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description="创建拾光合成演示数据")
    parser.add_argument("--root", default=str(HERE.parent / ".demo-data"))
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    store = ProfileStore(str(root))
    profile = next((p for p in store.list_profiles()
                    if p["display_name"] == "小满"), None)
    if not profile:
        profile = store.create(
            "小满", "friend",
            "大学时期认识的朋友，现在保持阅读与生活分享。",
            "general", "synthetic-contact",
        )
    messages = build_messages()
    info = write_chat_db(profile["data_root"], messages)
    seed_summaries(Path(info["path"]))
    profile = store.update(
        profile["id"], import_source="synthetic-demo", import_status="ready",
        message_count=info["message_count"], date_from=info["date_from"],
        date_to=info["date_to"],
    )
    store.set_active(profile["id"])

    os.environ["SHIGUANG_ROOT"] = profile["data_root"]
    import appdb
    appdb.init()
    appdb.update_settings({
        "user_name": "我", "partner_name": "小满",
        "relationship_type": "friend",
        "relationship_note": "合成演示关系，不对应任何真实人物。",
    })
    conversation = appdb.create_conversation("我们最近有哪些共同话题？")
    appdb.add_message(conversation, "user", "我们最近有哪些共同话题？")
    appdb.add_message(
        conversation, "assistant",
        "从合成记录看，最近主要围绕阅读、周末安排和互相照顾展开。",
        model="synthetic-demo",
    )
    print(json.dumps({"root": str(root), "profile": profile,
                      "messages": len(messages)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
