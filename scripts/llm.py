"""模型层：通用 OpenAI 兼容接口(DeepSeek / Kimi / OpenAI 均可)。

配置来自 config/ai_config.json 或环境变量 AI_BASE_URL / AI_API_KEY / AI_MODEL。
提供：complete(单次)、complete_json(结构化)、agent_loop(工具循环)。
"""
import json
import os
import re
import time
import base64
import ctypes
import getpass
import subprocess
import sys
from ctypes import wintypes

import requests

ROOT = os.environ.get("SHIGUANG_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config", "ai_config.json")
SECRET_PATH = os.path.join(ROOT, "config", "ai_secret.dat")
KEYCHAIN_SERVICE = "shiguang.ai.api-key"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data, decrypt=False):
    """用当前 Windows 用户的 DPAPI 加/解密，密文无法搬到另一台电脑直接使用。"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅在 Windows 可用")
    raw = base64.b64decode(data) if decrypt else data.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    src = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                        flags, ctypes.byref(dst))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "拾光", None, None, None,
                                      flags, ctypes.byref(dst))
    if not ok:
        raise ctypes.WinError()
    try:
        result = ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)
    return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load_secret():
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-a", getpass.getuser(),
                 "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True,
                timeout=10, check=True)
            return r.stdout.strip()
        except Exception:
            return ""
    if not os.path.exists(SECRET_PATH):
        return ""
    try:
        with open(SECRET_PATH, encoding="ascii") as f:
            token = f.read().strip()
        return _dpapi(token, decrypt=True)
    except Exception:
        return ""


def _store_secret(api_key):
    if os.name == "nt":
        with open(SECRET_PATH, "w", encoding="ascii") as f:
            f.write(_dpapi(api_key))
        return
    if sys.platform == "darwin":
        subprocess.run(
            ["security", "add-generic-password", "-a", getpass.getuser(),
             "-s", KEYCHAIN_SERVICE, "-w", api_key, "-U"],
            capture_output=True, text=True, timeout=15, check=True)
        return
    # 其它开发环境的降级方案；只允许当前用户读取。
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(api_key)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass


def save_cfg(base_url="", model="", api_key=None):
    """保存非敏感模型设置；密钥单独交给 Windows DPAPI。api_key=None 表示保留。"""
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    old = _read_json(CFG_PATH)
    cfg = {
        "base_url": (base_url or old.get("base_url", "")).strip(),
        "model": (model or old.get("model", "")).strip(),
    }
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    if api_key is not None and api_key.strip():
        _store_secret(api_key.strip())
    return {"base_url": cfg["base_url"], "model": cfg["model"],
            "configured": bool(cfg["base_url"] and cfg["model"] and _load_secret())}


def migrate_legacy_secret():
    """把旧版 ai_config.json 中的明文 key 原地迁移为 DPAPI 密文。"""
    cfg = _read_json(CFG_PATH)
    key = str(cfg.get("api_key", "") or "").strip()
    if key:
        save_cfg(cfg.get("base_url", ""), cfg.get("model", ""), key)
        return True
    return False


def load_cfg():
    cfg = _read_json(CFG_PATH)
    base = os.environ.get("AI_BASE_URL", cfg.get("base_url", "")).strip()
    # 兼容旧版明文配置，但新保存会自动移除它。
    key = os.environ.get("AI_API_KEY", "").strip() or _load_secret() or str(cfg.get("api_key", "")).strip()
    model = os.environ.get("AI_MODEL", cfg.get("model", "")).strip()
    return base, key, model


def public_cfg():
    base, key, model = load_cfg()
    return {"base_url": base, "model": model, "configured": bool(base and key and model),
            "key_hint": ("••••" + key[-4:]) if key else ""}


def configured():
    base, key, model = load_cfg()
    return bool(base and key and model)


def require_cfg():
    base, key, model = load_cfg()
    if not (base and key and model):
        raise SystemExit(
            "[-] 还没配置模型接口。复制 config/ai_config.example.json 为 "
            "config/ai_config.json 并填 base_url / api_key / model。\n"
            "    DeepSeek: base_url=https://api.deepseek.com/v1  model=deepseek-chat")
    return base.rstrip("/") + "/chat/completions", key, model


def _post(messages, tools=None, json_mode=False, temperature=0.4,
          max_tokens=4096, retries=3, model=None):
    endpoint, key, cfg_model = require_cfg()
    body = {"model": model or cfg_model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for i in range(retries):
        try:
            r = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=body, timeout=240)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            last = f"{r.status_code}: {r.text[:300]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break  # 客户端错误(如内容审核)不重试
        except Exception as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"接口调用失败：{last}")


def parse_json(text):
    """从模型输出里稳妥地抽出 JSON。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except Exception:
            return None
    return None


def complete(system, user, temperature=0.4, max_tokens=4096, model=None):
    msg = _post(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens, model=model)
    return msg.get("content", "") or ""


def complete_json(system, user, temperature=0.2, max_tokens=4096, model=None):
    """结构化输出，返回 dict(失败返回 None)。提示词里请出现 JSON 字样。
    若接口不支持 response_format，自动降级为普通调用再解析。"""
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    try:
        msg = _post(msgs, json_mode=True, temperature=temperature,
                    max_tokens=max_tokens, model=model)
        d = parse_json(msg.get("content", ""))
        if d is not None:
            return d
    except RuntimeError:
        pass
    # 降级：不用 json 模式，明确要求只输出 JSON
    msgs[0]["content"] += "\n注意：请只输出合法 JSON，不要任何额外文字或代码块标记。"
    msg = _post(msgs, temperature=temperature, max_tokens=max_tokens, model=model)
    return parse_json(msg.get("content", ""))


def agent_loop(system, user, tools_spec, tool_impl, max_steps=14,
               on_tool=None, temperature=0.4, max_tokens=4096, model=None,
               history=None):
    """给模型一组工具，让它自己反复调用直到给出答案。
    tools_spec: OpenAI function 定义列表; tool_impl: name->func(args)->str。"""
    messages = [{"role": "system", "content": system}]
    for h in (history or [])[-12:]:
        role = h.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": str(h.get("content", ""))[:6000]})
    messages.append({"role": "user", "content": user})
    for _ in range(max_steps):
        msg = _post(messages, tools=tools_spec, temperature=temperature,
                    max_tokens=max_tokens, model=model)
        messages.append(msg)
        tcs = msg.get("tool_calls")
        if not tcs:
            return msg.get("content", "") or ""
        for tc in tcs:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if on_tool:
                on_tool(name, args)
            fn = tool_impl.get(name)
            result = fn(args) if fn else json.dumps(
                {"error": f"未知工具 {name}"}, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": result})
    return "（推理轮数用尽，请缩小问题范围重试）"
