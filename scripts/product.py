"""产品版本配置。两个 Windows 版本共享代码，只改变默认关系模板和品牌文案。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


EDITIONS = {
    "general": {
        "id": "general",
        "name": "拾光关系",
        "tagline": "读懂每一段重要关系",
        "default_relation": "friend",
        "accent": "#516b63",
    },
    "couple": {
        "id": "couple",
        "name": "拾光情侣",
        "tagline": "把我们走过的日子重新看见",
        "default_relation": "couple",
        "accent": "#b4473d",
    },
}


def load(asset_root=None):
    edition = os.environ.get("SHIGUANG_EDITION", "").strip().lower()
    build_id = os.environ.get("SHIGUANG_BUILD_ID", "").strip()
    roots = [Path(asset_root)] if asset_root else []
    roots.extend([Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parent.parent])
    if not edition or not build_id:
        for root in roots:
            if not str(root):
                continue
            path = root / "_edition.json"
            try:
                packaged = json.loads(path.read_text(encoding="utf-8"))
                edition = edition or str(packaged.get("edition") or "")
                build_id = build_id or str(packaged.get("build_id") or "")
                if edition and build_id:
                    break
            except (OSError, ValueError):
                pass
    result = dict(EDITIONS.get(edition, EDITIONS["general"]))
    result["build_id"] = build_id or "development"
    return result
