#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多账号 token 采集工具：把本机 WorkBuddy 登录态采集进一个本地账号库，
并生成可直接粘贴到 GitHub Secret「WORKBUDDY_TOKENS」的 JSON 数组。

背景：
  WorkBuddy 客户端同一时间只保留一个登录账号。要配置多个账号时，按下面流程
  逐个操作即可（每换一个账号登录，就执行一次 --name 采集）：
    1. 打开 WorkBuddy 客户端，登录账号 A
    2. 运行  python scripts/build_tokens_json.py --name 主号
    3. 客户端退出登录，换成账号 B 登录
    4. 运行  python scripts/build_tokens_json.py --name 小号
    5. ...重复直到所有账号采集完毕
    6. 运行  python scripts/build_tokens_json.py --push   （需 gh CLI）
       或        python scripts/build_tokens_json.py --print   （手动复制到 Secret）

用法：
  python build_tokens_json.py --name 名称        # 采集当前登录账号（同名覆盖更新）
  python build_tokens_json.py --backup 名称      # 备份完整登录态（客户端免验证码切换用）
  python build_tokens_json.py --use 名称         # 切换客户端到指定账号（先完全退出客户端！）
  python build_tokens_json.py --current          # 查看客户端当前登录的是谁
  python build_tokens_json.py --list             # 查看已采集账号（token 脱敏）
  python build_tokens_json.py --remove 名称      # 从账号库移除某账号
  python build_tokens_json.py --print            # 输出 WORKBUDDY_TOKENS JSON（含明文 token，仅限本人操作）
  python build_tokens_json.py --push             # 通过 gh CLI 直接写入 Secret WORKBUDDY_TOKENS
  python build_tokens_json.py --out 文件路径     # 把 JSON 写入文件而不是打印（配合手动上传）

存储位置：
  ~/.workbuddy/scripts/wb_tokens.json               （token 账号库，供云端签到 Secret）
  ~/.workbuddy/scripts/wb_auth_files/<名称>.info    （完整登录态备份，供客户端切换）

安全说明：
  - token 等同于账号登录凭据：--print 的输出不要发到聊天工具 / 公开仓库
  - --push 通过 stdin 传值给 gh，token 不会出现在进程命令行参数中
  - token 约 2 个月过期：到期后对过期账号重新登录一次，再执行 --name 覆盖更新即可
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

STORE_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "scripts", "wb_tokens.json")
AUTH_BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "scripts", "wb_auth_files")
SECRET_NAME = "WORKBUDDY_TOKENS"


def _auth_filename(variant):
    """国内版: workbuddy-desktop.info；国际版(AI): workbuddy-desktop-ai.info"""
    return "workbuddy-desktop-ai.info" if variant == "ai" else "workbuddy-desktop.info"


def find_auth_file(variant="cn"):
    """定位当前登录态文件（与 workbuddy_checkin.py 的探测逻辑保持一致）。"""
    fname = _auth_filename(variant)
    home = os.path.expanduser("~")
    cands = []
    if sys.platform.startswith("win"):
        for env in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env, "")
            if base:
                cands.append(os.path.join(
                    base, "CodeBuddyExtension", "Data", "Public",
                    "auth", fname))
    elif sys.platform == "darwin":
        cands.append(os.path.join(
            home, "Library", "Application Support", "CodeBuddyExtension",
            "Data", "Public", "auth", fname))
    else:
        cands.append(os.path.join(
            home, ".config", "CodeBuddyExtension", "Data", "Public",
            "auth", fname))
    cands.append(os.path.join(home, ".workbuddy", "auth", fname))
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def load_current_auth(variant="cn"):
    """读取当前登录账号的 (token, domain)。"""
    path = find_auth_file(variant)
    if not path:
        return None, None, None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    auth = data.get("auth", {})
    token = auth.get("accessToken")
    default_domain = "www.workbuddy.ai" if variant == "ai" else "www.codebuddy.cn"
    domain = auth.get("domain") or default_domain
    return token, domain, path


def mask(t):
    return t[:6] + "..." + t[-4:] if t else "<empty>"


def load_store():
    if not os.path.isfile(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("accounts", []) if isinstance(data, dict) else []
    except Exception:
        return []


def save_store(accounts):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)


def build_json(accounts):
    """生成 WORKBUDDY_TOKENS 的值：[{name, token, domain}, ...]（domain 可省略）。"""
    items = []
    for a in accounts:
        item = {"name": a.get("name"), "token": a.get("token")}
        if a.get("domain"):
            item["domain"] = a.get("domain")
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def cmd_capture(name, variant="cn"):
    token, domain, path = load_current_auth(variant)
    if not token:
        print("✗ 未找到本机登录态文件或未登录，请先在 WorkBuddy 客户端登录要采集的账号")
        sys.exit(1)
    accounts = load_store()
    entry = {"name": name, "token": token, "domain": domain,
             "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    replaced = False
    for i, a in enumerate(accounts):
        if a.get("name") == name:
            accounts[i] = entry
            replaced = True
            break
    if not replaced:
        accounts.append(entry)
    save_store(accounts)
    print("✓ 已%s账号「%s」（token %s，domain %s）" %
          ("更新" if replaced else "采集", name, mask(token), domain))
    print("  来源：%s" % path)
    print("  账号库现有 %d 个账号（--list 查看，--push 写入 GitHub）" % len(accounts))
    # 同名不同 token 的旧账号提醒：token 过期重新登录后用同名覆盖即可


# ---------------- 客户端账号切换（整文件备份/恢复） ----------------

def _auth_backup_path(name):
    return os.path.join(AUTH_BACKUP_DIR, name + ".info")


def _sync_token_store(name, token, domain):
    """把采集到的 token/domain 同步进 token 账号库（签到用），返回是否更新。"""
    accounts = load_store()
    entry = {"name": name, "token": token, "domain": domain,
             "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for i, a in enumerate(accounts):
        if a.get("name") == name:
            accounts[i] = entry
            save_store(accounts)
            return "更新"
    accounts.append(entry)
    save_store(accounts)
    return "采集"


def _name_by_token(token):
    """按 token 反查账号库里的名字（识别当前登录的是哪个已采集账号）。"""
    for a in load_store():
        if a.get("token") == token:
            return a.get("name")
    return None


def cmd_backup(name, variant="cn"):
    """把当前登录态【整份文件】备份为指定名称（用于客户端免验证码切换）。"""
    token, domain, path = load_current_auth(variant)
    if not path or not token:
        print("✗ 未找到本机登录态文件或未登录，请先登录要备份的账号")
        sys.exit(1)
    os.makedirs(AUTH_BACKUP_DIR, exist_ok=True)
    dst = _auth_backup_path(name)
    shutil.copyfile(path, dst)
    action = _sync_token_store(name, token, domain)
    print("✓ 已备份「%s」的完整登录态 -> %s" % (name, dst))
    print("  同时已%s token 账号库（token %s，domain %s）" % (action, mask(token), domain))
    n = len([f for f in os.listdir(AUTH_BACKUP_DIR) if f.endswith(".info")])
    print("  完整登录态备份现有 %d 份（--list 查看，--use 名称 一键切换）" % n)


def cmd_use(name, variant="cn"):
    """把指定账号的完整登录态恢复到客户端（切换账号，免验证码）。

    ⚠️ 使用前必须完全退出 WorkBuddy 客户端（托盘图标也要退出），
    否则运行中的客户端可能在退出时把内存里的旧登录态写回文件，导致切换失效。
    """
    src = _auth_backup_path(name)
    if not os.path.isfile(src):
        avail = sorted(f[:-5] for f in os.listdir(AUTH_BACKUP_DIR)) \
            if os.path.isdir(AUTH_BACKUP_DIR) else []
        print("✗ 没有名为「%s」的登录态备份" % name)
        if avail:
            print("  可用备份：%s" % "、".join(avail))
        print("  先登录该账号后运行 --backup %s 生成备份" % name)
        sys.exit(1)
    cur_path = find_auth_file(variant)
    if not cur_path:
        print("✗ 未找到客户端登录态文件路径，无法切换")
        sys.exit(1)
    # 安全网：切换前把当前登录态存为 _previous，坏了可随时 --use _previous 还原
    prev = _auth_backup_path("_previous")
    try:
        shutil.copyfile(cur_path, prev)
    except Exception as e:
        print("✗ 备份当前登录态失败（中止切换，未做任何修改）：%s" % e)
        sys.exit(1)
    shutil.copyfile(src, cur_path)
    print("✓ 已切换为「%s」（原登录态已存为 _previous，可 --use _previous 还原）" % name)
    print("  下一步：启动 WorkBuddy 客户端即可，无需验证码")
    print("  ⚠️ 若客户端提示重新登录：登录后运行 --backup %s 刷新这份备份" % name)


def cmd_current():
    """显示客户端当前登录的是哪个账号（按 token 反查已采集的名字）。"""
    token, domain, path = load_current_auth()
    if not token:
        print("✗ 当前未登录或登录态文件缺失")
        sys.exit(1)
    name = _name_by_token(token)
    if name:
        label = name
    else:
        label = ("（未匹配到已采集账号）\n" 
                 "  可能是：a) 新账号，还没采集过；b) 客户端自动刷新了 token（正常现象）。\n"
                 "  前者用 --name 或 --backup 采集；后者是已采集账号的话，"
                 "用 --backup 同名 覆盖刷新即可")
    print("当前客户端登录：%s" % label)
    print("  token %s   domain %s" % (mask(token), domain))
    print("  文件：%s" % path)


def cmd_list():
    accounts = load_store()
    if not accounts:
        print("账号库为空。先登录账号后运行：python %s --name 账号名" % os.path.basename(__file__))
        return
    backups = set()
    if os.path.isdir(AUTH_BACKUP_DIR):
        backups = {f[:-5] for f in os.listdir(AUTH_BACKUP_DIR) if f.endswith(".info")}
    print("账号库：%s（共 %d 个）" % (STORE_PATH, len(accounts)))
    print("★ = 已有完整登录态备份，可用 --use 名称 在客户端免验证码切换")
    for a in accounts:
        star = "★" if a.get("name") in backups else " "
        print("  %s %-16s token %s   domain %-20s 更新于 %s"
              % (star, a.get("name"), mask(a.get("token")),
                 a.get("domain") or "(默认)", a.get("updated_at") or "?"))


def cmd_remove(name):
    accounts = load_store()
    before = len(accounts)
    accounts = [a for a in accounts if a.get("name") != name]
    if len(accounts) == before:
        print("✗ 账号库中没有名为「%s」的账号" % name)
        sys.exit(1)
    save_store(accounts)
    print("✓ 已移除账号「%s」，剩余 %d 个" % (name, len(accounts)))


def cmd_print(out_path=None):
    accounts = load_store()
    if not accounts:
        print("✗ 账号库为空，先用 --name 采集账号")
        sys.exit(1)
    payload = build_json(accounts)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload)
        print("✓ 已写入 %s（共 %d 个账号）" % (out_path, len(accounts)))
        print("  ⚠️ 该文件含明文 token，仅放在本机，不要提交进仓库或发送给他人")
    else:
        print(payload)
        print()
        print("(↑ 把上面这行 JSON 粘贴到 GitHub 仓库 Settings → Secrets → Actions →"
              " New repository secret，名称填 %s)" % SECRET_NAME)


def cmd_push(repo=None, dry_run=False):
    if not shutil.which("gh"):
        print("✗ 未找到 gh 命令。请安装 GitHub CLI（https://cli.github.com/）并 gh auth login，"
              "或改用 --print 手动粘贴")
        sys.exit(1)
    accounts = load_store()
    if not accounts:
        print("✗ 账号库为空，先用 --name 采集账号")
        sys.exit(1)
    payload = build_json(accounts)
    cmd = ["gh", "secret", "set", SECRET_NAME]
    if repo:
        cmd += ["--repo", repo]
    if dry_run:
        print("[dry-run] 将写入 Secret：%s（%d 个账号）" % (SECRET_NAME, len(accounts)))
        return
    r = subprocess.run(cmd, input=payload.encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print("✗ 写入失败：%s" % r.stderr.decode("utf-8", "replace").strip())
        sys.exit(1)
    print("✓ 已写入 Secret：%s（%d 个账号）" % (SECRET_NAME, len(accounts)))
    print("  下一步：到仓库 Actions 页面手动 Run workflow 一次验证结果")


def main():
    ap = argparse.ArgumentParser(
        description="WorkBuddy 多账号 token 采集工具（生成/维护 WORKBUDDY_TOKENS）")
    ap.add_argument("--name", help="采集当前登录账号到账号库（同名覆盖）")
    ap.add_argument("--list", action="store_true", help="查看已采集账号（token 脱敏）")
    ap.add_argument("--remove", help="从账号库移除指定名称的账号")
    ap.add_argument("--backup", metavar="名称",
                    help="备份当前登录态整份文件（用于客户端免验证码切换），并同步 token 账号库")
    ap.add_argument("--use", metavar="名称",
                    help="切换客户端到指定账号（需先完全退出客户端；切换前自动留 _previous 还原点）")
    ap.add_argument("--current", action="store_true", help="显示客户端当前登录的账号")
    ap.add_argument("--ai", action="store_true",
                    help="操作国际版 WorkBuddy AI（登录态文件 workbuddy-desktop-ai.info）")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="输出 WORKBUDDY_TOKENS JSON（含明文 token）")
    ap.add_argument("--out", help="配合 --print：把 JSON 写入文件而不是打印")
    ap.add_argument("--push", action="store_true", help="通过 gh CLI 写入 GitHub Secret")
    ap.add_argument("--repo", help="目标仓库 owner/repo（配合 --push，默认取当前 git remote）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --push：只预览不写入")
    args = ap.parse_args()

    print("=" * 56)
    print("WorkBuddy 多账号 token 采集工具")
    print("=" * 56)

    variant = "ai" if getattr(args, "ai", False) else "cn"
    if args.name:
        cmd_capture(args.name, variant)
    elif args.backup:
        cmd_backup(args.backup, variant)
    elif args.use:
        cmd_use(args.use, variant)
    elif args.current:
        cmd_current(variant)
    elif args.remove:
        cmd_remove(args.remove)
    elif args.push:
        cmd_push(args.repo, args.dry_run)
    elif args.do_print or args.out:
        cmd_print(args.out)
    else:
        cmd_list()
        print()
        print("常用流程：")
        print("  1. 客户端登录账号 A →  python %s --name 主号" % os.path.basename(__file__))
        print("  2. 换登账号 B      →  python %s --name 小号" % os.path.basename(__file__))
        print("  3. python %s --push   （或 --print 手动粘贴到 Secret）" % os.path.basename(__file__))


if __name__ == "__main__":
    main()
