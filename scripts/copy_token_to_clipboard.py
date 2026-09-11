#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本机 WorkBuddy 登录态 accessToken 复制到剪贴板（用于手动粘贴到 GitHub Secrets）。

为什么需要它：
  未安装 GitHub CLI（gh）时，用户需手动到网页添加 Secret，
  但 accessToken 长达 1300+ 字符，肉眼选中复制极易漏字符，且直接打印到
  终端会留存在滚动历史 / 日志中。本脚本通过剪贴板传递，不打印内容。

安全说明：
  - 不打印 token 内容（仅提示已复制，并显示脱敏形态用于核对）
  - 只读登录态文件，不修改、不联网
  - 用完请自行清空剪贴板（脚本会提示）

用法：
  python copy_token_to_clipboard.py            # 复制 accessToken
  python copy_token_to_clipboard.py --domain   # 复制接口域名
"""

import argparse
import json
import os
import subprocess
import sys


def find_auth_file():
    home = os.path.expanduser("~")
    cands = []
    if sys.platform.startswith("win"):
        for env in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env, "")
            if base:
                cands.append(os.path.join(base, "CodeBuddyExtension", "Data",
                                          "Public", "auth", "workbuddy-desktop.info"))
    elif sys.platform == "darwin":
        cands.append(os.path.join(home, "Library", "Application Support",
                                  "CodeBuddyExtension", "Data", "Public", "auth",
                                  "workbuddy-desktop.info"))
    else:
        cands.append(os.path.join(home, ".config", "CodeBuddyExtension", "Data",
                                  "Public", "auth", "workbuddy-desktop.info"))
    cands.append(os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"))
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def mask(t):
    return t[:6] + "..." + t[-4:] if t else "<empty>"


def copy_to_clipboard(text):
    """跨平台写入剪贴板；成功返回 True。不依赖第三方库。"""
    try:
        if sys.platform.startswith("win"):
            # 通过 PowerShell 写入剪贴板（Set-Clipboard 在 Win10+ 可用）
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            return r.returncode == 0
        elif sys.platform == "darwin":
            r = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            return r.returncode == 0
        else:
            # Linux: 依次尝试 xclip / xsel / wl-copy
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
                try:
                    r = subprocess.run(cmd, input=text.encode("utf-8"),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                    if r.returncode == 0:
                        return True
                except FileNotFoundError:
                    continue
            return False
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="复制 accessToken / 域名到剪贴板（用于手动配置 GitHub Secrets）")
    ap.add_argument("--domain", action="store_true", help="复制接口域名而非 token")
    args = ap.parse_args()

    path = find_auth_file()
    if not path:
        print("✗ 未找到本机登录态文件，请先登录 WorkBuddy 客户端")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        auth = json.load(f).get("auth", {})

    domain = auth.get("domain") or "www.codebuddy.cn"

    if args.domain:
        if copy_to_clipboard(domain):
            print("✓ 已复制接口域名到剪贴板：%s" % domain)
            print("  → 粘贴到 Secret「WORKBUDDY_DOMAIN」")
        else:
            print("✗ 复制失败，请手动填写：%s" % domain)
        return

    token = auth.get("accessToken")
    if not token:
        print("✗ 登录态中未找到 accessToken，请确认已登录 WorkBuddy")
        sys.exit(1)

    if copy_to_clipboard(token):
        print("✓ 已复制 accessToken 到剪贴板（脱敏核对：%s）" % mask(token))
        print("  → 粘贴到 Secret「WORKBUDDY_ACCESS_TOKEN」")
        print()
        print("⚠️ 安全提醒：配置完成后请清空剪贴板，避免凭据残留。")
        print("   清空方法（Windows）：复制一段无关文本即可覆盖。")
    else:
        print("✗ 复制到剪贴板失败（可能缺少剪贴板工具或权限）。")
        print("  备选方案：手动打开下面的文件，复制其中 auth.accessToken 的值：")
        print("  %s" % path)
        sys.exit(1)


if __name__ == "__main__":
    main()
