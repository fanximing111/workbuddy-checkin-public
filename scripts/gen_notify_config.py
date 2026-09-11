#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从环境变量生成 notify_config.json（GitHub Actions / CI 场景专用）。

脚本本体只从本地文件读 notify_config.json；在 CI 中没有该文件，
因此这里把放在 Secrets 里的通道凭据写成文件，供签到脚本按原有逻辑读取。

环境变量（任一为空即跳过该通道）：
  WECOM_WEBHOOK   企业微信群机器人 webhook
  PUSHPLUS_TOKEN  PushPlus token（个人微信推送）
  BARK_URL        Bark URL（iOS 推送）
  SERVERCHAN_SENDKEY  Server酱（方糖）SendKey（个人微信推送，形如 SCTxxxxx）
  SUCCESS_NOTIFY  签到成功是否也播报（"true"/"1"/"yes" 视为开启，默认关闭）

用法：
  python gen_notify_config.py <输出路径>

行为：
  - 未配置任何通道 → 不生成文件（脚本会静默跳过推送），退出码 0
  - 已配置通道     → 写入文件并打印已启用的通道名（绝不打印密钥内容）
"""

import json
import os
import sys


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on", "y")


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("用法: python gen_notify_config.py <输出路径>\n")
        sys.exit(2)

    out_path = sys.argv[1]

    wecom = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    pushplus = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    bark = (os.environ.get("BARK_URL") or "").strip()
    serverchan = (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()
    success_notify = _truthy(os.environ.get("SUCCESS_NOTIFY"))

    channels = []
    if wecom:
        channels.append("wecom_webhook")
    if pushplus:
        channels.append("pushplus_token")
    if bark:
        channels.append("bark_url")
    if serverchan:
        channels.append("serverchan_sendkey")

    if not channels:
        print("未配置任何通知通道（WECOM_WEBHOOK / PUSHPLUS_TOKEN / BARK_URL / SERVERCHAN_SENDKEY），跳过推送配置生成。")
        sys.exit(0)

    cfg = {
        "enabled": True,
        "success_notify": success_notify,
    }
    if wecom:
        cfg["wecom_webhook"] = wecom
    if pushplus:
        cfg["pushplus_token"] = pushplus
    if bark:
        cfg["bark_url"] = bark
    if serverchan:
        cfg["serverchan_sendkey"] = serverchan

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 只打印通道名，绝不含密钥
    print("已生成推送配置：%s" % out_path)
    print("已启用通道：%s" % ", ".join(channels))
    print("成功播报：%s" % ("开启" if success_notify else "关闭（仅失败时提醒）"))


if __name__ == "__main__":
    main()
