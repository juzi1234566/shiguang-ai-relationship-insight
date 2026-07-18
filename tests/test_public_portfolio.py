import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import appdb
import chat_import
from runtime_ports import ExclusiveThreadingHTTPServer


class PublicPortfolioTests(unittest.TestCase):
    def test_app_conversations_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = appdb.DB
            try:
                appdb.DB = str(Path(tmp) / "app.db")
                appdb._READY = False
                appdb.init()
                cid = appdb.create_conversation("测试")
                appdb.add_message(cid, "user", "你好")
                appdb.add_message(cid, "assistant", "在呢")
                data = appdb.get_conversation(cid)
                self.assertEqual(
                    [item["content"] for item in data["messages"]],
                    ["你好", "在呢"],
                )
            finally:
                appdb.DB = old_db
                appdb._READY = False

    def test_json_import_builds_an_isolated_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = [
                {"ts": 1700000000, "sender": "me", "type": "text", "content": "你好"},
                {"ts": 1700000300, "sender": "her", "type": "text", "content": "晚上好"},
            ]
            result = chat_import.write_chat_db(tmp, messages)
            con = sqlite3.connect(result["path"])
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            finally:
                con.close()

    def test_synthetic_demo_contains_no_real_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, str(SCRIPTS / "create_demo.py"),
                 "--root", tmp, "--reset"],
                check=True, capture_output=True, text=True,
            )
            registry = sqlite3.connect(Path(tmp) / "data" / "profiles.db")
            try:
                profile = registry.execute(
                    "SELECT display_name,data_root,message_count FROM profiles"
                ).fetchone()
            finally:
                registry.close()
            self.assertEqual(profile[0], "小满")
            self.assertGreater(profile[2], 50)
            chat = sqlite3.connect(Path(profile[1]) / "data" / "clean" / "chat.db")
            try:
                self.assertGreater(
                    chat.execute("SELECT count(*) FROM daily_summary").fetchone()[0], 7
                )
            finally:
                chat.close()

    def test_local_server_uses_exclusive_port_binding(self):
        self.assertFalse(ExclusiveThreadingHTTPServer.allow_reuse_address)

    def test_repository_has_no_private_runtime_artifacts(self):
        forbidden_suffixes = {".db", ".dat", ".exe", ".pdf", ".key", ".pem"}
        ignored_parts = {".git", ".demo-data", "output", "__pycache__"}
        files = [path for path in ROOT.rglob("*") if path.is_file()
                 and not ignored_parts.intersection(path.parts)]
        self.assertFalse([path for path in files if path.suffix.lower() in forbidden_suffixes])
        source = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in files if path.suffix.lower() in {".py", ".md", ".html", ".yml"}
        )
        markers = (
            "wx" + "id_",
            "C:" + "\\Users\\",
            "BEGIN " + "PRIVATE KEY",
            "github" + "_pat_",
        )
        for marker in markers:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
