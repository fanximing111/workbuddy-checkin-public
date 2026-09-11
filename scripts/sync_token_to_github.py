#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本机 WorkBuddy 登录态同步到 GitHub 仓库 Secrets（一键配置 / 续期）。

用途：
  - 首次配置：把本机 accessToken + domain 写入 GitHub Secrets，供 Actions 使用
  - 续期：token 过期后（默认有效期约 2 个月）重新执行本脚本即可覆盖更新

依赖：
  - 本机已安装并登录 WorkBuddy 客户端（提供登录态文件）
  - 已安装 GitHub CLI（gh）并执行过 gh auth login
    https://cli.github.com/

用法：
  # 自动识别当前仓库
  python sync_token_to_github.py

  # 指定仓库
  python sync_token_to_github.py --repo owner/repo

  # 顺带写入通知通道（可选，留空则不动）
  python sync_token_to_github.py --wecom "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
  python sync_token_to_github.py --pushplus "你的token"
  python sync_token_to_github.py --bark "https://api.day.app/你的KEY/"
  python sync_token_to_github.py --success-notify

  # 只预览，不真正写入
  python sync_token_to_github.py --dry-run

安全说明：
  - 本脚本只读取本机登录态文件，不修改、不上传到除 GitHub Secrets 之外的任何地方
  - 全程不打印真实 token（仅打印脱敏形态，不打印长度）
  - 通过 stdin 传值给 gh，避免密钥出现在进程命令行参数中
  - 未安装 gh 时自动降级为「手动配置指引」，不会卡住，也不会泄露凭据
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

AUTH_CANDIDATES_WIN = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
    os.path.join(os.environ.get("APPDATA", ""), "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
]


def find_auth_file():
    home = os.path.expanduser("~")
    cands = list(AUTH_CANDIDATES_WIN)
    if sys.platform == "darwin":
        cands.append(os.path.join(home, "Library", "Application Support", "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"))
    else:
        cands.append(os.path.join(home, ".config", "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"))
    cands.append(os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"))
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def load_auth(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    auth = data.get("auth", {})
    token = auth.get("accessToken")
    domain = auth.get("domain") or "www.codebuddy.cn"
    if not token:
        raise RuntimeError("登录态文件中未找到 accessToken，请确认 WorkBuddy 已登录")
    return token, domain


def mask(t):
    return t[:6] + "..." + t[-4:] if t else "<empty>"


def gh_available():
    """检测 gh 是否可用：先 PATH，再探测常见安装位置。"""
    if shutil.which("gh"):
        return True
    home = os.path.expanduser("~")
    candidates = [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        os.path.join(home, "AppData", "Local", "GitHubCLI", "gh.exe"),
        os.path.join(home, "AppData", "Local", "Programs", "GitHub CLI", "gh.exe"),
    ]
    return any(os.path.isfile(c) for c in candidates)


def gh_set_secret(repo, name, value, dry_run=False):
    """通过 stdin 写入 Secret，避免密钥出现在命令行参数中。

    安全要点：
      - 值经 stdin 传递，不出现在进程列表（ps）里
      - 输出只提示「已写入 Secret：<名称>」，绝不回显值或长度
    """
    cmd = ["gh", "secret", "set", name]
    if repo:
        cmd += ["--repo", repo]
    if dry_run:
        # 只提示将写入哪个 Secret，不输出值 / 长度（长度亦可作为指纹信息）
        print("  [dry-run] 将写入 Secret：%s" % name)
        return True
    try:
        r = subprocess.run(cmd, input=value.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip()
            print("  ✗ 写入 %s 失败：%s" % (name, err))
            return False
        print("  ✓ 已写入 Secret：%s" % name)
        return True
    except FileNotFoundError:
        print("  ✗ 未找到 gh 命令，请先安装 GitHub CLI：https://cli.github.com/")
        return False
    except Exception as e:
        print("  ✗ 写入 %s 异常：%s" % (name, e))
        return False


def find_git():
    """定位 git 可执行文件：优先 PATH，其次常见安装位置 / WorkBuddy 自带 PortableGit。

    为什么需要它：某些环境下 PATH 异常（如 WorkBuddy 内置 Bash 的 PATH 缺失），
    直接调用 "git" 会 FileNotFoundError，这里做主动探测以提升健壮性。
    """
    found = shutil.which("git")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        os.path.join(home, "AppData", "Local", "Programs", "Git", "cmd", "git.exe"),
    ]
    # WorkBuddy 自带 PortableGit（版本号不确定，做通配搜索）
    candidates += sorted(glob.glob(os.path.join(
        home, ".workbuddy", "binaries", "PortableGit", "versions", "*", "cmd", "git.exe")))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def detect_repo(git_path=None):
    """尝试从当前目录的 git remote 推断仓库（owner/repo）。"""
    git_exe = git_path or find_git() or "git"
    try:
        r = subprocess.run([git_exe, "remote", "get-url", "origin"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            return None
        url = r.stdout.decode("utf-8", "replace").strip()
        # git@github.com:owner/repo.git 或 https://github.com/owner/repo.git
        if "github.com" not in url:
            return None
        part = url.split("github.com")[-1].lstrip(":/").rstrip("/")
        if part.endswith(".git"):
            part = part[:-4]
        return part
    except Exception:
        return None


def print_manual_guide(token, domain, repo, channels):
    """gh 不可用时的降级方案：打印手动配置指引（不含凭据明文）。"""
    print()
    print("=" * 56)
    print("⚠️  未检测到 GitHub CLI（gh），无法自动写入 Secrets。")
    print("    请在浏览器中按以下步骤手动配置（共 %d 项）：" % (2 + len(channels)))
    print("=" * 56)
    print()
    print("打开：仓库 → Settings → Secrets and variables → Actions → New repository secret")
    if repo:
        print("仓库地址：https://github.com/%s/settings/secrets/actions" % repo)
    print()
    print("需要添加的 Secret（名称 → 值来源）：")
    print("  1) WORKBUDDY_ACCESS_TOKEN")
    print("     → 值为你的登录态 accessToken；可运行下面的命令复制到剪贴板（不会打印到屏幕）：")
    print()
    if sys.platform.startswith("win"):
        print("       python scripts/copy_token_to_clipboard.py")
    else:
        print("       python scripts/copy_token_to_clipboard.py   # 需自行安装剪贴板工具，或手动打开登录态文件复制")
    print()
    print("  2) WORKBUDDY_DOMAIN")
    print("     → 值：%s" % domain)
    for i, (name, _) in enumerate(channels, start=3):
        print("  %d) %s" % (i, name))
    print()
    print("另外建议安装 gh 以获得更好的体验：https://cli.github.com/")
    print("安装后重新运行本脚本即可自动完成。")
    print()
    print("提示：也可直接打开登录态文件复制 accessToken（仅在本人设备上操作）：")
    print("  %s" % find_auth_file())


def main():
    ap = argparse.ArgumentParser(description="把本机 WorkBuddy 登录态同步到 GitHub Secrets")
    ap.add_argument("--repo", help="目标仓库 owner/repo（默认自动从 git remote 推断）")
    ap.add_argument("--wecom", help="企业微信群机器人 webhook（可选）")
    ap.add_argument("--pushplus", help="PushPlus token（可选）")
    ap.add_argument("--bark", help="Bark URL（可选）")
    ap.add_argument("--success-notify", action="store_true", help="签到成功也推送播报")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不真正写入")
    args = ap.parse_args()

    print("=" * 56)
    print("WorkBuddy 登录态 → GitHub Secrets 同步工具")
    print("=" * 56)

    has_gh = gh_available() or args.dry_run

    repo = args.repo or detect_repo()
    if repo:
        print("目标仓库：%s" % repo)
    else:
        print("未指定仓库且无法自动推断（可能未配置 git remote）；可用 --repo owner/repo 指定")

    auth_path = find_auth_file()
    if not auth_path:
        print("✗ 未找到本机登录态文件，请先登录 WorkBuddy 客户端")
        sys.exit(1)
    print("登录态文件：%s" % auth_path)

    token, domain = load_auth(auth_path)
    print("token（脱敏）：%s" % mask(token))
    print("接口域名：%s" % domain)
    print("-" * 56)

    # gh 不可用（且非 dry-run）→ 降级为手动配置指引，不在中途失败
    if not has_gh:
        channels = []
        if args.wecom:
            channels.append(("WECOM_WEBHOOK", args.wecom))
        if args.pushplus:
            channels.append(("PUSHPLUS_TOKEN", args.pushplus))
        if args.bark:
            channels.append(("BARK_URL", args.bark))
        if args.success_notify:
            channels.append(("SUCCESS_NOTIFY", "true"))
        print_manual_guide(token, domain, repo, channels)
        sys.exit(0)

    ok = True
    print("[1/3] 写入 accessToken 与 domain")
    ok &= gh_set_secret(repo, "WORKBUDDY_ACCESS_TOKEN", token, args.dry_run)
    ok &= gh_set_secret(repo, "WORKBUDDY_DOMAIN", domain, args.dry_run)

    print("[2/3] 写入通知通道（仅填写了的）")
    any_channel = False
    if args.wecom:
        ok &= gh_set_secret(repo, "WECOM_WEBHOOK", args.wecom, args.dry_run); any_channel = True
    if args.pushplus:
        ok &= gh_set_secret(repo, "PUSHPLUS_TOKEN", args.pushplus, args.dry_run); any_channel = True
    if args.bark:
        ok &= gh_set_secret(repo, "BARK_URL", args.bark, args.dry_run); any_channel = True
    if args.success_notify:
        ok &= gh_set_secret(repo, "SUCCESS_NOTIFY", "true", args.dry_run); any_channel = True
    if not any_channel:
        print("  （未指定任何通知通道，跳过；仅失败时也不会推送）")

    print("[3/3] 完成")
    print("-" * 56)
    if ok:
        print("✓ 同步成功。")
        print("  下一步：在 GitHub 仓库 Actions 页面选中「WorkBuddy 每日自动签到」")
        print("         点击 Run workflow 手动跑一次，确认结果为 status=ok。")
    else:
        print("✗ 部分 Secret 写入失败，请检查 gh 登录状态与仓库权限（需 repo 写权限）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
