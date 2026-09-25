#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy签到助手（每日自动签到脚本，接口直签，无需 GUI 点击 / OCR）

原理：
  1. 读取本机 WorkBuddy 登录态文件中的 accessToken（只读，绝不修改登录态）
  2. 查询今日签到状态  POST {base}/billing/meter/checkin-activity-status
  3. 若今日未签到，调用 POST {base}/billing/meter/daily-checkin 领取
  4. 已签到 / 接口返回 code=10001 则安全跳过，不做重复领取

失败推送（可选）：
  若签到结果为 status!=ok，会读取本地配置文件
  ~/.workbuddy/scripts/notify_config.json（若存在），向微信通道推送失败提醒。
  支持：企业微信群机器人 webhook / PushPlus / Bark。配置缺失则静默跳过，不影响签到。

成功推送（可选，默认关闭）：
  在 notify_config.json 中设置 "success_notify": true 后，
  签到成功（本次新签到 / 今日已签跳过）也会向同一组微信通道推送一条播报。
  默认不开启，保持「静默无打扰」；仅失败时提醒。

安全约定：
  - 不打印 token / accessToken / refreshToken（任何输出都不含敏感凭据）
  - 不修改本机登录态文件
  - 推送密钥只存在于本地 notify_config.json，永不进入脚本或技能目录
  - 异常只记录失败原因，最多重试 1 次，不无限重试

用法：
  python workbuddy_checkin.py            # 查询 + 必要时领取
  python workbuddy_checkin.py --check-only   # 仅查询状态（只读，不领取）
  python workbuddy_checkin.py --no-notify    # 跳过全部推送与桌面通知（调试用）
  python workbuddy_checkin.py --diagnose     # 环境自检（Python/登录态/网络/桌面会话/微信配置）
  python workbuddy_checkin.py --init-config  # 生成 notify_config.json.example 模板
  python workbuddy_checkin.py --help         # 显示帮助
  python workbuddy_checkin.py --version      # 显示版本
  成功推送开关见 ~/.workbuddy/scripts/notify_config.json 的 "success_notify"
"""

import sys

# Python 版本守卫：太旧时给出友好提示，而不是抛出一堆堆栈
if sys.version_info < (3, 6):
    sys.stderr.write(
        "WorkBuddy签到助手：需要 Python 3.6+，当前为 %s。\n"
        "请安装 Python 3.8+（https://www.python.org/downloads/，勾选 Add to PATH），\n"
        "或直接使用 WorkBuddy 自带的托管 Python（~/.workbuddy/binaries/python）。\n"
        % sys.version.split()[0])
    sys.exit(2)

import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ---- 配置 ----
def _auth_candidates():
    """按操作系统返回 WorkBuddy 登录态文件候选路径，依次探测。"""
    home = os.path.expanduser("~")
    cands = []
    if sys.platform.startswith("win"):
        for env in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env, "")
            if base:
                cands.append(os.path.join(
                    base, "CodeBuddyExtension", "Data", "Public",
                    "auth", "workbuddy-desktop.info"))
    elif sys.platform == "darwin":
        cands.append(os.path.join(
            home, "Library", "Application Support", "CodeBuddyExtension",
            "Data", "Public", "auth", "workbuddy-desktop.info"))
    else:  # linux / 其他类 Unix
        cands.append(os.path.join(
            home, ".config", "CodeBuddyExtension", "Data", "Public",
            "auth", "workbuddy-desktop.info"))
    # 兜底：便携版 / 未知布局（与 ~/.workbuddy 同根）
    cands.append(os.path.join(
        home, ".workbuddy", "auth", "workbuddy-desktop.info"))
    return [p for p in cands if p]
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"
HTTP_TIMEOUT = 10
MAX_RETRY = 1
VERSION = "2.0.0"
# 失败推送配置（含密钥，仅本地，不入库）
NOTIFY_CONFIG = os.path.join(os.path.expanduser("~"),
                             ".workbuddy", "scripts", "notify_config.json")


def find_auth_file():
    for p in _auth_candidates():
        if os.path.isfile(p):
            return p
    return None


def _env_token():
    """从环境变量读取 token / domain（用于 CI / 无登录态文件的场景）。

    支持两种变量名（后者优先，便于在 GitHub Secrets 中统一命名）：
      - WORKBUDDY_ACCESS_TOKEN / WORKBUDDY_TOKEN            （accessToken）
      - WORKBUDDY_DOMAIN      / WORKBUDDY_AUTH_DOMAIN       （域名，默认 www.codebuddy.cn）
    返回 (token, domain)；未设置 token 时返回 (None, None)。
    """
    token = (os.environ.get("WORKBUDDY_ACCESS_TOKEN")
             or os.environ.get("WORKBUDDY_TOKEN") or "").strip()
    if not token:
        return None, None
    domain = (os.environ.get("WORKBUDDY_DOMAIN")
              or os.environ.get("WORKBUDDY_AUTH_DOMAIN")
              or "www.codebuddy.cn").strip()
    # 容错：允许传入带协议前缀的域名
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    return token, domain


def _accounts_from_env():
    """从环境变量 WORKBUDDY_TOKENS 读取多账号列表（用于 GitHub Actions 批量签到）。

    支持三种写法（推荐 JSON 数组，可由 scripts/build_tokens_json.py 生成）：
      1. JSON 数组，元素为 token 字符串：["token1", "token2"]
      2. JSON 数组，元素为对象：[{"name": "主号", "token": "...", "domain": "..."}, ...]
      3. 纯文本按行分隔（每行一个 token，可选 "名称:" 前缀）

    返回：
      - None   未设置 WORKBUDDY_TOKENS（走原有单账号流程）
      - []     已设置但解析后没有任何有效账号（调用方应报错）
      - [{name, token, domain}, ...]
    """
    raw = (os.environ.get("WORKBUDDY_TOKENS") or "").strip()
    if not raw:
        return None
    accounts = []

    def _norm_name(i, name):
        return (str(name or "").strip() or "account%d" % (i + 1))

    def _clean_domain(d):
        d = str(d or "").replace("https://", "").replace("http://", "").strip("/")
        return d or None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 兜底：按行解析，支持 "名称:token" / 纯 token 两种行格式
        lines = [l.strip() for l in raw.replace("\r", "").split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if ":" in line and not line.startswith("{"):
                name, _, tok = line.partition(":")
                accounts.append({"name": _norm_name(i, name),
                                 "token": tok.strip(), "domain": None})
            else:
                accounts.append({"name": _norm_name(i, None),
                                 "token": line, "domain": None})
        return [a for a in accounts if a["token"]]

    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        inner = data.get("accounts")
        items = inner if isinstance(inner, list) else [data]

    for i, item in enumerate(items):
        if isinstance(item, str) and item.strip():
            accounts.append({"name": _norm_name(i, None),
                             "token": item.strip(), "domain": None})
        elif isinstance(item, dict) and str(item.get("token", "")).strip():
            accounts.append({
                "name": _norm_name(i, item.get("name")),
                "token": str(item.get("token")).strip(),
                "domain": _clean_domain(item.get("domain")),
            })
    return accounts


def _check_expiry(auth):
    """登录态已过期时给出友好提示（只读取 expiresAt，绝不打印 token）。"""
    raw = auth.get("expiresAt")
    if not raw:
        return  # 无过期字段则跳过检查
    try:
        exp = int(raw)
    except (TypeError, ValueError):
        return
    # 兼容 epoch 毫秒（13 位）/ 秒（10 位）
    if exp > 10 ** 11:
        exp = exp / 1000.0
    if exp <= time.time():
        expire_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
        raise RuntimeError(
            "登录态已过期（过期时间 %s），请重新登录 WorkBuddy 客户端后再试" % expire_str)


def load_token(auth_path):
    with open(auth_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    auth = data.get("auth", {})
    token = auth.get("accessToken")
    domain = auth.get("domain") or "www.codebuddy.cn"
    if not token:
        raise RuntimeError("登录态文件中未找到 accessToken（可能未登录或登录态已失效）")
    _check_expiry(auth)
    return token, domain


def api_call(base, path, token, payload=None, method="POST"):
    url = base + path
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Script/1.1")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        # 非 2xx 也读取响应体（如已签到返回的 HTTP 400 / code=10001）
        try:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body}
        except Exception:
            return e.code, {"raw": ""}


def mask_token(t):
    if not t:
        return "<empty>"
    return t[:6] + "..." + t[-4:]


def _extract_balance(*bodies):
    """从接口响应中尽力提取「积分余额」（total / balance 类字段）。

    不同版本接口返回的余额字段名不统一，这里按候选名 + 嵌套层级兜底提取，
    找不到则返回 None（不影响签到主流程）。
    """
    candidates = (
        "total_credit", "total_credit_balance", "total_points", "points_balance",
        "credit_balance", "balance", "remain_credit", "remain", "score",
        "integral", "totalCredit", "pointsBalance", "balanceCredit",
    )
    sections = ("", "data", "result", "data.result")
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for sec in sections:
            node = body
            for part in sec.split(".") if sec else []:
                if isinstance(node, dict):
                    node = node.get(part)
                else:
                    node = None
                    break
            if not isinstance(node, dict):
                # 顶层（sec 为空字符串）直接看 body 本身
                node = body if sec == "" else None
            if not isinstance(node, dict):
                continue
            for k in candidates:
                v = node.get(k)
                if isinstance(v, (int, float)):
                    return v
    return None


# ---------------- 失败推送（微信） ----------------

def load_notify_config():
    if not os.path.isfile(NOTIFY_CONFIG):
        return None
    try:
        with open(NOTIFY_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return None
        return cfg
    except Exception:
        return None


def _http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Script/1.1")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def notify_via_wecom(webhook, title, content):
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    return _http_post_json(webhook, payload)


def notify_via_pushplus(token, title, content):
    url = "https://www.pushplus.plus/send"
    payload = {"token": token, "title": title,
               "content": content, "template": "markdown"}
    return _http_post_json(url, payload)


def notify_via_bark(bark_url, title, content):
    # bark_url 形如 https://api.day.app/<key>/ ，脚本自动拼接标题与内容
    base = bark_url.rstrip("/")
    url = "%s/%s/%s" % (base,
                        urllib.parse.quote(title),
                        urllib.parse.quote(content))
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Script/1.1")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def notify_via_serverchan(sendkey, title, content):
    """Server酱（方糖，ServerChan）推送到个人微信。

    - Turbo 版（SendKey 形如 SCTxxxx）：POST https://sctapi.ftqq.com/<sendkey>.send
      表单参数 title / desp（desp 支持 Markdown）
    - 旧版（SendKey 形如 SCUxxxx）：POST https://sc.ftqq.com/<sendkey>.send
      表单参数 text / desp
    """
    if sendkey.startswith("SCU"):
        url = "https://sc.ftqq.com/%s.send" % sendkey
        data = urllib.parse.urlencode({"text": title, "desp": content}).encode("utf-8")
    else:
        url = "https://sctapi.ftqq.com/%s.send" % sendkey
        data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Script/1.1")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _dispatch_channels(cfg, title, content):
    """按本地配置向所有已启用的微信通道推送；返回各通道结果列表（不含任何密钥）。"""
    results = []
    # 企业微信群机器人 webhook（优先级最高）
    webhook = cfg.get("wecom_webhook")
    if webhook:
        try:
            st, _ = notify_via_wecom(webhook, title, content)
            results.append("wecom:%s" % st)
        except Exception as e:
            results.append("wecom_err:%s" % e)
    # PushPlus（推送到个人微信）
    token = cfg.get("pushplus_token")
    if token:
        try:
            st, _ = notify_via_pushplus(token, title, content)
            results.append("pushplus:%s" % st)
        except Exception as e:
            results.append("pushplus_err:%s" % e)
    # Bark（iOS 推送）
    bark = cfg.get("bark_url")
    if bark:
        try:
            st, _ = notify_via_bark(bark, title, content)
            results.append("bark:%s" % st)
        except Exception as e:
            results.append("bark_err:%s" % e)
    # Server酱（方糖）推送到个人微信
    sc = cfg.get("serverchan_sendkey")
    if sc:
        try:
            st, _ = notify_via_serverchan(sc, title, content)
            results.append("serverchan:%s" % st)
        except Exception as e:
            results.append("serverchan_err:%s" % e)
    return results


def notify_failure(res):
    """签到失败时，按本地配置推送微信提醒；配置缺失则静默跳过。"""
    cfg = load_notify_config()
    res.setdefault("detail", {})["notify_config_present"] = cfg is not None
    res["detail"]["notify_enabled"] = cfg.get("enabled") if cfg else None
    if not cfg:
        return
    if cfg.get("enabled") is False:
        return

    title = "⚠️ WorkBuddy签到助手 · 签到失败"
    now = time.strftime("%Y-%m-%d %H:%M:%S")  # 本机时区（北京时间）
    msg = res.get("msg", "未知原因")
    auth_file = res.get("detail", {}).get("auth_file", "未知")

    content = (
        "### ⚠️ WorkBuddy签到助手 · 签到失败\n\n"
        "> **时间**：%s\n\n"
        "> **原因**：%s\n\n"
        "> **登录态文件**：%s\n\n"
        "> **处理建议**：请检查 WorkBuddy 是否已登录、电脑是否联网、09:00 前后是否开机且客户端未退出；"
        "必要时重启客户端刷新登录态后，可手动再跑一次脚本。\n"
    ) % (now, msg, auth_file)

    results = _dispatch_channels(cfg, title, content)
    # 仅记录推送动作结果（不含任何密钥 / token），便于排查
    res["detail"]["notify"] = results


def notify_success(res):
    """签到成功（新签到 / 今日已签跳过）时，按本地配置推送微信播报。

    仅当 notify_config.json 中 success_notify=true 时才推送；否则静默。
    与失败推送共用通道与密钥配置。
    """
    cfg = load_notify_config()
    res.setdefault("detail", {})["notify_config_present"] = cfg is not None
    res["detail"]["notify_enabled"] = cfg.get("enabled") if cfg else None
    res["detail"]["notify_success_notify_flag"] = cfg.get("success_notify") if cfg else None
    if not cfg:
        return
    if cfg.get("enabled") is False:
        return
    if not cfg.get("success_notify"):
        return

    now = time.strftime("%Y-%m-%d %H:%M:%S")  # 本机时区（北京时间）
    action = res.get("action")
    # 仅对明确的成功态推送（新签到 / 今日已签跳过）；其他态（失败 / 纯查询）不推
    if action not in ("clicked", "skip_already_signed"):
        return
    msg = res.get("msg", "")
    points = res.get("points")
    streak = res.get("detail", {}).get("streak_days")
    balance = res.get("balance")

    if action == "skip_already_signed":
        title = "✅ WorkBuddy签到助手 · 今日已签"
        content = (
            "### ✅ WorkBuddy签到助手 · 今日已签\n\n"
            "> **时间**：%s\n\n"
            "> **状态**：今日已签到，无需重复领取（幂等保护）\n\n"
            "> **说明**：系统定时任务 / 技能已正常执行，无需处理。\n"
        ) % now
    else:
        title = "✅ WorkBuddy签到助手 · 签到成功"
        lines = (
            "### ✅ WorkBuddy签到助手 · 签到成功\n\n"
            "> **时间**：%s\n\n"
            "> **状态**：%s\n"
        ) % (now, msg)
        if points:
            lines += "> **积分**：+%s\n\n" % points
        if streak:
            lines += "> **连续天数**：第 %s 天\n\n" % streak
        if balance is not None:
            lines += "> **当前积分余额**：%s\n\n" % balance
        lines += "> **说明**：系统定时任务 / 技能已正常执行，无需处理。\n"
        content = lines

    results = _dispatch_channels(cfg, title, content)
    res["detail"]["notify_success"] = results


def notify_system(res):
    """签到完成后弹出操作系统级桌面通知（toast / 气球提示），展示结果 + 积分余额。

    跨平台兼容 Windows / macOS / Linux；best-effort、非阻塞、失败静默，
    绝不影响签到结果与退出码。受 --no-notify 一并抑制（与微信推送调试开关一致）。
    """
    try:
        status = res.get("status")
        title = "WorkBuddy签到助手"
        if status == "error":
            body = "签到失败：" + str(res.get("msg", ""))
        else:
            body = str(res.get("msg", "签到完成"))
            bal = res.get("balance")
            if bal is not None:
                body += " ｜ 当前积分余额：" + str(bal)
        _system_toast(title, body)
    except Exception:
        pass  # 通知失败绝不影响签到


def _system_toast(title, body):
    """按操作系统分发到原生命令；任何异常一律忽略。"""
    plat = sys.platform
    try:
        if plat == "darwin":
            msg = body.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 'display notification "%s" with title "%s"' % (msg, title)],
                timeout=5, check=False,
            )
        elif plat.startswith("linux"):
            subprocess.run(["notify-send", title, body], timeout=5, check=False)
        elif plat == "win32":
            _win_toast(title, body)
    except Exception:
        pass


def _win_toast(title, body):
    """Windows：用 .NET 气球提示（非阻塞、约 5s）。PowerShell 不可用或被策略拦截时静默跳过。"""
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "Add-Type -AssemblyName System.Drawing;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;"
          "$n.Visible=$true;"
          "$n.ShowBalloonTip(5000,'%s','%s','Info');"
          "Start-Sleep -Milliseconds 150;"
          "$n.Dispose()") % (safe_title, safe_body)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        timeout=10, check=False,
    )


# ---------------- 主流程 ----------------

def run(check_only, token=None, domain=None, label=None):
    """执行一次签到流程。

    多账号模式：调用方显式传入 token（可选 domain / label）时，跳过环境变量与
    本机登录态文件，直接使用该凭据；单账号模式行为与原版完全一致。
    """
    result = {"status": "unknown", "action": None, "points": None,
              "balance": None, "msg": "", "detail": {}}

    auth_path = None
    # 0) 多账号模式：显式传入 token（来自 WORKBUDDY_TOKENS 解析结果）
    if token:
        result["detail"]["auth_source"] = "multi"
        result["detail"]["account_name"] = label or mask_token(token)
        result["detail"]["auth_file"] = "(multi: %s)" % result["detail"]["account_name"]
        result["detail"]["token_masked"] = mask_token(token)
        if not domain:
            domain = (os.environ.get("WORKBUDDY_DOMAIN")
                      or os.environ.get("WORKBUDDY_AUTH_DOMAIN") or "").strip() \
                     or "www.codebuddy.cn"
            domain = domain.replace("https://", "").replace("http://", "").strip("/")
        result["detail"]["domain"] = domain
    else:
        # 1) 优先从环境变量读取（CI / GitHub Actions 等无登录态文件的场景）
        env_token, env_domain = _env_token()
        if env_token:
            token, domain = env_token, env_domain
            result["detail"]["auth_source"] = "env"
            result["detail"]["domain"] = domain
            result["detail"]["auth_file"] = "(env: WORKBUDDY_ACCESS_TOKEN)"
            result["detail"]["token_masked"] = mask_token(token)
        else:
            # 2) 回退到本机登录态文件
            auth_path = find_auth_file()
            if not auth_path:
                result.update(status="error",
                              msg="未找到本机登录态文件，也未设置 WORKBUDDY_ACCESS_TOKEN 环境变量")
                return result
            try:
                token, domain = load_token(auth_path)
            except Exception as e:
                result.update(status="error", msg="读取登录态失败: %s" % e)
                return result
            result["detail"]["auth_source"] = "file"
            result["detail"]["domain"] = domain
            result["detail"]["auth_file"] = auth_path
            # 仅记录 token 形态，绝不记录真实值
            result["detail"]["token_masked"] = mask_token(token)

    base = "https://%s/v2" % domain

    attempt = 0
    last_err = None
    while attempt <= MAX_RETRY:
        attempt += 1
        try:
            # 1) 查询今日状态
            st_code, st_body = api_call(base, STATUS_PATH, token)
            result["detail"]["status_http"] = st_code
            result["detail"]["status_resp"] = st_body
            # 从状态响应中提前提取积分余额（领取分支会用领取响应再覆盖一次）
            result["balance"] = _extract_balance(st_body)
            result["detail"]["balance"] = result["balance"]

            # 判断是否已签到：兼容多种返回形态
            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif st_body.get("data", {}).get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True

            # 活动未开放（如国际版 workbuddy.ai 暂无进行中的签到活动）→ 安静跳过，不算失败
            data_obj = st_body.get("data") if isinstance(st_body, dict) else None
            if isinstance(data_obj, dict) and data_obj.get("active") is False \
                    and not today_signed:
                result.update(status="ok", action="skip_no_active",
                              msg="当前无进行中的签到活动，跳过（活动开放后将自动签到）")
                result["detail"]["today_signed"] = False
                return result

            if check_only:
                bal_txt = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                result.update(
                    status="ok",
                    action="skip_check_only",
                    msg="状态查询成功（未执行领取）" + bal_txt,
                )
                result["detail"]["today_signed"] = today_signed
                return result

            if today_signed:
                bal_txt = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                result.update(status="ok", action="skip_already_signed",
                              msg="今日已签到，无需重复领取" + bal_txt)
                return result

            # 2) 领取签到
            ck_code, ck_body = api_call(base, CHECKIN_PATH, token)
            result["detail"]["checkin_http"] = ck_code
            result["detail"]["checkin_resp"] = ck_body

            if isinstance(ck_body, dict):
                code = str(ck_body.get("code", ""))
                msg = ck_body.get("msg") or ck_body.get("message") or ""
                data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
                # 已签/重复提示（幂等保护）
                if code == "10001" or "已签到" in msg or "今天已签到" in msg:
                    result.update(status="ok", action="skip_already_signed",
                                  msg="今日已签到（接口返回 code=10001）")
                    return result
                # 领取成功判定：HTTP 2xx 且业务码为成功（兼容无 code / code=0 / code=200）
                success_code = code in ("", "0", "200")
                if 200 <= ck_code < 300 and success_code:
                    credit = (ck_body.get("credit") or data.get("credit")
                              or data.get("daily_credit") or data.get("today_credit"))
                    streak = ck_body.get("streak_days") or data.get("streak_days")
                    # 领取响应若带回余额则覆盖状态响应中的值
                    bbal = _extract_balance(ck_body)
                    if bbal is not None:
                        result["balance"] = bbal
                        result["detail"]["balance"] = bbal
                    bal_txt = ("，当前积分余额 %s" % result["balance"]) if result.get("balance") is not None else ""
                    result.update(status="ok", action="clicked",
                                  points=credit,
                                  msg="领取成功" + (("，+%s 积分" % credit) if credit else "") +
                                      (("，连续第 %s 天" % streak) if streak else "") + bal_txt)
                    result["detail"]["streak_days"] = streak
                    return result
                # 其余视为失败（含 2xx 但业务码异常、或非 2xx）
                result.update(status="error", action="failed",
                              msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
                return result
            else:
                result.update(status="error", action="failed",
                              msg="领取接口返回非 JSON: %s" % ck_body.get("raw", "")[:200])
                return result

        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last_err = "网络错误: %s" % e.reason
        except Exception as e:
            last_err = "异常: %s" % e

        # 重试前稍作等待
        if attempt <= MAX_RETRY:
            time.sleep(2)

    result.update(status="error", msg="重试 %d 次后仍失败: %s" % (MAX_RETRY, last_err))
    return result


def write_log(res):
    """把每次运行结果追加写入脚本同目录的 checkin.log（本地核查用，不含 token）。"""
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.log")
        line = "%s | status=%s | action=%s | msg=%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            res.get("status"), res.get("action"), res.get("msg"))
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 写日志失败绝不影响签到


# ---------------- 多账号批量 ----------------

MULTI_SLEEP_RANGE = (3, 10)  # 账号间随机间隔秒数（降低批量请求特征）


def run_multi(check_only, accounts):
    """依次对多个账号执行签到；账号之间加随机间隔，避免固定节律的批量请求。

    返回 (汇总结果 agg, 明细列表 results)。汇总结果不含任何 token，可安全输出。
    """
    results = []
    for i, acc in enumerate(accounts):
        if i > 0:
            time.sleep(random.uniform(MULTI_SLEEP_RANGE[0], MULTI_SLEEP_RANGE[1]))
        res = run(check_only, token=acc.get("token"),
                  domain=acc.get("domain"), label=acc.get("name"))
        res["detail"]["account_name"] = acc.get("name") or ("account%d" % (i + 1))
        res["detail"]["index"] = i + 1
        results.append(res)
        write_log(res)  # 每个账号单独留痕，便于排查

    total = len(results)
    ok_list = [r for r in results if r.get("status") == "ok"]
    bad_list = [r for r in results if r.get("status") != "ok"]
    signed_new = sum(1 for r in ok_list if r.get("action") == "clicked")
    already = sum(1 for r in ok_list if r.get("action") == "skip_already_signed")
    summary = ("多账号签到完成：共 %d 个账号，成功 %d（新签 %d / 已签跳过 %d），失败 %d"
               % (total, len(ok_list), signed_new, already, len(bad_list)))
    agg = {
        "status": "ok" if not bad_list else "error",
        "action": "multi",
        "points": None,
        "balance": None,
        "msg": summary,
        "detail": {
            "multi": True,
            "total": total,
            "ok": len(ok_list),
            "failed": len(bad_list),
            "accounts": [
                {"index": r["detail"].get("index"),
                 "name": r["detail"].get("account_name"),
                 "status": r.get("status"),
                 "action": r.get("action"),
                 "msg": r.get("msg"),
                 "balance": r.get("balance")}
                for r in results
            ],
        },
    }
    return agg, results


def notify_multi(agg, results):
    """多账号模式推送：任一账号失败必推；全部成功仅当 success_notify=true 时推。

    内容只含账号名（或脱敏 token 形态）与签到结论，不含任何凭据。
    """
    cfg = load_notify_config()
    if not cfg or cfg.get("enabled") is False:
        return
    any_fail = agg.get("status") != "ok"
    if not any_fail and not cfg.get("success_notify"):
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    title = ("⚠️ WorkBuddy 多账号签到 · 有失败" if any_fail
             else "✅ WorkBuddy 多账号签到 · 全部成功")
    lines = [
        "### %s" % title,
        "",
        "> **时间**：%s" % now,
        "> **汇总**：%s" % agg.get("msg", ""),
        "",
        "| 账号 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for r in results:
        mark = "✅" if r.get("status") == "ok" else "❌"
        lines.append("| %s %s | %s | %s |" % (
            mark, r["detail"].get("account_name"),
            "成功" if r.get("status") == "ok" else "失败",
            r.get("msg", "")))
    content = "\n".join(lines) + "\n"
    agg["detail"]["notify"] = _dispatch_channels(cfg, title, content)


# ---------------- 环境自检与配置模板 ----------------

def _has_desktop_session():
    """best-effort 探测当前是否存在桌面图形会话（影响系统通知能否弹出）。

    探测失败或无法确定时返回 "unknown"，绝不影响签到主流程。
    """
    try:
        if sys.platform.startswith("win"):
            # SM_REMOTESESSION：1=远端会话（通常为无桌面/服务态），0=本地控制台
            try:
                import ctypes
                return "yes" if ctypes.windll.user32.GetSystemMetrics(0x1000) == 0 else "remote/no"
            except Exception:
                return "unknown"
        elif sys.platform == "darwin":
            # macOS 桌面环境一般存在；无法可靠区分锁屏，保守返回 yes
            return "yes"
        else:
            # Linux/类 Unix：有 $DISPLAY 或 $WAYLAND_DISPLAY 通常代表有桌面
            if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
                return "yes"
            return "no"
    except Exception:
        return "unknown"


def write_config_example():
    """生成 notify_config.json.example 模板（不含任何真实密钥，可放心查看/转发）。"""
    path = NOTIFY_CONFIG + ".example"
    example = {
        "enabled": True,
        "success_notify": False,
        "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=替换为你的群机器人KEY",
        "pushplus_token": "替换为你的PushPlus_token（个人微信推送）",
        "bark_url": "https://api.day.app/替换为你的Bark_KEY/",
        "serverchan_sendkey": "替换为你的Server酱SendKey（方糖，个人微信推送，形如 SCTxxxxx）"
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(example, f, ensure_ascii=False, indent=2)
    return path


def diagnose():
    """环境自检：检查 Python / 登录态 / 网络 / 桌面会话 / 微信配置。

    只读、不触发任何签到请求，便于用户首次安装后快速确认「能不能用」。
    返回结构化 dict，由 main() 以 JSON 打印。
    """
    report = {"version": VERSION, "python": {}, "auth": {}, "network": {}, "desktop": {}, "notify_config": {}}

    # 1) Python 版本
    report["python"] = {
        "version": sys.version.split()[0],
        "ok": sys.version_info >= (3, 6),
        "note": "" if sys.version_info >= (3, 6) else "低于 3.6，请升级 Python 或改用 WorkBuddy 托管 Python",
    }

    # 2) 凭据来源：优先环境变量，其次登录态文件
    env_token, env_domain = _env_token()
    if env_token:
        report["auth"]["source"] = "env"
        report["auth"]["found"] = True
        report["auth"]["token_present"] = True
        report["auth"]["domain"] = env_domain
        report["auth"]["path"] = "(env: WORKBUDDY_ACCESS_TOKEN)"
    else:
        report["auth"]["source"] = "file"
        auth_path = find_auth_file()
        if auth_path:
            report["auth"]["found"] = True
            report["auth"]["path"] = auth_path
            try:
                token, domain = load_token(auth_path)
                report["auth"]["token_present"] = True
                report["auth"]["domain"] = domain
                report["auth"]["expired"] = False
            except Exception as e:
                report["auth"]["token_present"] = False
                report["auth"]["error"] = str(e)
                report["auth"]["expired"] = ("过期" in str(e))
        else:
            report["auth"]["found"] = False
            report["auth"]["hint"] = ("未找到登录态文件，也未设置 WORKBUDDY_ACCESS_TOKEN 环境变量；"
                                      "请登录 WorkBuddy 客户端或在 CI 中配置该 Secret")

    # 3) 网络连通性（DNS 解析 best-effort）
    domain = report["auth"].get("domain") or "www.codebuddy.cn"
    try:
        ip = socket.gethostbyname(domain)
        report["network"]["dns_ok"] = True
        report["network"]["host"] = domain
        report["network"]["resolved_ip"] = ip
    except Exception as e:
        report["network"]["dns_ok"] = False
        report["network"]["host"] = domain
        report["network"]["error"] = str(e)

    # 4) 桌面会话（影响系统通知弹窗）
    report["desktop"]["session"] = _has_desktop_session()
    report["desktop"]["note"] = (
        "存在桌面会话，系统通知可正常弹出" if report["desktop"]["session"] == "yes"
        else "未检测到桌面会话（如锁屏/无 GUI 服务态），系统通知可能不弹出；stdout 与 checkin.log 仍可记录结果"
    )

    # 5) 微信推送配置
    cfg = load_notify_config()
    if cfg is None:
        report["notify_config"]["present"] = False
        report["notify_config"]["hint"] = "未配置（可选）；运行 --init-config 生成模板"
    else:
        report["notify_config"]["present"] = True
        report["notify_config"]["enabled"] = cfg.get("enabled", True)
        report["notify_config"]["success_notify"] = bool(cfg.get("success_notify"))
        channels = [k for k in ("wecom_webhook", "pushplus_token", "bark_url", "serverchan_sendkey") if cfg.get(k)]
        report["notify_config"]["channels"] = channels
        # 简单校验：enabled 但无通道 = 配了也不会推
        if cfg.get("enabled", True) and not channels:
            report["notify_config"]["warn"] = "enabled=true 但未填写任何通道，推送不会生效"

    return report


USAGE = (
    "WorkBuddy签到助手（接口直签）\n\n"
    "用法：\n"
    "  python workbuddy_checkin.py                # 查询今日状态 + 必要时领取\n"
    "  python workbuddy_checkin.py --check-only   # 仅查询状态（只读，不领取）\n"
    "  python workbuddy_checkin.py --no-notify    # 跳过全部推送与桌面通知（调试用）\n"
    "  python workbuddy_checkin.py --diagnose     # 环境自检（Python/登录态/网络/桌面会话/微信配置）\n"
    "  python workbuddy_checkin.py --init-config  # 生成 notify_config.json.example 模板\n"
    "  python workbuddy_checkin.py --help         # 显示本帮助\n"
    "  python workbuddy_checkin.py --version      # 显示版本号\n\n"
    "退出码：成功 0 / 失败 1（便于自动化判断是否推送告警）\n"
    "签到成功后会在结果中展示当前积分余额（若接口返回 balance / total_credit 等字段）。\n"
    "微信推送开关见 ~/.workbuddy/scripts/notify_config.json 的 \"success_notify\" 字段。\n\n"
    "多账号批量：设置环境变量 WORKBUDDY_TOKENS 后自动切换为批量模式，\n"
    "  值为 JSON 数组（推荐用 scripts/build_tokens_json.py 生成），例如：\n"
    '  [{"name": "主号", "token": "..."}, {"name": "小号", "token": "..."}]\n'
    "  逐个账号签到，账号间随机间隔 3~10 秒，任一失败退出码为 1。\n"
)


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)
    if "--version" in sys.argv:
        print("workbuddy_checkin %s" % VERSION)
        sys.exit(0)
    # 环境自检：只读、不签到，便于首次安装后确认可用性
    if "--diagnose" in sys.argv:
        rep = diagnose()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        sys.exit(0)
    # 生成微信推送配置模板（可选）
    if "--init-config" in sys.argv:
        p = write_config_example()
        print("已生成配置模板：%s" % p)
        print("请复制为 notify_config.json 并填入你的微信通道密钥（或使用默认值保持关闭）。")
        sys.exit(0)
    check_only = "--check-only" in sys.argv
    no_notify = "--no-notify" in sys.argv

    # 多账号批量模式：设置了 WORKBUDDY_TOKENS 时走批量流程（None = 未设置，走单账号）
    accounts = _accounts_from_env()
    if accounts is not None:
        if not accounts:
            res = {"status": "error", "action": None, "points": None,
                   "msg": ("WORKBUDDY_TOKENS 已设置但未解析到任何有效账号"
                           "（应为 JSON 数组，元素为 token 字符串或 {name, token, domain} 对象）"),
                   "detail": {"multi": True}}
            if not no_notify:
                try:
                    notify_failure(res)
                except Exception:
                    pass
            print(json.dumps(res, ensure_ascii=False, indent=2))
            write_log(res)
            sys.exit(1)
        try:
            agg, results = run_multi(check_only, accounts)
        except Exception as e:
            agg = {"status": "error", "action": "multi", "points": None,
                   "msg": "多账号流程未捕获异常: %s" % e, "detail": {"multi": True}}
            results = []
        if not no_notify:
            try:
                notify_multi(agg, results)
            except Exception:
                pass  # 推送失败不影响签到结果与退出码
        print(json.dumps(agg, ensure_ascii=False, indent=2))
        write_log(agg)
        # 全部账号成功才算成功（任一失败返回非 0，触发 GitHub 失败通知）
        sys.exit(0 if agg.get("status") == "ok" else 1)

    try:
        res = run(check_only)
    except Exception as e:
        res = {"status": "error", "action": None, "points": None,
               "msg": "脚本未捕获异常: %s" % e, "detail": {}}
    # 输出结果（不含任何真实 token）
    # 失败推送（配置缺失则跳过；--no-notify 用于调试）
    if not no_notify and res.get("status") != "ok":
        try:
            notify_failure(res)
        except Exception:
            pass  # 推送失败不影响签到结果与退出码
    # 成功推送（仅当 notify_config.json 中 success_notify=true；--no-notify 用于调试）
    if not no_notify and res.get("status") == "ok":
        try:
            notify_success(res)
        except Exception:
            pass  # 推送失败不影响签到结果与退出码
    # 系统桌面通知（跨平台 toast / 气球；--no-notify 一并抑制；失败静默）
    if not no_notify:
        try:
            notify_system(res)
        except Exception:
            pass
    # 输出结果（不含任何真实 token）—— 移后以便包含推送状态
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # 本地运行日志（系统定时任务无对话汇报，靠它核查）
    write_log(res)
    # 退出码：成功 0，失败 1，便于自动化判断是否推送告警
    sys.exit(0 if res.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
