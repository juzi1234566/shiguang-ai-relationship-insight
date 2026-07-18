import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_TEMP_ROOT = tempfile.mkdtemp(prefix="shiguang-public-test-")
os.environ["SHIGUANG_APP_HOME"] = _TEMP_ROOT

import server


class LocalHttpSecurityTests(unittest.TestCase):
    def test_only_same_origin_loopback_requests_are_authorized(self):
        check = server._request_is_authorized
        self.assertTrue(check(
            "127.0.0.1:18756", "http://127.0.0.1:18756",
            "shiguang_session=secret", "secret",
        ))
        self.assertFalse(check(
            "attacker.example:18756", "http://attacker.example:18756",
            "shiguang_session=secret", "secret",
        ))
        self.assertFalse(check(
            "127.0.0.1:18756", "https://attacker.example",
            "shiguang_session=secret", "secret",
        ))

    def test_request_size_is_bounded(self):
        for value in ("garbage", "-1", str(2 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                server._parse_content_length(value)
        self.assertEqual(server._parse_content_length("42"), 42)


if __name__ == "__main__":
    unittest.main()
