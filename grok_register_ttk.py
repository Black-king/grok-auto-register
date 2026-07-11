#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import datetime
import time
import os
import sys
import gc
import queue
import secrets
import struct
import random
import re
import string
import json

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

import browser_profile as bp
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
MEMORY_CLEANUP_INTERVAL = 5

# ===== 配色（深色仪表盘 · 青色强调） =====
UI_BG = "#0e1116"          # 应用底色
UI_HEADER_BG = "#0b0e12"   # 顶栏
UI_PANEL_BG = "#161b22"    # 卡片
UI_PANEL_ALT = "#1c232d"   # 次级卡片 / 输入区
UI_BORDER = "#2a3441"      # 边框
UI_FG = "#e6edf3"          # 正文
UI_MUTED_FG = "#8b95a1"    # 次要文字
UI_ENTRY_BG = "#1c232d"    # 输入框
UI_BUTTON_BG = "#222b36"   # 次要按钮
UI_ACTIVE_BG = "#243447"   # 悬停 / 选中
UI_ACCENT = "#3fb6c9"      # 强调青
UI_ACCENT_HOVER = "#57d0e3"
UI_ACCENT_DIM = "#2b6f7d"
UI_ON_ACCENT = "#06222a"   # 强调色上的文字
UI_SUCCESS = "#3fb950"
UI_WARN = "#d29922"
UI_ERROR = "#f85149"
UI_DEBUG = "#6e7681"
UI_CPA = "#a371f7"
UI_LOG_BG = "#0a0d11"      # 日志底色

UI_FONT = "Segoe UI"
UI_MONO_FONT = "Cascadia Code"

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "templol_api_key": "",
    "templol_domains": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "proxy": "http://127.0.0.1:7890",
    "proxy_mode": "single",
    "proxy_pool": [],
    "proxy_pool_strategy": "rotate",
    "anti_fingerprint": True,
    "max_ip_retry": 3,
    "concurrency": 1,
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    "cpa_export_enabled": True,
    "cpa_auth_dir": "cpa_auths",
    "cpa_proxy": "",
    "cpa_headless": False,
    "cpa_probe_after_write": True,
    "cpa_mint_timeout_sec": 240,
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
    "cpa_force_standalone": False,
    "cpa_mint_cookie_inject": True,
    "cpa_mint_browser_reuse": True,
    "cpa_mint_browser_recycle_every": 15,
    "cpa_hotload_dir": "",
    "cpa_copy_to_hotload": False,
    "cpa_server_host": "",
    "cpa_server_user": "root",
    "cpa_server_password": "",
    "cpa_server_auth_dir": "",
    "token_only_file": "",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_proxy_pool = None
_output_lock = threading.Lock()  # 保护 accounts/token.json/tokens.txt/mail_credentials 并发写入

# 每个 worker 线程各自持有独立的浏览器 / 页面 / 画像（并发注册用）
_tls = threading.local()


def get_browser():
    return getattr(_tls, "browser", None)


def set_browser(value):
    _tls.browser = value


def get_page():
    return getattr(_tls, "page", None)


def set_page(value):
    _tls.page = value


def get_profile():
    return getattr(_tls, "profile", None)


def set_profile(value):
    _tls.profile = value


def worker_tag():
    return getattr(_tls, "tag", "")


# 所有存活浏览器的登记表，供「停止」时强制关闭以打断阻塞中的浏览器操作
_live_browsers = set()
_live_lock = threading.Lock()


def _register_browser(b):
    if b is not None:
        with _live_lock:
            _live_browsers.add(b)


def _unregister_browser(b):
    if b is not None:
        with _live_lock:
            _live_browsers.discard(b)


def force_stop_all_browsers():
    """强制关闭所有存活浏览器，让卡在导航/等待里的 worker 立刻报错退出。"""
    with _live_lock:
        browsers = list(_live_browsers)
        _live_browsers.clear()
    for b in browsers:
        try:
            b.quit(del_data=True)
        except Exception:
            pass


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


class ProxySwitchNeeded(Exception):
    """当前 IP 打不开网页或过不了 Cloudflare，需要切换到下一个代理重试。"""
    pass


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    proxy = ""
    _profile = get_profile()
    if _profile:
        proxy = str(_profile.get("proxy", "") or "").strip()
    if not proxy:
        proxy = str(config.get("proxy", "") or "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return os.path.join(os.path.dirname(__file__), "token.json")


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def get_grok2api_remote_api_bases(base):
    """生成 grok2api 管理 API 候选根路径。

    参数:
      - base str: 用户配置的 grok2api 远端地址

    返回:
      - list[str]: 依次尝试的管理 API 根路径
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return []
    lower = normalized.lower()
    candidates = [normalized]
    if lower.endswith("/admin/api"):
        return candidates
    if lower.endswith("/admin"):
        candidates.append(f"{normalized}/api")
    else:
        candidates.append(f"{normalized}/admin/api")
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    api_bases = get_grok2api_remote_api_bases(base)
    add_errors = []
    # 优先使用 add 接口，避免全量覆盖远端池
    add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
    for api_base in api_bases:
        endpoint = f"{api_base}/tokens/add"
        try:
            resp_add = http_post(
                endpoint,
                headers=headers,
                params=query,
                json=add_payload,
                timeout=30,
                proxies={},
            )
            resp_add.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({endpoint})")
            return True
        except Exception as add_exc:
            add_errors.append(f"{endpoint}: {add_exc}")
    if log_callback:
        log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {'; '.join(add_errors)}")

    # 兜底：旧版全量保存接口
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20, proxies={})
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    save_errors = []
    save_bases = []
    for item in [fallback_base, *(api_bases or [base])]:
        if item and item not in save_bases:
            save_bases.append(item)
    for api_base in save_bases:
        try:
            resp2 = http_post(f"{api_base}/tokens", headers=headers, params=query, json=current, timeout=30, proxies={})
            resp2.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({api_base}/tokens)")
            return True
        except Exception as save_exc:
            save_errors.append(f"{api_base}/tokens: {save_exc}")
    raise RuntimeError(f"grok2api 远端 /tokens 全量模式写入失败: {'; '.join(save_errors)}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    with _output_lock:
        if config.get("grok2api_auto_add_local", True):
            try:
                add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
        if config.get("grok2api_auto_add_remote", False):
            try:
                add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def add_token_to_token_only_file(raw_token, log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_only_file = str(config.get("token_only_file", "") or "").strip()
    if not token_only_file:
        token_only_file = os.path.join(os.path.dirname(__file__), "tokens.txt")
    try:
        with _output_lock:
            with open(token_only_file, "a", encoding="utf-8") as f:
                f.write(f"{token}\n")
        if log_callback:
            log_callback(f"[+] 已写入 token 文件: {token_only_file}")
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 写入 token 文件失败: {exc}")
        return False


def upload_to_cpa_server(local_path, log_callback=None):
    host = str(config.get("cpa_server_host", "") or "").strip()
    user = str(config.get("cpa_server_user", "root") or "root").strip()
    password = str(config.get("cpa_server_password", "") or "").strip()
    remote_dir = str(config.get("cpa_server_auth_dir", "") or "").strip()
    if not host or not remote_dir:
        return False
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=15)
        sftp = ssh.open_sftp()
        filename = os.path.basename(local_path)
        remote_path = remote_dir.rstrip("/") + "/" + filename
        sftp.put(local_path, remote_path)
        try:
            sftp.chmod(remote_path, 0o600)
        except Exception:
            pass
        sftp.close()
        ssh.close()
        if log_callback:
            log_callback(f"[cpa] 已上传到服务器: {host}:{remote_path}")
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[cpa] 上传到服务器失败: {exc}")
        return False


def export_cpa_xai_for_account(email, password, sso=None, log_callback=None, page=None):
    if not config.get("cpa_export_enabled", True):
        if log_callback:
            log_callback("[cpa] CPA 导出已禁用，跳过")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    try:
        from cpa_export import export_cpa_xai_for_account as _export
        return _export(
            email, password,
            sso=sso,
            page=page,
            config=config,
            log_callback=log_callback,
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[cpa] CPA xAI 导出失败: {exc}")
        return {"ok": False, "error": str(exc)}


def anti_fingerprint_enabled():
    return bool(config.get("anti_fingerprint", True))


def create_browser_options():
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)

    profile = get_profile()
    proxy = ""
    if profile:
        proxy = str(profile.get("proxy", "") or "").strip()
    else:
        proxy = str(config.get("proxy", "") or "").strip()

    if anti_fingerprint_enabled() and profile:
        # 只随机安全维度：视口与语言。刻意不改 UA / 平台，保持与真实浏览器、
        # client hints、TLS 一致，避免触发 Cloudflare Turnstile。
        vw, vh = profile.get("viewport", [1920, 1080])
        options.set_argument(f"--window-size={vw},{vh}")
        options.set_argument(f"--lang={profile.get('lang', 'en-US')}")
        options.set_argument("--disable-blink-features=AutomationControlled")

    if proxy:
        server = bp.proxy_server_arg(proxy)
        if server:
            options.set_argument(f"--proxy-server={server}")
        # 带认证代理的用户名/密码通过 CDP Fetch 在页面上处理（见 ensure_proxy_auth），
        # 不再用扩展 service worker（新版 Chrome 上不可靠）。

    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取 token 失败: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 无已验证域名可用")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


TEMPLOL_API_BASE = "https://api.tempmail.lol/v2"


def get_templol_api_key():
    return str(config.get("templol_api_key", "") or "").strip()


def get_templol_domains():
    """读取 TempMail.lol 自定义域名，支持列表或逗号分隔字符串。"""
    raw = config.get("templol_domains", "")
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw or "").split(",")
    return [str(x).strip().lower() for x in items if str(x).strip()]


def templol_build_headers(content_type=False):
    headers = {"Accept": "application/json", "User-Agent": "grok-register/1.0"}
    if content_type:
        headers["Content-Type"] = "application/json"
    key = get_templol_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def templol_generate_subdomain():
    length = random.randint(4, 10)
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def templol_select_domain():
    """按配置随机选择域名；`*.example.com` 通配会生成随机子域名。"""
    domains = get_templol_domains()
    if not domains:
        return None, False
    domain = random.choice(domains)
    if domain.startswith("*.") and len(domain) > 2:
        return f"{templol_generate_subdomain()}.{domain[2:]}", True
    return domain, False


def templol_create_mailbox(username=None):
    payload = {}
    domain, _force_prefix = templol_select_domain()
    if domain:
        payload["domain"] = domain
    payload["prefix"] = username or generate_username(10)
    resp = http_post(
        f"{TEMPLOL_API_BASE}/inbox/create",
        json=payload,
        headers=templol_build_headers(content_type=True),
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"TempMail.lol 创建邮箱返回非JSON: {resp.text[:300]}")
    address = str(data.get("address") or "").strip()
    token = str(data.get("token") or "").strip()
    if not address or not token:
        raise Exception(f"TempMail.lol 创建邮箱缺少 address/token: {data}")
    return address, token


def templol_get_email_and_token():
    address, token = templol_create_mailbox()
    print(f"[*] 已创建 TempMail.lol 邮箱: {address}")
    return address, token


def _templol_pick_messages(data):
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        for key in ("emails", "messages", "data", "items", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
    return []


def templol_get_messages(token):
    resp = http_get(
        f"{TEMPLOL_API_BASE}/inbox",
        params={"token": token},
        headers=templol_build_headers(),
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"TempMail.lol inbox 返回非JSON: {resp.text[:300]}")
    return _templol_pick_messages(data)


def _templol_message_id(msg):
    for key in ("id", "_id", "token", "messageId"):
        value = msg.get(key)
        if value:
            return str(value)
    return "|".join(str(msg.get(k, "")) for k in ("from", "subject", "date"))


def _templol_message_text(msg):
    parts = []
    for field in ("text", "body", "content", "text_content", "raw", "intro", "snippet"):
        value = msg.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    html_value = msg.get("html")
    if html_value is None:
        html_value = msg.get("html_content")
    if isinstance(html_value, str):
        html_value = [html_value]
    if isinstance(html_value, (list, tuple)):
        for h in html_value:
            if isinstance(h, str) and h.strip():
                parts.append(re.sub(r"<[^>]+>", " ", h))
    return "\n".join(parts)


def templol_get_oai_code(
    token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = templol_get_messages(token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] TempMail.lol 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] TempMail.lol 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = _templol_message_id(msg)
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            combined = _templol_message_text(msg)
            subject = str(msg.get("subject", "") or "")
            if log_callback:
                log_callback(f"[Debug] TempMail.lol 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] TempMail.lol 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"TempMail.lol 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 无已验证域名可用")


def get_email_provider():
    return config.get("email_provider", "duckmail")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "templol":
        return templol_get_email_and_token()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "templol":
        return templol_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        raw = str(res.text or "")
    except Exception:
        raw = ""
    if raw:
        # gRPC/protobuf 等二进制响应体当文本会渲染成乱码方块，检测后改为摘要
        bad = sum(
            1
            for ch in raw
            if (ord(ch) < 0x20 and ch not in "\r\n\t")
            or ord(ch) == 0x7f
            or 0x80 <= ord(ch) < 0xA0
            or ord(ch) == 0xFFFD
        )
        if bad / len(raw) > 0.08:
            try:
                n = len(res.content)
            except Exception:
                n = len(raw)
            return f"<binary {n} bytes>"
    text = re.sub(r"\s+", " ", raw).strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def setup_light_theme(root):
    try:
        root.option_add("*Background", UI_BG)
        root.option_add("*Foreground", UI_FG)
        root.option_add("*selectBackground", UI_ACCENT_DIM)
        root.option_add("*selectForeground", UI_FG)
        root.option_add("*insertBackground", UI_ACCENT)
        root.option_add("*Menu.Background", UI_PANEL_ALT)
        root.option_add("*Menu.Foreground", UI_FG)
        root.option_add("*Menu.activeBackground", UI_ACCENT_DIM)
        root.option_add("*Menu.activeForeground", UI_FG)
        root.configure(bg=UI_BG)

        style = ttk.Style(root)
        available = set(style.theme_names())
        if "clam" in available:
            style.theme_use("clam")
        elif "default" in available:
            style.theme_use("default")

        style.configure(".", background=UI_BG, foreground=UI_FG,
                        fieldbackground=UI_ENTRY_BG, bordercolor=UI_BORDER,
                        focuscolor=UI_ACCENT)
        style.configure("TFrame", background=UI_BG)
        style.configure("Card.TFrame", background=UI_PANEL_BG)
        style.configure("TLabel", background=UI_BG, foreground=UI_FG, font=(UI_FONT, 10))

        # 配置区 Tab
        style.configure("TNotebook", background=UI_PANEL_BG, borderwidth=0,
                        tabmargins=(4, 6, 4, 0))
        style.configure("TNotebook.Tab", background=UI_PANEL_BG, foreground=UI_MUTED_FG,
                        padding=(18, 9), font=(UI_FONT, 10), borderwidth=0)
        style.map(
            "TNotebook.Tab",
            background=[("selected", UI_PANEL_ALT)],
            foreground=[("selected", UI_ACCENT), ("active", UI_FG)],
        )

        # 进度条
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=UI_PANEL_ALT,
                        background=UI_ACCENT, borderwidth=0, thickness=8)

        # 滚动条
        style.configure("Vertical.TScrollbar", background=UI_PANEL_ALT, troughcolor=UI_LOG_BG,
                        bordercolor=UI_LOG_BG, arrowcolor=UI_MUTED_FG, borderwidth=0)
        style.map("Vertical.TScrollbar", background=[("active", UI_ACCENT_DIM)])

        # 下拉框（只读 Combobox）
        style.configure(
            "Dark.TCombobox",
            fieldbackground=UI_ENTRY_BG,
            background=UI_ENTRY_BG,
            foreground=UI_FG,
            arrowcolor=UI_MUTED_FG,
            bordercolor=UI_BORDER,
            lightcolor=UI_BORDER,
            darkcolor=UI_BORDER,
            relief="flat",
            padding=6,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", UI_ENTRY_BG), ("disabled", UI_PANEL_BG)],
            foreground=[("readonly", UI_FG), ("disabled", UI_MUTED_FG)],
            arrowcolor=[("disabled", UI_DEBUG), ("active", UI_ACCENT)],
            bordercolor=[("focus", UI_ACCENT), ("hover", UI_ACCENT_DIM)],
            selectbackground=[("readonly", UI_ENTRY_BG)],
            selectforeground=[("readonly", UI_FG)],
        )
        # 弹出列表
        root.option_add("*TCombobox*Listbox.background", UI_PANEL_ALT)
        root.option_add("*TCombobox*Listbox.foreground", UI_FG)
        root.option_add("*TCombobox*Listbox.selectBackground", UI_ACCENT_DIM)
        root.option_add("*TCombobox*Listbox.selectForeground", UI_FG)
        root.option_add("*TCombobox*Listbox.font", (UI_FONT, 10))
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
        root.option_add("*TCombobox*Listbox.relief", "flat")
    except Exception:
        pass


def tk_label(parent, text="", bg=UI_PANEL_BG, fg=UI_FG, **kwargs):
    kwargs.setdefault("font", (UI_FONT, 10))
    return tk.Label(parent, text=text, bg=bg, fg=fg, **kwargs)


def tk_entry(parent, textvariable=None, width=30, **kwargs):
    return tk.Entry(
        parent,
        textvariable=textvariable,
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_ACCENT,
        disabledbackground=UI_PANEL_BG,
        disabledforeground=UI_MUTED_FG,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        highlightcolor=UI_ACCENT,
        relief=tk.FLAT,
        font=(UI_FONT, 10),
        **kwargs,
    )


def tk_checkbutton(parent, text="", variable=None, bg=UI_PANEL_BG, **kwargs):
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg=bg,
        fg=UI_FG,
        activebackground=bg,
        activeforeground=UI_ACCENT,
        selectcolor=UI_PANEL_ALT,
        font=(UI_FONT, 10),
        highlightthickness=0,
        bd=0,
        anchor="w",
        cursor="hand2",
        **kwargs,
    )


def tk_option_menu(parent, variable, values, width=12):
    menu = tk.OptionMenu(parent, variable, *values)
    menu.configure(
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        activebackground=UI_ACCENT_DIM,
        activeforeground=UI_FG,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        relief=tk.FLAT,
        font=(UI_FONT, 10),
        anchor="w",
        cursor="hand2",
    )
    menu["menu"].configure(
        bg=UI_PANEL_ALT,
        fg=UI_FG,
        activebackground=UI_ACCENT_DIM,
        activeforeground=UI_FG,
        bd=0,
    )
    return menu


def tk_combo(parent, variable, values, width=14):
    """只读下拉框，深色扁平样式（替代原生 OptionMenu）。"""
    cb = ttk.Combobox(
        parent,
        textvariable=variable,
        values=list(values),
        state="readonly",
        width=width,
        style="Dark.TCombobox",
        font=(UI_FONT, 10),
    )
    return cb


def tk_text(parent, height=4, width=30):
    """多行文本输入框，深色扁平样式（用于代理池等多行内容）。"""
    return tk.Text(
        parent,
        height=height,
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_ACCENT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        highlightcolor=UI_ACCENT,
        relief=tk.FLAT,
        bd=0,
        wrap="none",
        padx=8,
        pady=6,
        font=(UI_MONO_FONT, 10),
    )



def make_button(parent, text="", command=None, primary=False, state=tk.NORMAL, width=None):
    """扁平化按钮 + 悬停高亮。primary=True 为青色主按钮。"""
    base = UI_ACCENT if primary else UI_BUTTON_BG
    fg = UI_ON_ACCENT if primary else UI_FG
    hover = UI_ACCENT_HOVER if primary else UI_ACTIVE_BG
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        state=state,
        bg=base,
        fg=fg,
        activebackground=hover,
        activeforeground=fg,
        disabledforeground=UI_MUTED_FG,
        relief=tk.FLAT,
        bd=0,
        padx=16,
        pady=9,
        cursor="hand2",
        font=(UI_FONT, 10, "bold") if primary else (UI_FONT, 10),
    )
    if width:
        btn.configure(width=width)
    btn._base_bg = base
    btn._hover_bg = hover

    def on_enter(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=hover)

    def on_leave(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=base)

    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn


def tk_button(parent, text="", command=None, state=tk.NORMAL, **kwargs):
    # 兼容旧调用；委托到扁平按钮
    return make_button(parent, text=text, command=command, state=state)


def tk_spinbox(parent, textvariable=None, from_=1, to=100, width=8):
    return tk.Spinbox(
        parent,
        from_=from_,
        to=to,
        width=width,
        textvariable=textvariable,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_ACCENT,
        buttonbackground=UI_BUTTON_BG,
        disabledbackground=UI_PANEL_BG,
        disabledforeground=UI_MUTED_FG,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        highlightcolor=UI_ACCENT,
        relief=tk.FLAT,
        font=(UI_FONT, 10),
    )


def _blend_hex(c1, c2, t):
    """在两个 #rrggbb 之间线性插值，t∈[0,1]。"""
    a = c1.lstrip("#")
    b = c2.lstrip("#")
    r1, g1, b1 = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    r2, g2, b2 = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    bl = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _sanitize_log_text(s):
    """去除控制/不可打印字符，避免二进制内容在日志里显示成乱码方块。"""
    out = []
    for ch in str(s):
        if ch == "\t":
            out.append("    ")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f or ord(ch) == 0xfffd:
            continue
        else:
            out.append(ch)
    return "".join(out)




def init_proxy_pool(log_callback=None):
    """根据 config 初始化全局代理池。"""
    global _proxy_pool
    _proxy_pool = bp.ProxyPool(
        mode=config.get("proxy_mode", "single"),
        single=config.get("proxy", ""),
        pool=config.get("proxy_pool", []),
        strategy=config.get("proxy_pool_strategy", "rotate"),
    )
    if log_callback:
        if _proxy_pool.mode == "pool":
            log_callback(f"[*] 代理模式: 代理池({_proxy_pool.size()} 个) 策略={_proxy_pool.strategy}")
        else:
            label = bp.proxy_log_label(_proxy_pool.single) if _proxy_pool.single else "(直连)"
            log_callback(f"[*] 代理模式: 单一代理 {label}")
    return _proxy_pool


def start_new_account_profile(log_callback=None, cancel=None):
    """每个账号开始时调用：领取代理 + 生成新指纹画像，存到线程本地画像。"""
    global _proxy_pool
    if _proxy_pool is None:
        init_proxy_pool(log_callback=log_callback)
    # 释放上一个账号的临时扩展与占用的代理
    prev = get_profile()
    if prev is not None:
        bp.cleanup_profile(prev)
        if _proxy_pool:
            _proxy_pool.release(prev.get("proxy"))
    proxy = _proxy_pool.acquire(cancel=cancel) if _proxy_pool else config.get("proxy", "")
    profile = bp.build_account_profile(proxy=proxy)
    set_profile(profile)
    if log_callback:
        if anti_fingerprint_enabled():
            log_callback(f"[*] 本账号浏览器画像: {bp.profile_summary(profile)}")
        else:
            log_callback(f"[*] 本账号出口 IP: {bp.proxy_log_label(profile.get('proxy'))} (指纹随机化已关闭)")
    return profile


def switch_to_next_ip(log_callback=None, cancel=None):
    """标记当前 IP 失效并换到下一个，重建画像（沿用指纹随机化设置）。"""
    global _proxy_pool
    if _proxy_pool is None:
        init_proxy_pool(log_callback=log_callback)
    prev = get_profile()
    old_proxy = prev.get("proxy") if prev else None
    if prev is not None:
        bp.cleanup_profile(prev)
    if old_proxy and _proxy_pool:
        _proxy_pool.mark_bad(old_proxy)  # 内部会移出占用集合
    proxy = _proxy_pool.acquire(cancel=cancel) if _proxy_pool else None
    profile = bp.build_account_profile(proxy=proxy)
    set_profile(profile)
    if log_callback:
        log_callback(f"[*] 已切换出口 IP: {bp.proxy_log_label(old_proxy)} -> {bp.proxy_log_label(proxy)}")
    return profile


def release_current_proxy():
    """worker 结束时释放它占用的代理。"""
    prof = get_profile()
    if prof is not None and _proxy_pool:
        _proxy_pool.release(prof.get("proxy"))


def ensure_proxy_auth(target_page, log_callback=None):
    """用 CDP Fetch 处理带认证代理的用户名/密码（比扩展 SW 更可靠，且新版 Chrome MV2 已失效）。

    在每个标签页上启用 Fetch.enable(handleAuthRequests)，收到 authRequired 时
    用 continueWithAuth 提供凭据，其余请求直接 continueRequest 放行。
    """
    if target_page is None:
        return
    profile = get_profile()
    proxy = str((profile or {}).get("proxy", "") or "").strip()
    if not proxy:
        proxy = str(config.get("proxy", "") or "").strip()
    info = bp.parse_proxy(proxy)
    if not info or not info.get("username"):
        return  # 无认证代理无需处理
    if getattr(target_page, "_proxy_auth_applied", False):
        return
    user = info["username"]
    pw = info["password"]
    try:
        driver = target_page.driver

        def _on_auth(**kw):
            try:
                driver.run(
                    "Fetch.continueWithAuth",
                    requestId=kw.get("requestId"),
                    authChallengeResponse={
                        "response": "ProvideCredentials",
                        "username": user,
                        "password": pw,
                    },
                )
            except Exception:
                pass

        def _on_paused(**kw):
            try:
                driver.run("Fetch.continueRequest", requestId=kw.get("requestId"))
            except Exception:
                pass

        driver.set_callback("Fetch.authRequired", _on_auth)
        driver.set_callback("Fetch.requestPaused", _on_paused)
        target_page.run_cdp("Fetch.enable", handleAuthRequests=True, patterns=[{"urlPattern": "*"}])
        try:
            target_page._proxy_auth_applied = True
        except Exception:
            pass
        if log_callback:
            log_callback(f"[Debug] 已启用代理认证(CDP): {bp.proxy_log_label(proxy)}")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 启用代理认证失败: {exc}")


def ensure_stealth(target_page, log_callback=None):
    """对指定标签页幂等注入 stealth 脚本（每个新文档加载前执行），并处理代理认证。"""
    ensure_proxy_auth(target_page, log_callback=log_callback)
    _profile = get_profile()
    if not anti_fingerprint_enabled() or not _profile or target_page is None:
        return
    if getattr(target_page, "_stealth_applied", False):
        return
    try:
        script = bp.build_stealth_script(_profile)
        target_page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=script)
        try:
            target_page._stealth_applied = True
        except Exception:
            pass
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 注入 stealth 脚本失败: {exc}")


def start_browser(log_callback=None):
    last_exc = None
    for attempt in range(1, 5):
        try:
            browser = Chromium(create_browser_options())
            set_browser(browser)
            _register_browser(browser)
            tabs = browser.get_tabs()
            page = tabs[-1] if tabs else browser.new_tab()
            set_page(page)
            ensure_stealth(page, log_callback=log_callback)
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                _b = get_browser()
                if _b is not None:
                    _unregister_browser(_b)
                    _b.quit(del_data=True)
            except Exception:
                pass
            set_browser(None)
            set_page(None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    browser = get_browser()
    if browser is not None:
        _unregister_browser(browser)
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    set_browser(None)
    set_page(None)


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")


def refresh_active_page():
    browser = get_browser()
    if browser is None:
        restart_browser()
        browser = get_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
        set_page(page)
        ensure_stealth(page)
    except Exception:
        restart_browser()
    return get_page()


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    page = get_page()
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


CF_BLOCK_MARKERS = (
    "just a moment",
    "verifying you are human",
    "verify you are human",
    "attention required",
    "checking your browser",
    "cf-browser-verification",
    "cf-challenge",
    "请稍候",
    "正在验证",
    "稍等",
)


def _page_probe(target_page):
    try:
        url = str(target_page.url or "")
    except Exception:
        url = ""
    title = ""
    body = ""
    try:
        title = str(target_page.title or "")
    except Exception:
        pass
    try:
        body = target_page.run_js(
            "return (document.body ? document.body.innerText : '').slice(0, 2000);"
        ) or ""
    except Exception:
        body = ""
    return url, title, str(body)


def check_page_blocked(target_page, settle_seconds=8, cancel_callback=None, log_callback=None):
    """返回 'ok' | 'blocked' | 'unreachable'。短暂等待让 Cloudflare 挑战自解。"""
    deadline = time.time() + max(1, settle_seconds)
    last = "ok"
    url = title = ""
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        url, title, body = _page_probe(target_page)
        low = (title + "\n" + body).lower()
        if url.startswith("chrome-error") or url in ("", "about:blank"):
            last = "unreachable"
        elif any(m in low for m in CF_BLOCK_MARKERS):
            last = "blocked"
        else:
            return "ok"
        sleep_with_cancel(1.0, cancel_callback)
    if log_callback and last != "ok":
        log_callback(f"[Debug] 页面状态检测: {last} url={url} title={title[:60]}")
    return last


def _can_switch_ip():
    return _proxy_pool is not None and _proxy_pool.mode == "pool" and _proxy_pool.has_alternative()


def open_signup_page(log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    if get_browser() is None:
        start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    browser = get_browser()
    try:
        page = browser.get_tab(0)
        # 导航前先确保代理认证(CDP Fetch)已在该标签页启用，避免 407 弹框
        ensure_stealth(page, log_callback=log_callback)
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            page = browser.new_tab()
            ensure_stealth(page, log_callback=log_callback)
            page.get(SIGNUP_URL)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            browser = get_browser()
            page = browser.new_tab()
            ensure_stealth(page, log_callback=log_callback)
            page.get(SIGNUP_URL)
    set_page(page)
    ensure_stealth(page, log_callback=log_callback)
    page.wait.doc_loaded()
    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    status = check_page_blocked(
        page, cancel_callback=cancel_callback, log_callback=log_callback
    )
    if status != "ok":
        reason = "页面无法打开" if status == "unreachable" else "被 Cloudflare 拦截"
        if _can_switch_ip():
            raise ProxySwitchNeeded(reason)
        if log_callback:
            log_callback(f"[!] {reason}，且无可切换的备用 IP，继续尝试当前流程")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
    page = get_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    page = get_page()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", page.url if page else "") if isinstance(filled, dict) else (page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    page = get_page()
    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    page = get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        sleep_with_cancel(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0
    page = get_page()

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                if token_len == "0":
                    pause_seconds = random.uniform(1, 3)
                    if log_callback:
                        log_callback(f"[*] Cloudflare token 为空，暂停 {pause_seconds:.1f}s 后继续检测")
                    sleep_with_cancel(pause_seconds, cancel_callback)
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            page = get_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


# ============ 并发编排 ============

class _BatchState:
    """一次批量注册的共享状态（线程安全）。"""

    def __init__(self, target, accounts_output_file, log_base, should_stop, on_stats):
        self.lock = threading.Lock()
        self.next_slot = 0
        self.target = int(target)
        self.success = 0
        self.fail = 0
        self.results = []
        self.accounts_output_file = accounts_output_file
        self._log_base = log_base
        self.should_stop = should_stop
        self._on_stats = on_stats

    def claim(self):
        with self.lock:
            if self.next_slot >= self.target:
                return None
            s = self.next_slot
            self.next_slot += 1
            return s

    def log(self, msg):
        self._log_base(f"{worker_tag()}{msg}")

    def on_success(self, rec=None):
        with self.lock:
            self.success += 1
            if rec:
                self.results.append(rec)
        if self._on_stats:
            self._on_stats(self.success, self.fail)

    def on_fail(self):
        with self.lock:
            self.fail += 1
        if self._on_stats:
            self._on_stats(self.success, self.fail)


def register_one_account(state, log):
    """跑完一个账号的完整流程（假设本 worker 的浏览器/画像已就绪）。
    成功则写文件/入池并计数；失败抛异常（含 ProxySwitchNeeded/AccountRetryNeeded）。"""
    mail_cred_path = os.path.join(os.path.dirname(__file__), "mail_credentials.txt")
    email = ""
    dev_token = ""
    code = ""
    mail_ok = False
    max_mail_retry = 3
    for mail_try in range(1, max_mail_retry + 1):
        log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
        open_signup_page(log_callback=log, cancel_callback=state.should_stop)
        log("[*] 2. 创建邮箱并提交")
        email, dev_token = fill_email_and_submit(log_callback=log, cancel_callback=state.should_stop)
        log(f"[*] 邮箱: {email}")
        log(f"[Debug] 邮箱credential(jwt): {dev_token}")
        try:
            with _output_lock:
                with open(mail_cred_path, "a", encoding="utf-8") as f:
                    f.write(f"{email}\t{dev_token}\n")
        except Exception:
            pass
        log("[*] 3. 拉取验证码")
        try:
            code = fill_code_and_submit(email, dev_token, log_callback=log, cancel_callback=state.should_stop)
            mail_ok = True
            break
        except Exception as mail_exc:
            msg = str(mail_exc)
            if state.should_stop():
                raise RegistrationCancelled("用户停止注册")
            if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                restart_browser(log_callback=log)
                sleep_with_cancel(1, state.should_stop)
                continue
            raise
    if not mail_ok:
        raise Exception("验证码阶段失败，已达到最大重试次数")
    log(f"[*] 验证码: {code}")
    log("[*] 4. 填写资料")
    profile = fill_profile_and_submit(log_callback=log, cancel_callback=state.should_stop)
    log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
    log("[*] 5. 等待 sso cookie")
    sso = wait_for_sso_cookie(log_callback=log, cancel_callback=state.should_stop)

    cpa_thread = None
    cpa_result_box = {}
    _cpa_page = get_page()
    if config.get("cpa_export_enabled", True):
        log("[*] 6. CPA xAI 导出 (OIDC refreshToken) — 复用注册浏览器")

        def _cpa_mint():
            try:
                cpa_result_box["result"] = export_cpa_xai_for_account(
                    email, profile.get("password", ""), sso=sso, log_callback=log, page=_cpa_page
                )
            except Exception as e:
                cpa_result_box["result"] = {"ok": False, "error": str(e)}

        cpa_thread = threading.Thread(target=_cpa_mint, daemon=True)
        cpa_thread.start()
    if config.get("enable_nsfw", True):
        log("[*] 6. 开启 NSFW")
        nsfw_ok, nsfw_msg = enable_nsfw_for_token(sso, log_callback=log)
        if nsfw_ok:
            log(f"[+] NSFW 开启成功: {nsfw_msg}")
        else:
            log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
    if cpa_thread is not None:
        log("[*] 等待 CPA xAI 导出完成...")
        cpa_thread.join(timeout=float(config.get("cpa_mint_timeout_sec", 240) or 240))
        cpa_result = cpa_result_box.get("result", {"ok": False, "error": "timeout"})
        if cpa_result.get("ok"):
            log(f"[+] CPA xAI 导出成功: {cpa_result.get('path', '')}")
        elif cpa_result.get("skipped"):
            log("[cpa] CPA 导出已跳过")
        else:
            log(f"[!] CPA xAI 导出失败: {cpa_result.get('error', '未知错误')}")

    try:
        with _output_lock:
            with open(state.accounts_output_file, "a", encoding="utf-8") as f:
                f.write(f"{email}----{profile.get('password','')}----{sso}\n")
    except Exception as file_exc:
        log(f"[Debug] 保存账号文件失败: {file_exc}")
    add_token_to_grok2api_pools(sso, email=email, log_callback=log)
    add_token_to_token_only_file(sso, log_callback=log)
    state.on_success({"email": email, "sso": sso, "profile": profile})
    log(f"[+] 注册成功: {email}")


def _worker_loop(state, worker_id):
    _tls.tag = f"[W{worker_id}] " if worker_id else ""
    log = state.log
    max_slot_retry = 3
    try:
        while True:
            if state.should_stop():
                break
            slot = state.claim()
            if slot is None:
                break
            stop_browser()
            start_new_account_profile(log_callback=log, cancel=state.should_stop)
            if state.should_stop():
                stop_browser()
                break
            start_browser(log_callback=log)
            if state.should_stop():
                stop_browser()
                break
            log(f"--- 开始第 {slot + 1}/{state.target} 个账号 ---")
            ip_switch_count = 0
            retry_count = 0
            while True:
                try:
                    register_one_account(state, log)
                    break
                except RegistrationCancelled:
                    break
                except ProxySwitchNeeded as exc:
                    if state.should_stop():
                        break
                    budget = _proxy_pool.ip_budget(config.get("max_ip_retry", 3)) if _proxy_pool else 1
                    ip_switch_count += 1
                    if ip_switch_count < budget and _can_switch_ip():
                        log(f"[!] {exc}，切换下一个 IP 重试当前账号 ({ip_switch_count}/{budget})")
                        switch_to_next_ip(log_callback=log, cancel=state.should_stop)
                        restart_browser(log_callback=log)
                        continue
                    state.on_fail()
                    log(f"[-] 换 IP 重试已达上限，跳过当前账号: {exc}")
                    break
                except AccountRetryNeeded as exc:
                    if state.should_stop():
                        break
                    retry_count += 1
                    if retry_count <= max_slot_retry:
                        log(f"[!] 当前账号流程卡住，重试第 {retry_count}/{max_slot_retry} 次: {exc}")
                        restart_browser(log_callback=log)
                        continue
                    state.on_fail()
                    log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
                    break
                except Exception as exc:
                    if state.should_stop():
                        break
                    state.on_fail()
                    log(f"[-] 注册失败: {exc}")
                    break
    finally:
        stop_browser()
        release_current_proxy()
        try:
            bp.cleanup_profile(get_profile())
        except Exception:
            pass


def run_batch(count, log_base, should_stop, on_stats=None, accounts_output_file="", concurrency=1):
    """并发编排：启动 N 个 worker，各自独立浏览器，共同完成 count 个账号。返回 (成功, 失败)。"""
    init_proxy_pool(log_callback=log_base)
    try:
        n = max(1, int(concurrency))
    except Exception:
        n = 1
    if _proxy_pool and _proxy_pool.mode == "pool" and _proxy_pool.size() > 0 and n > _proxy_pool.size():
        log_base(f"[*] 并发数 {n} 超过代理池大小 {_proxy_pool.size()}，多出的 worker 将排队等待空闲 IP")
    n = min(n, max(1, int(count)))
    state = _BatchState(count, accounts_output_file, log_base, should_stop, on_stats)
    if n > 1:
        log_base(f"[*] 并发数: {n}")
    threads = []
    for wid in range(1, n + 1):
        t = threading.Thread(target=_worker_loop, args=(state, wid if n > 1 else 0), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return state.success, state.fail


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok Register Studio")
        self.root.geometry("1180x860")
        self.root.minsize(1040, 680)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.target_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self._pulse_on = False
        self._pulse_step = 0
        self.setup_ui()

    def setup_ui(self):
        load_config()
        root = self.root
        root.configure(bg=UI_BG)

        # ===== 顶栏 =====
        header = tk.Frame(root, bg=UI_HEADER_BG, height=66)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        title_wrap = tk.Frame(header, bg=UI_HEADER_BG)
        title_wrap.pack(side=tk.LEFT, padx=20)
        tk.Label(title_wrap, text="⚡ Grok Register Studio", bg=UI_HEADER_BG, fg=UI_FG,
                 font=(UI_FONT, 16, "bold")).pack(anchor="w", pady=(13, 0))
        tk.Label(title_wrap, text="自动注册工作台 · Turnstile · 代理池 · 指纹",
                 bg=UI_HEADER_BG, fg=UI_MUTED_FG, font=(UI_FONT, 9)).pack(anchor="w")

        status_wrap = tk.Frame(header, bg=UI_HEADER_BG)
        status_wrap.pack(side=tk.RIGHT, padx=20)
        self.pulse_canvas = tk.Canvas(status_wrap, width=14, height=14, bg=UI_HEADER_BG,
                                      highlightthickness=0)
        self.pulse_canvas.pack(side=tk.LEFT, padx=(0, 8))
        self._pulse_dot = self.pulse_canvas.create_oval(3, 3, 12, 12, fill=UI_SUCCESS, outline="")
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = tk.Label(status_wrap, textvariable=self.status_var, bg=UI_HEADER_BG,
                                     fg=UI_SUCCESS, font=(UI_FONT, 11, "bold"))
        self.status_label.pack(side=tk.LEFT)

        tk.Frame(root, bg=UI_BORDER, height=1).pack(fill=tk.X)

        # ===== 主体：左右分栏 =====
        body = tk.Frame(root, bg=UI_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)
        body.grid_columnconfigure(0, minsize=486)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # --- 左：配置 ---
        left = tk.Frame(body, bg=UI_BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        nb = ttk.Notebook(left)
        nb.grid(row=0, column=0, sticky="nsew")
        tab_general = self._make_tab(nb, "  常规  ")
        tab_mail = self._make_tab(nb, "  邮箱  ")
        tab_pool = self._make_tab(nb, "  入池  ")

        # ---- 常规 ----
        r = 0
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = tk_combo(tab_general, self.email_provider_var, ["templol", "duckmail", "yyds", "cloudflare"], width=16)
        r = self._field(tab_general, r, "邮箱服务商", self.email_provider_combo, stretch=False)

        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = tk_spinbox(tab_general, self.count_var, 1, 2500, width=10)
        r = self._field(tab_general, r, "注册数量", self.count_spinbox, stretch=False)

        self.proxy_mode_var = tk.StringVar(value=config.get("proxy_mode", "single"))
        self.proxy_mode_combo = tk_combo(tab_general, self.proxy_mode_var, ["single", "pool"], width=16)
        self.proxy_mode_combo.bind("<<ComboboxSelected>>", self._update_proxy_mode_state)
        r = self._field(tab_general, r, "代理模式", self.proxy_mode_combo, stretch=False)

        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = tk_entry(tab_general, textvariable=self.proxy_var)
        r = self._field(tab_general, r, "代理地址", self.proxy_entry, hint="single 模式使用")

        self.proxy_pool_text = tk_text(tab_general, height=4)
        _pv = config.get("proxy_pool", [])
        if isinstance(_pv, (list, tuple)):
            _pv = "\n".join(str(x) for x in _pv)
        else:
            _pv = str(_pv).replace(",", "\n")
        if _pv.strip():
            self.proxy_pool_text.insert("1.0", _pv.strip())
        r = self._field_multiline(tab_general, r, "代理池", self.proxy_pool_text, hint="每行一个代理，pool 模式使用")

        self.max_ip_retry_var = tk.StringVar(value=str(config.get("max_ip_retry", 3)))
        self.max_ip_retry_spinbox = tk_spinbox(tab_general, self.max_ip_retry_var, 1, 50, width=10)
        r = self._field(tab_general, r, "换 IP 重试上限", self.max_ip_retry_spinbox, stretch=False)

        self.concurrency_var = tk.StringVar(value=str(config.get("concurrency", 1)))
        self.concurrency_spinbox = tk_spinbox(tab_general, self.concurrency_var, 1, 8, width=10)
        r = self._field(tab_general, r, "并发数", self.concurrency_spinbox, stretch=False,
                        hint="每个并发各开一个浏览器；pool 模式每 worker 不同 IP")

        r = self._divider(tab_general, r)

        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = tk_checkbutton(tab_general, text="注册后开启 NSFW", variable=self.nsfw_var, bg=UI_PANEL_ALT)
        r = self._field(tab_general, r, None, self.nsfw_check)

        self.anti_fp_var = tk.BooleanVar(value=bool(config.get("anti_fingerprint", True)))
        self.anti_fp_check = tk_checkbutton(tab_general, text="指纹随机化（随机视口/语言）", variable=self.anti_fp_var, bg=UI_PANEL_ALT)
        r = self._field(tab_general, r, None, self.anti_fp_check)

        self.cpa_export_var = tk.BooleanVar(value=bool(config.get("cpa_export_enabled", True)))
        self.cpa_export_check = tk_checkbutton(tab_general, text="CPA 导出 OIDC refreshToken（需浏览器）", variable=self.cpa_export_var, bg=UI_PANEL_ALT)
        r = self._field(tab_general, r, None, self.cpa_export_check)

        # ---- 邮箱 ----
        r = 0
        r = self._subhead(tab_mail, r, "TempMail.lol（推荐，零配置）")
        self.templol_api_key_var = tk.StringVar(value=str(config.get("templol_api_key", "")))
        self.templol_api_key_entry = tk_entry(tab_mail, textvariable=self.templol_api_key_var)
        r = self._field(tab_mail, r, "API Key", self.templol_api_key_entry)
        self.templol_domains_var = tk.StringVar(value=str(config.get("templol_domains", "")))
        self.templol_domains_entry = tk_entry(tab_mail, textvariable=self.templol_domains_var)
        r = self._field(tab_mail, r, "自定义域名", self.templol_domains_entry)

        r = self._subhead(tab_mail, r, "DuckMail")
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = tk_entry(tab_mail, textvariable=self.api_key_var)
        r = self._field(tab_mail, r, "API Key", self.api_key_entry)

        r = self._subhead(tab_mail, r, "Cloudflare 临时邮箱")
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "none"))
        self.cloudflare_auth_mode_combo = tk_combo(tab_mail, self.cloudflare_auth_mode_var, ["none", "query-key", "bearer", "x-api-key", "x-admin-auth"], width=18)
        r = self._field(tab_mail, r, "鉴权模式", self.cloudflare_auth_mode_combo, stretch=False)
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = tk_entry(tab_mail, textvariable=self.cloudflare_api_base_var)
        r = self._field(tab_mail, r, "API Base", self.cloudflare_api_base_entry)
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = tk_entry(tab_mail, textvariable=self.cloudflare_api_key_var)
        r = self._field(tab_mail, r, "API Key", self.cloudflare_api_key_entry)
        self.cloudflare_paths_var = tk.StringVar(value=",".join([
            config.get("cloudflare_path_domains", "/api/domains"),
            config.get("cloudflare_path_accounts", "/api/new_address"),
            config.get("cloudflare_path_token", "/api/token"),
            config.get("cloudflare_path_messages", "/api/mails"),
        ]))
        self.cloudflare_paths_entry = tk_entry(tab_mail, textvariable=self.cloudflare_paths_var)
        r = self._field(tab_mail, r, "CF 路径", self.cloudflare_paths_entry, hint="domains,accounts,token,messages")

        # ---- 入池 ----
        r = 0
        r = self._subhead(tab_pool, r, "本地 grok2api")
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = tk_checkbutton(tab_pool, text="写入本地 token.json", variable=self.grok2api_local_auto_var, bg=UI_PANEL_ALT)
        r = self._field(tab_pool, r, None, self.grok2api_local_auto_check)
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = tk_combo(tab_pool, self.grok2api_pool_name_var, ["ssoBasic", "ssoSuper"], width=16)
        r = self._field(tab_pool, r, "池名", self.grok2api_pool_name_combo, stretch=False)
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = tk_entry(tab_pool, textvariable=self.grok2api_local_file_var)
        r = self._field(tab_pool, r, "token.json 路径", self.grok2api_local_file_entry, hint="留空则用程序目录")

        r = self._subhead(tab_pool, r, "远端 grok2api")
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = tk_checkbutton(tab_pool, text="写入远端 grok2api", variable=self.grok2api_remote_auto_var, bg=UI_PANEL_ALT)
        r = self._field(tab_pool, r, None, self.grok2api_remote_auto_check)
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = tk_entry(tab_pool, textvariable=self.grok2api_remote_base_var)
        r = self._field(tab_pool, r, "远端 Base", self.grok2api_remote_base_entry)
        self.grok2api_remote_key_var = tk.StringVar(value=str(config.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = tk_entry(tab_pool, textvariable=self.grok2api_remote_key_var)
        r = self._field(tab_pool, r, "远端 app_key", self.grok2api_remote_key_entry)

        # 按钮区
        btnbar = tk.Frame(left, bg=UI_BG)
        btnbar.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.start_btn = make_button(btnbar, "▶  开始注册", command=self.start_registration, primary=True)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = make_button(btnbar, "■  停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)
        self.clear_btn = make_button(btnbar, "清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.RIGHT)

        # --- 右：仪表盘 ---
        right = tk.Frame(body, bg=UI_BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)

        cards = tk.Frame(right, bg=UI_BG)
        cards.grid(row=0, column=0, sticky="ew")
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1, uniform="stat")
        self.card_success = self._make_stat_card(cards, 0, "成功", UI_SUCCESS)
        self.card_fail = self._make_stat_card(cards, 1, "失败", UI_ERROR)
        self.card_target = self._make_stat_card(cards, 2, "目标", UI_ACCENT)
        self.card_progress = self._make_stat_card(cards, 3, "进度", UI_FG)

        self.progress_var = tk.DoubleVar(value=0)
        pb = ttk.Progressbar(right, style="Accent.Horizontal.TProgressbar",
                             variable=self.progress_var, maximum=100)
        pb.grid(row=1, column=0, sticky="ew", pady=(12, 14))

        logcard = tk.Frame(right, bg=UI_PANEL_BG, highlightthickness=1, highlightbackground=UI_BORDER)
        logcard.grid(row=2, column=0, sticky="nsew")
        logcard.grid_rowconfigure(1, weight=1)
        logcard.grid_columnconfigure(0, weight=1)
        loghead = tk.Frame(logcard, bg=UI_PANEL_BG)
        loghead.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 8))
        tk.Label(loghead, text="运行日志", bg=UI_PANEL_BG, fg=UI_FG, font=(UI_FONT, 11, "bold")).pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功 0 · 失败 0")
        tk.Label(loghead, textvariable=self.stats_var, bg=UI_PANEL_BG, fg=UI_MUTED_FG, font=(UI_FONT, 9)).pack(side=tk.RIGHT)

        logwrap = tk.Frame(logcard, bg=UI_LOG_BG)
        logwrap.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        logwrap.grid_rowconfigure(0, weight=1)
        logwrap.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(logwrap, bg=UI_LOG_BG, fg=UI_FG, insertbackground=UI_ACCENT,
                                relief=tk.FLAT, bd=0, wrap="word", padx=12, pady=8,
                                font=(UI_MONO_FONT, 10), selectbackground=UI_ACCENT_DIM,
                                selectforeground=UI_FG)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logwrap, orient="vertical", style="Vertical.TScrollbar", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        self._setup_log_tags()

        self._update_proxy_mode_state()
        self.update_stats()
        self.log("[*] GUI 已就绪，配置已加载")
        self.log(f"[*] 当前邮箱服务商: {self.email_provider_var.get()} | 目标数量: {self.count_var.get()}")

    # ===== UI 构建辅助 =====
    def _make_tab(self, nb, title):
        page = tk.Frame(nb, bg=UI_PANEL_ALT)
        nb.add(page, text=title)
        inner = tk.Frame(page, bg=UI_PANEL_ALT)
        inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=10)
        inner.grid_columnconfigure(1, weight=1)
        return inner

    def _field(self, page, r, label_text, widget, stretch=True, hint=None):
        if label_text is None:
            widget.grid(row=r, column=0, columnspan=2, sticky="w", padx=14, pady=7)
        else:
            tk_label(page, text=label_text, bg=UI_PANEL_ALT, fg=UI_MUTED_FG).grid(
                row=r, column=0, sticky="w", padx=(14, 12), pady=7)
            widget.grid(row=r, column=1, sticky="ew" if stretch else "w", padx=(0, 14), pady=7)
        r += 1
        if hint:
            tk.Label(page, text=hint, bg=UI_PANEL_ALT, fg=UI_DEBUG, font=(UI_FONT, 8)).grid(
                row=r, column=1, sticky="w", padx=(0, 14), pady=(0, 4))
            r += 1
        return r

    def _field_multiline(self, page, r, label_text, widget, hint=None):
        tk_label(page, text=label_text, bg=UI_PANEL_ALT, fg=UI_MUTED_FG).grid(
            row=r, column=0, sticky="nw", padx=(14, 12), pady=(9, 7))
        widget.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=7)
        r += 1
        if hint:
            tk.Label(page, text=hint, bg=UI_PANEL_ALT, fg=UI_DEBUG, font=(UI_FONT, 8)).grid(
                row=r, column=1, sticky="w", padx=(0, 14), pady=(0, 4))
            r += 1
        return r

    def _update_proxy_mode_state(self, *_):
        is_pool = (self.proxy_mode_var.get() or "single").strip().lower() == "pool"
        self._set_input_enabled(self.proxy_entry, not is_pool)
        self._set_text_enabled(self.proxy_pool_text, is_pool)

    def _set_input_enabled(self, widget, enabled):
        try:
            widget.config(state=(tk.NORMAL if enabled else tk.DISABLED))
        except Exception:
            pass

    def _set_text_enabled(self, widget, enabled):
        try:
            if enabled:
                widget.config(state=tk.NORMAL, bg=UI_ENTRY_BG, fg=UI_FG)
            else:
                widget.config(state=tk.DISABLED, bg=UI_PANEL_BG, fg=UI_MUTED_FG)
        except Exception:
            pass

    def _subhead(self, page, r, text):
        f = tk.Frame(page, bg=UI_PANEL_ALT)
        f.grid(row=r, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 2))
        tk.Frame(f, bg=UI_ACCENT, width=3, height=14).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(f, text=text, bg=UI_PANEL_ALT, fg=UI_FG, font=(UI_FONT, 10, "bold")).pack(side=tk.LEFT)
        return r + 1

    def _divider(self, page, r):
        tk.Frame(page, bg=UI_BORDER, height=1).grid(row=r, column=0, columnspan=2, sticky="ew", padx=14, pady=8)
        return r + 1

    def _make_stat_card(self, parent, col, title, color):
        card = tk.Frame(parent, bg=UI_PANEL_BG, highlightthickness=1, highlightbackground=UI_BORDER)
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 10, 0))
        tk.Frame(card, bg=color, width=3).pack(side=tk.LEFT, fill=tk.Y)
        inner = tk.Frame(card, bg=UI_PANEL_BG)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=16, pady=12)
        val = tk.Label(inner, text="0", bg=UI_PANEL_BG, fg=color, font=(UI_FONT, 24, "bold"))
        val.pack(anchor="w")
        tk.Label(inner, text=title, bg=UI_PANEL_BG, fg=UI_MUTED_FG, font=(UI_FONT, 9)).pack(anchor="w")
        return val

    # ===== 脉冲动画 =====
    def _start_pulse(self):
        if self._pulse_on:
            return
        self._pulse_on = True
        self._pulse_step = 0
        self._pulse()

    def _pulse(self):
        if not self._pulse_on:
            return
        import math
        self._pulse_step = (self._pulse_step + 1) % 50
        t = (math.sin(self._pulse_step / 50 * 2 * math.pi) + 1) / 2
        try:
            self.pulse_canvas.itemconfig(self._pulse_dot, fill=_blend_hex(UI_ACCENT_DIM, UI_ACCENT_HOVER, t))
            self.root.after(70, self._pulse)
        except Exception:
            self._pulse_on = False

    def _stop_pulse(self, color):
        self._pulse_on = False
        try:
            self.pulse_canvas.itemconfig(self._pulse_dot, fill=color)
        except Exception:
            pass

    # ===== 日志 =====
    def _setup_log_tags(self):
        self.log_text.tag_configure("ts", foreground=UI_DEBUG)
        self.log_text.tag_configure("info", foreground=UI_FG)
        self.log_text.tag_configure("ok", foreground=UI_SUCCESS)
        self.log_text.tag_configure("warn", foreground=UI_WARN)
        self.log_text.tag_configure("err", foreground=UI_ERROR)
        self.log_text.tag_configure("debug", foreground=UI_DEBUG)
        self.log_text.tag_configure("cpa", foreground=UI_CPA)

    def _log_tag(self, text):
        if text.startswith("[+]"):
            return "ok"
        if text.startswith("[!]"):
            return "warn"
        if text.startswith("[-]"):
            return "err"
        if text.startswith("[Debug]"):
            return "debug"
        if text.startswith("[cpa]"):
            return "cpa"
        return "info"

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = _sanitize_log_text(message)
        try:
            print(f"[{timestamp}] {text}", flush=True)
        except Exception:
            pass
        try:
            self.root.after(0, self._append_log, timestamp, text)
        except Exception:
            pass

    def _append_log(self, timestamp, text):
        try:
            self.log_text.insert(tk.END, f"{timestamp}  ", ("ts",))
            self.log_text.insert(tk.END, f"{text}\n", (self._log_tag(text),))
            last = int(self.log_text.index("end-1c").split(".")[0])
            if last > 2200:
                self.log_text.delete("1.0", f"{last - 2000}.0")
            self.log_text.see(tk.END)
        except Exception:
            pass

    def clear_log(self):
        try:
            self.log_text.delete("1.0", tk.END)
        except Exception:
            pass

    # ===== 统计 / 运行态 =====
    def update_stats(self):
        try:
            self.root.after(0, self._apply_stats)
        except Exception:
            pass

    def _apply_stats(self):
        target = self.target_count
        if not target:
            try:
                target = int(self.count_var.get())
            except Exception:
                target = 0
        done = self.success_count + self.fail_count
        try:
            self.card_success.config(text=str(self.success_count))
            self.card_fail.config(text=str(self.fail_count))
            self.card_target.config(text=str(target) if target else "—")
            pct = int(done / target * 100) if target else 0
            pct = max(0, min(100, pct))
            self.card_progress.config(text=f"{pct}%")
            self.progress_var.set(pct)
            self.stats_var.set(f"成功 {self.success_count} · 失败 {self.fail_count}")
        except Exception:
            pass

    def _set_btn_enabled(self, btn, enabled):
        try:
            if enabled:
                btn.config(state=tk.NORMAL, bg=getattr(btn, "_base_bg", UI_BUTTON_BG))
            else:
                btn.config(state=tk.DISABLED, bg=UI_PANEL_ALT)
        except Exception:
            pass

    def _set_running_ui(self, running):
        self.is_running = running
        try:
            self.root.after(0, self._apply_running_ui, running)
        except Exception:
            pass

    def _apply_running_ui(self, running):
        self._set_btn_enabled(self.start_btn, not running)
        self._set_btn_enabled(self.stop_btn, running)
        if running:
            self.status_var.set("运行中")
            self.status_label.config(fg=UI_ACCENT)
            self._start_pulse()
        else:
            self.status_var.set("就绪")
            self.status_label.config(fg=UI_SUCCESS)
            self._stop_pulse(UI_SUCCESS)

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "duckmail"
        config["enable_nsfw"] = bool(self.nsfw_var.get())
        config["proxy"] = self.proxy_var.get().strip()
        config["proxy_mode"] = self.proxy_mode_var.get().strip() or "single"
        config["proxy_pool"] = bp.normalize_proxy_list(self.proxy_pool_text.get("1.0", tk.END))
        config["anti_fingerprint"] = bool(self.anti_fp_var.get())
        config["cpa_export_enabled"] = bool(self.cpa_export_var.get())
        try:
            config["max_ip_retry"] = max(1, int(self.max_ip_retry_var.get()))
        except Exception:
            config["max_ip_retry"] = 3
        try:
            config["concurrency"] = max(1, int(self.concurrency_var.get()))
        except Exception:
            config["concurrency"] = 1
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["templol_api_key"] = self.templol_api_key_var.get().strip()
        config["templol_domains"] = self.templol_domains_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "none"
        config["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        config["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        config["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        config["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        config["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        config["grok2api_remote_app_key"] = self.grok2api_remote_key_var.get().strip()
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        config["register_count"] = count
        save_config()
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.target_count = count
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}")
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count,),
            daemon=True,
        ).start()

    def stop_registration(self):
        if not self.is_running:
            return
        self.stop_requested = True
        self.log("[!] 用户停止注册，正在关闭浏览器…")
        # 立即反馈：状态改「停止中」，禁用停止按钮防重复点击
        try:
            self.status_var.set("停止中…")
            self.status_label.config(fg=UI_WARN)
            self._stop_pulse(UI_WARN)
            self.stop_btn.config(state=tk.DISABLED, bg=UI_PANEL_ALT)
        except Exception:
            pass
        # 强制关闭所有浏览器，打断卡在死代理导航里的 worker（放后台线程，避免卡 UI）
        threading.Thread(target=force_stop_all_browsers, daemon=True).start()

    def run_registration(self, count):
        try:
            conc = int(config.get("concurrency", 1) or 1)
        except Exception:
            conc = 1

        def _on_stats(success, fail):
            self.success_count = success
            self.fail_count = fail
            self.update_stats()

        try:
            run_batch(
                count,
                self.log,
                self.should_stop,
                on_stats=_on_stats,
                accounts_output_file=self.accounts_output_file,
                concurrency=conc,
            )
        except Exception as exc:
            self.log(f"[!] 任务异常: {exc}")
        finally:
            stop_browser()
            self._set_running_ui(False)
            self.log("[*] 任务结束")


class CliStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


_cli_log_lock = threading.Lock()


def cli_log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    with _cli_log_lock:
        print(f"[{timestamp}] {message}", flush=True)


def run_registration_cli(count):
    controller = CliStopController()
    accounts_output_file = os.path.join(
        os.path.dirname(__file__),
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    cli_log(f"[*] 终端模式启动，目标数量: {count}")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    try:
        conc = int(config.get("concurrency", 1) or 1)
    except Exception:
        conc = 1
    success = 0
    fail = 0
    try:
        success, fail = run_batch(
            count,
            cli_log,
            controller.should_stop,
            on_stats=None,
            accounts_output_file=accounts_output_file,
            concurrency=conc,
        )
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止并清理")
        force_stop_all_browsers()
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        force_stop_all_browsers()
        stop_browser()
        cli_log(f"[*] 任务结束。成功 {success} | 失败 {fail}")


def main_cli():
    load_config()
    count = int(config.get("register_count", 1) or 1)
    cli_log("[*] CLI 已加载配置")
    cli_log(f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | 注册数量: {count}")
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    run_registration_cli(count)


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
