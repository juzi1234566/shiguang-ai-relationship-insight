"""Public portfolio adapter for chat sources.

The production project contains a Windows-only native WeChat importer. Its key
recovery implementation is intentionally not published because this repository
is designed for portfolio review and synthetic/local JSON demos. The rest of
the application talks to this small interface, so another consent-based source
adapter can be implemented without changing the product layer.
"""


NOTICE = (
    "公开作品集版未包含原生微信密钥恢复。请使用合成演示数据，"
    "或在首次向导中选择自己合法导出的聊天 JSON。"
)


def status():
    return {
        "connected": False,
        "stage": "public_demo",
        "message": NOTICE,
        "accounts": [],
        "default_account": "",
    }


def prepare(*_args, **_kwargs):
    return status()


def list_contacts(*_args, **_kwargs):
    return {"status": "error", "message": NOTICE, "contacts": []}


def fetch_contact_messages(*_args, **_kwargs):
    raise RuntimeError(NOTICE)


def choose_data_dir():
    return ""
