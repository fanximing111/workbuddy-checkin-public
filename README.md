# WorkBuddy 每日自动签到 · GitHub Actions 版

> ☝️ **开源发布说明**：本仓库的代码（脚本 / workflow）本身不含任何凭据，可安全公开。
> 真实 token 只存于 GitHub Secrets，绝不会出现在代码或公开日志中（摘要已白名单过滤）。
> 因此本仓库既可设为 **private** 也可 **public** 开源；区别仅在于：
> public 仓库的 Actions 运行日志对所有人可见，但本项目已做多层脱敏，无敏感信息外泄。
> 仍建议在「Settings → Actions → General」中将工作流权限保持最小，并不要在本仓库的 Issue / PR 中粘贴任何 token。

用 GitHub Actions 定时执行 WorkBuddy「Buddy 加油站」每日签到。**接口直签，无需常驻本机、无需开机、无需 GUI**。

> 本目录由 WorkBuddy 签到助手（v2.0.0）改造而来：把「读本机登录态文件」改为「读环境变量 / Secrets」，
> 使脚本能在 GitHub 的云端 runner 上运行。

---

## 一、原理与限制（先读这段）

| 项目 | 说明 |
|---|---|
| 原理 | 从 GitHub Secrets 取 `accessToken`，直接 POST 官方接口 `checkin-activity-status` → `daily-checkin` |
| 每日 100 积分 | 签到成功领取 100 积分；已签到返回 `code=10001`，幂等不会重复领 |
| ✅ 优势 | 不依赖本机开机 / 客户端常驻 / 网络环境；换机、重装都不影响 |
| ⚠️ 限制 | **token 会过期**（本机实测有效期约 2 个月），过期后需重新执行同步脚本更新 Secret |
| ⚠️ 限制 | GitHub 定时任务在整点前后**可能延迟数分钟到数十分钟**，属平台正常现象，不影响结果 |
| ⚠️ 限制 | 仓库连续 60 天无活动，GitHub 会自动暂停定时任务，需手动重新启用 |

> 💡 **建议**：本机 WorkBuddy 自动化（每日 09:00）与 GitHub Actions 二者可**同时存在**，互为备份，已签到的会走幂等跳过分支，不会重复领。

---

## 二、目录结构

```
workbuddy-checkin-gha/
├── .github/workflows/checkin.yml      # 工作流定义（定时触发 + 手动触发）
├── scripts/
│   ├── workbuddy_checkin.py           # 签到主脚本（已支持环境变量读取 token）
│   ├── gen_notify_config.py           # 从环境变量生成推送配置（CI 专用）
│   ├── sync_token_to_github.py        # 本机一键同步 token 到 GitHub Secrets
│   ├── copy_token_to_clipboard.py     # 复制 token 到剪贴板（手动配置兜底）
│   └── summarize_result.py            # 生成白名单摘要（防止原始响应进日志）
├── requirements.txt                   # 无第三方依赖（仅 Python 标准库）
└── README.md
```

---

## 三、快速开始（三步）

### 步骤 1：把本目录推送到你的 GitHub 仓库

```bash
cd workbuddy-checkin-gha
git init
git add .
git commit -m "feat: WorkBuddy 每日自动签到 (GitHub Actions)"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 步骤 2：把 token 写入仓库 Secrets（一键）

需要先安装并登录 [GitHub CLI](https://cli.github.com/)（`gh auth login`），然后：

```bash
# 在仓库目录内执行，自动识别仓库
python scripts/sync_token_to_github.py

# 或显式指定仓库
python scripts/sync_token_to_github.py --repo <用户名>/<仓库名>
```

脚本会读取本机登录态 `workbuddy-desktop.info`，写入两个 Secret：

| Secret 名 | 内容 | 必填 |
|---|---|---|
| `WORKBUDDY_ACCESS_TOKEN` | 登录态中的 accessToken（JWT） | ✅ 必填 |
| `WORKBUDDY_DOMAIN` | 接口域名，本机实测 `copilot.tencent.com` | ✅ 必填 |

**没装 gh？脚本会自动降级为手动指引**，按提示操作即可：

1. 打开仓库 `Settings → Secrets and variables → Actions → New repository secret`
2. 添加 `WORKBUDDY_ACCESS_TOKEN`：运行下面命令把 token 复制到剪贴板（**不会打印到屏幕**，避免凭据留存于终端历史）

   ```bash
   python scripts/copy_token_to_clipboard.py
   ```

3. 添加 `WORKBUDDY_DOMAIN`：值填本机 `auth.domain`（也可用 `python scripts/copy_token_to_clipboard.py --domain` 复制）

> ⚠️ accessToken 长 1300+ 字符，**不要手动框选复制**（极易漏字符导致 401）。用剪贴板脚本或直接复制文件里的值。

### 步骤 3：手动跑一次验证

到仓库 **Actions** 页面 → 选择「WorkBuddy 每日自动签到」→ 点 **Run workflow**。
首次建议勾选 `check_only`（仅查询，不领取）先确认接口通，确认无误后再跑一次真实签到。

看到 `status: ok` 即配置成功。

---

## 四、可选：配置失败/成功推送通知

在步骤 2 的命令后追加参数，或手动在 Secrets 中添加：

| Secret 名 | 用途 | 获取方式 |
|---|---|---|
| `WECOM_WEBHOOK` | 企业微信群机器人 | 群设置 → 群机器人 → 添加 → 复制 Webhook |
| `PUSHPLUS_TOKEN` | 个人微信推送 | 注册 https://www.pushplus.plus ，在一对一推送处获取 |
| `BARK_URL` | iOS 推送 | 安装 Bark App，复制 `https://api.day.app/<KEY>/` |
| `SERVERCHAN_SENDKEY` | 个人微信推送（Server酱 / 方糖） | 微信扫码登录 https://sct.ftqq.com ，复制 SendKey（形如 `SCTxxxxx`） |
| `SUCCESS_NOTIFY` | 设为 `true` 时签到成功也播报（默认仅失败提醒） | 填 `true` 即可 |

```bash
python scripts/sync_token_to_github.py \
  --wecom "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
```

三个通道可同时配置；全部留空则静默运行，仅靠 Actions 页面查看结果。

---

## 五、运行结果说明

脚本输出 JSON，关键字段如下：

| status | action | 含义 | 处理 |
|---|---|---|---|
| `ok` | `clicked` | ✅ 本次领取成功 | 查看 `points`（本次积分）、`balance`（余额） |
| `ok` | `skip_already_signed` | 今日已签到，自动跳过 | 正常，无需处理 |
| `ok` | `skip_check_only` | 仅查询模式 | 查看 `detail.today_signed` |
| `error` | `failed` | ❌ 签到失败 | 查看 `msg`；常见原因见下 |

**失败常见原因**

| msg 关键词 | 原因 | 解决 |
|---|---|---|
| 401 / 未授权 / token 失效 | Secret 中的 token 已过期 | 重新执行 `sync_token_to_github.py` |
| 404 | 域名错误 | 检查 `WORKBUDDY_DOMAIN` 是否为本机 `auth.domain` 的值 |
| 网络错误 | runner 网络问题 | 重跑 workflow，或临时改用手动触发 |
| 未设置 WORKBUDDY_ACCESS_TOKEN | Secret 未配置 | 按步骤 2 配置 |

---

## 六、token 续期（约每 2 个月一次）

token 过期后 Actions 会失败并报警。续期只需在本机**重新登录 WorkBuddy 客户端**，然后重跑一次：

```bash
python scripts/sync_token_to_github.py --repo <用户名>/<仓库名>
```

脚本会覆盖旧 Secret，无需其他操作。

---

## 七、本地自测（可选）

```bash
# 1. 环境自检（读本机登录态）
python scripts/workbuddy_checkin.py --diagnose

# 2. 用环境变量模拟 CI 环境
export WORKBUDDY_ACCESS_TOKEN="你的token"
export WORKBUDDY_DOMAIN="copilot.tencent.com"
python scripts/workbuddy_checkin.py --check-only   # 只查不领
python scripts/workbuddy_checkin.py                # 查 + 必要时领取
```

---

## 八、安全说明

### 8.1 凭据保护

- 脚本**只读**登录态文件，不修改、不删除。
- token 通过 **GitHub Secrets 加密存储**，日志中永不回显（脚本输出脱敏为 `eyJhbG...xxxx`）。
- 通过 `gh secret set` 的 **stdin** 写入，避免密钥出现在进程命令行参数（`ps` 可见）中。
- 所有脚本**均不打印 token 内容，也不打印 token 长度**（长度亦属可用于指纹识别的信息）。
- 零第三方依赖，仅用 Python 标准库，降低供应链风险。
- 复制 token 用剪贴板脚本，避免凭据留存在终端回滚历史中。

### 8.2 日志脱敏（多层防护）

Actions 的运行日志与摘要**会长期留存且可能被他人查看**，因此做了多层防护：

| 层级 | 措施 |
|---|---|
| 脚本层 | 签到脚本输出的 `detail.token_masked` 已是脱敏形态；`auth_file` 只含路径不含凭据 |
| 摘要层 | 用 `summarize_result.py` **白名单过滤**，只输出 `status/action/points/balance/msg/domain/streak_days` 等结论字段，**不输出原始接口响应** `detail.status_resp` |
| 兜底层 | 摘要脚本把任何长度 ≥80 的 base64url 风格长串替换为 `<REDACTED>`，防止接口未来变更引入非预期字段 |

> 实测：原始 `result.json` 1585 字符 → 摘要 131 字符（缩减 92%），token / 路径 / requestId / 原始响应**全部被过滤**。

### 8.3 工作流安全

| 项 | 措施 |
|---|---|
| 权限 | `permissions: contents: read`（最小权限，不给写权限） |
| 触发 | 仅 `schedule` + `workflow_dispatch`，**无 `pull_request_target`**（防 PR 投毒） |
| 并发 | `concurrency` 固定组名，防止重复领取 |
| 超时 | `timeout-minutes: 10`，防挂死占用额度 |
| 退出码 | 用 `PIPESTATUS[0]` 精确取脚本退出码（管道会吞掉 `$?`），确保**失败必定触发告警**，不会静默成功 |

### 8.4 部署前检查清单

- [ ] 仓库设为 **private**（public 仓库的 Actions 日志默认公开可见）
- [ ] `notify_config.json` **未**被提交（`.gitignore` 已覆盖，仍建议 `git status` 确认）
- [ ] 确认提交的文件中无 token（可运行 `python ../security_audit.py` 自检）
- [ ] 首次部署先用 `check_only` 手动跑一次，确认 `status: ok`
- [ ] 配好失败通知（企微 / PushPlus / Server酱 / Bark），否则失败只能靠 GitHub 邮件

### 8.5 切勿做的事

- ❌ 不要把 token 硬编码进任何文件或 workflow。
- ❌ 不要把 `notify_config.json` 提交到仓库。
- ❌ 不要把 token 粘贴到聊天工具、Issue、PR 描述中。
- ❌ 不要在 public 仓库中长期保留含接口响应的日志。
- ⚠️ token 一旦疑似泄露：立即在本机退出并重新登录 WorkBuddy（使旧 token 失效），再更新 Secret。
