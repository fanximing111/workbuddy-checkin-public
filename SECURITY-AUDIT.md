# 安全审计报告 · WorkBuddy 签到 GitHub Actions 版

**审计时间**：2026-09-11 20:16
**审计范围**：`workbuddy-checkin-gha/` 全部 10 个文件
**审计方式**：自动化扫描 + 人工逐行审查 + 运行期实测验证
**审计结论**：✅ **可安全部署**（已修复 4 处发现的问题）

---

## 一、总体结论

| 维度 | 结果 |
|---|---|
| 凭据泄露 | ✅ 无（本机真实 token 未出现在任何交付文件中） |
| 危险代码 | ✅ 无（无 shell=True / eval / os.system / rm -rf / verify=False / curl\|sh） |
| 敏感文件入库 | ✅ 无（`notify_config.json` 未入库，`.gitignore` 已覆盖） |
| 日志脱敏 | ✅ 多层防护已建立并实测有效 |
| 工作流安全 | ✅ 最小权限、无投毒面、退出码正确传播 |
| **发现并修复的问题** | **4 处**（详见第三节） |

---

## 二、审计发现汇总

### 2.1 通过项

| 检查项 | 说明 |
|---|---|
| 真实 token 比对 | 用本机实际 token（1355 字符）全文/片段比对 10 个文件，**零命中** |
| 密钥模式扫描 | JWT / 企微 webhook / Bark key / 私钥块 / AWS AK / GitHub PAT / Slack / Google Key —— 全部无命中 |
| 危险代码模式 | 6 类高危模式全部无命中 |
| 敏感文件名 | 无 `notify_config.json` 等文件 |
| workflow 权限 | `permissions: contents: read`（仅读，无写权限） |
| 触发面 | 仅 `schedule` + `workflow_dispatch`，**无 `pull_request_target`**（避免 PR 投毒） |
| 超时 | `timeout-minutes: 10` |
| 第三方依赖 | 零依赖，仅 Python 标准库 |

### 2.2 已修复的问题（4 项）

#### 问题 1 · 敏感信息暴露：打印 token 长度（中等）
- **位置**：`.github/workflows/checkin.yml` 「检查凭据是否已配置」步骤
- **原代码**：`echo "凭据已配置，token 长度 ${#WORKBUDDY_ACCESS_TOKEN}，域名 ..."`
- **风险**：token 长度是稳定的指纹信息，结合 JWT 结构可缩小攻击者搜索空间，属可避免的信息暴露。
- **修复**：改为只输出存在性结论 `✓ 凭据已配置（token 存在，内容不外显）`，不输出长度。

#### 问题 2 · 日志长期留存泄露风险：完整响应进摘要（较高）
- **位置**：`.github/workflows/checkin.yml` 「输出运行摘要」步骤
- **原代码**：`cat result.json` 全文写入 `$GITHUB_STEP_SUMMARY`
- **风险**：Actions 运行摘要**长期留存**在仓库页面，任何能访问仓库的人都能看到。`result.json` 含 `detail.status_resp`（原始接口响应全文）、`auth_file`（runner 路径）、`token_masked`。虽然当前响应仅含签到数据，但**接口未来变更可能引入非预期字段**，属不可控风险。
- **修复**：新增 `scripts/summarize_result.py`，采用**白名单过滤**，只输出 `status/action/points/balance/msg/domain/streak_days` 等结论字段。
- **实测效果**：原始 1585 字符 → 摘要 131 字符（**缩减 92%**），token / 路径 / requestId / 原始响应**全部被过滤**。

#### 问题 3 · 逻辑缺陷导致静默失败：管道吞掉退出码（较高）
- **位置**：`.github/workflows/checkin.yml` 「执行签到」步骤
- **原代码**：`python scripts/workbuddy_checkin.py $ARGS | tee result.json`
- **风险**：**这是本次审计发现的最严重问题**。管道会吞掉 Python 的退出码，`$?` 变成 `tee` 的退出码（通常为 0）。导致**即使签到失败，该步骤仍判为 success**，进而使「失败时终止任务」不触发 —— 你会收不到任何失败告警，签到悄悄坏掉。
- **修复**：用 `PIPESTATUS[0]` 精确取 Python 退出码，并显式 `exit $rc`：
  ```bash
  set +e
  python scripts/workbuddy_checkin.py $ARGS | tee result.json
  rc=${PIPESTATUS[0]}
  set -e
  echo "checkin_exit_code=$rc" >> "$GITHUB_OUTPUT"
  exit $rc
  ```

#### 问题 4 · 部署阻塞：gh 未安装导致流程中断（中等）
- **位置**：`scripts/sync_token_to_github.py`
- **实际发现**：本机**未安装 `gh`**（GitHub CLI），且 `git` 也不在 PATH 中（但存在于 `~/.workbuddy/binaries/PortableGit/versions/1.2.0/cmd/git.exe`）。
- **风险**：原代码在 gh 缺失时直接 `exit 1` 中断，用户会卡在部署第二步无法继续。
- **修复**：
  1. 新增 `find_git()`，主动探测 PortableGit 路径；
  2. 增强 `gh_available()`，除 PATH 外还探测常见安装位置；
  3. gh 缺失时**降级为手动配置指引**（输出 Secret 名称、值来源、仓库设置页直链），不再中断；
  4. 新增 `scripts/copy_token_to_clipboard.py`：把 token 复制到剪贴板（**不打印到屏幕**），解决 1300+ 字符 token 手动框选极易出错的问题；
  5. dry-run 与正式运行均**不再打印 token 长度**。

#### 附带修复：审计脚本自身误报
- `security_audit.py` 的检测规则字面量（如 `re.compile(r"shell\s*=\s*True")`）会被自身命中，产生 3 条假告警。已加入 `SKIP_FILES` 跳过自身，现在输出干净。

---

## 三、多层防护设计（防泄露）

| 层级 | 机制 | 防护目标 |
|---|---|---|
| ① 存储层 | GitHub Secrets 加密存储 | 凭据不以明文存在于仓库 |
| ② 传输层 | `gh secret set` 经 **stdin** 传值 | 密钥不出现在进程命令行（`ps` 可见） |
| ③ 脚本层 | 输出仅含 `token_masked`（脱敏） | 日志不含真实 token |
| ④ 摘要层 | `summarize_result.py` 白名单过滤 | 阻止原始响应进入长期留存的摘要 |
| ⑤ 兜底层 | 长度 ≥80 的 base64url 长串替换为 `<REDACTED>` | 防接口变更引入非预期敏感字段 |
| ⑥ 交付层 | `security_audit.py` 部署前自检 | 防人为失误把凭据提交进仓库 |
| ⑦ 仓库层 | `.gitignore` 覆盖密钥文件与运行产物 | 防误 `git add` 敏感文件 |

---

## 四、实测验证记录

| 验证项 | 方法 | 结果 |
|---|---|---|
| 全文件 token 比对 | 本机真实 token 逐文件全文/片段匹配 | ✅ 零命中 |
| 摘要过滤有效性 | 7 项敏感字段检查（完整 token / 前 50 字符 / masked 字段名 / 路径 / 原始响应 / requestId / `eyJhb` 前缀） | ✅ 7/7 全部安全 |
| 环境变量模式签到 | 模拟 CI 注入 token 运行 | ✅ 退出码 0，`status=ok`，`auth_source=env` |
| 摘要泄漏检查 | 断言 token 不出现在摘要输出 | ✅ 无泄漏 |
| 退出码传播逻辑 | 审查 + PIPESTATUS 修复 | ✅ 已修正 |
| gh 降级路径 | 在本机（无 gh）实跑 | ✅ 正常输出手动指引，不中断 |
| 剪贴板脚本 | `--domain` 实跑 | ✅ 成功复制 |
| 全部脚本语法 | `py_compile` | ✅ 6/6 通过 |
| 运行残留清理 | 删除 `__pycache__` / `result.json` / `checkin.log` | ✅ 已清理 |

---

## 五、部署前必做清单

- [ ] **仓库设为 private**（public 仓库的 Actions 日志与摘要默认公开可见）
- [ ] 运行 `python security_audit.py` 确认输出「泄露风险 无」
- [ ] `git status` 确认无 `notify_config.json` 等敏感文件被 add
- [ ] 配置至少一个失败通知通道（企微 / PushPlus / Bark），否则失败仅靠 GitHub 邮件
- [ ] 首次部署用 `check_only` 手动跑一次，确认 `status: ok`

---

## 六、残余风险与说明

| 风险 | 说明 | 缓解 |
|---|---|---|
| token 有效期约 2 个月 | 属 WorkBuddy 机制，非本项目缺陷 | 过期后重跑 `sync_token_to_github.py` 续期 |
| public 仓库日志公开 | GitHub 平台特性 | 使用 private 仓库；摘要已白名单过滤 |
| 定时任务延迟 | GitHub 平台特性 | 可接受；或与本机自动化互为备份 |
| 仓库 60 天无活动暂停 | GitHub 平台特性 | 定期查看，或手动触发保活 |
| token 明文存于本机登录态文件 | WorkBuddy 客户端机制 | 脚本只读、不外传；确保本机账号安全 |

> **本项目自身未引入新的残余风险**。以上均为平台或产品固有约束，已在 README 中明确告知。

---

## 七、审计工具

```bash
# 部署前自检（退出码 0 = 无风险，1 = 有风险，可用于 CI 卡点）
python security_audit.py
```

建议在推送前、以及后续每次修改后运行一次。
