# watching-claude

**Claude Code 外部监工：让 Claude Code 持续干活，用 DeepSeek 当验收员判断是否收工。**

它自己不做开发任务，而是驱动 Claude Code 反复执行，并在每一轮之后判断
「做完了吗、下一步做什么」，直到任务真正完成。

> 让 Claude Code 执行任务 → 观察执行结果 → 判断是否完成 →
> 未完成则让 DeepSeek 生成下一步指令 → 继续执行 → 循环

本质上是一个 **Claude Code 外部 Supervisor（监工）**，解决的是
「一轮跑完之后，不需要人盯着判断做没做完、下一步干什么」。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [安全提示](#安全提示)
- [工作流程](#工作流程)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [任务 prompt 怎么写](#任务-prompt-怎么写)
- [命令行参数](#命令行参数)
- [任务文件自动发现](#任务文件自动发现)
- [环境变量](#环境变量)
- [可靠性机制](#可靠性机制)
- [退出码](#退出码)
- [目录结构](#目录结构)
- [测试](#测试)
- [设计取舍与已知限制](#设计取舍与已知限制)

---

## 它解决什么问题

Claude Code 的 `-p`（一次性执行）模式跑完一轮就结束。如果任务较大，你需要
反复人工判断「它做完了吗」，再手动决定下一轮的指令——这正是本脚本要替代的部分。

具体来说它做到：

1. **读取任务** —— 从被监督项目的 `.claude/watching/` 取任务目标
2. **启动 Claude Code** —— 以 `--resume` 续跑同一个 session，保留上下文
3. **实时监控** —— 流式解析 `stream-json` 事件，实时打印工具调用与模型输出
4. **处理异常** —— 指数退避重试、session 自愈、停止请求识别
5. **让 DeepSeek 当验收员** —— 基于执行事实（工具调用、工具结果、git 状态）
   判断任务是否真的完成，未完成则给出下一步指令
6. **循环到完成为止** —— 因 Claude 侧网络不稳定，循环**不设轮次上限**

## 安全提示

> ⚠️ **请先读完这一节再使用。**

- 本脚本用 `--dangerously-skip-permissions` 启动 Claude Code，
  即 **Claude 不会在动手前向你逐项确认权限**。它会自主读写文件、执行命令、
  `git push`、发布 release。请在你有把握的项目里、最好在可丢弃的分支或
  容器/虚拟机中使用。
- **循环不设轮次上限**，且无人值守时会一直跑下去。在无人看管的情况下运行，
  意味着它可能持续消耗 Claude 与 DeepSeek 的额度。
- 脚本会把任务原文、Claude 的工具调用与工具结果、`git status` / `git log`
  一起发送给 DeepSeek API。**不要把含密钥、隐私或商业机密的任务放进任务文件。**
- 建议先在一件小事上跑通全流程，再用于正式任务。

## 工作流程

```
任务 Prompt
    │
    ▼
启动 Claude Code
    │
    │  --dangerously-skip-permissions
    │  --output-format stream-json
    │  --include-partial-messages
    ▼
实时读取 Claude 输出（独立 reader 线程 + queue，避免 readline 阻塞 watchdog）
    │
    ├── Claude 执行成功 ──────┐
    │                        │
    ├── Claude 执行失败       │
    │      │                 │
    │      ▼                 │
    │   指数退避 + 重试        │
    │                        │
    ▼                        │
收集本轮结果                  │
+ Git 最近提交记录             │
+ 原始任务目标                 │
    │                        │
    ▼                        │
调用 DeepSeek                │
    │                        │
    ├── done = true 且置信度达标
    │       ↓
    │     停止
    │
    └── 否则
            ↓
       获取 next_prompt
            │
            ▼
      继续 Claude Code
            │
            └───────────────↺
```

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python | 3.9 或更高（依赖 `openai` 2.x 自身的下限也是 3.9） |
| [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) | 需已安装并完成登录（`claude` 在 PATH 中） |
| DeepSeek API Key | 用于验收判断，[申请地址](https://platform.deepseek.com/) |

## 安装

```bash
git clone https://github.com/<your-name>/watching-claude.git
cd watching-claude
pip install -r requirements.txt
```

设置 API Key：

```powershell
# Windows PowerShell（持久化到用户环境变量；设置后需要新开终端）
setx DEEPSEEK_API_KEY "sk-xxxxxxxx"
```

```bash
# macOS / Linux
export DEEPSEEK_API_KEY="sk-xxxxxxxx"
```

## 快速开始

```bash
# 1. 在被监督的项目里创建任务文件
mkdir -p /path/to/project/.claude/watching
# 把任务目标写进 /path/to/project/.claude/watching/task_prompt.md

# 2. 先确认任务文件能被发现（不会启动 watcher）
python claude_watcher.py --list-tasks --project /path/to/project

# 3. 启动监工
python claude_watcher.py --project /path/to/project

# 或者 cd 到目标项目后直接运行，无需任何参数
cd /path/to/project
python /path/to/watching-claude/claude_watcher.py
```

运行期间的所有产出都在被监督项目的 `.claude/watching/` 下：

| 文件 | 内容 |
| --- | --- |
| `watcher.log` | 监工日志（每轮开始/结束、失败分类、DeepSeek 结论） |
| `stream_logs/claude_*.jsonl` | Claude 的原始 stream-json 事件流 |
| `last_session_id.txt` | 当前 Claude session，用于 `--resume` |

按 `Ctrl+C` 可随时停止；已产生的 session 会被保留，下次运行自动续跑。

## 任务 prompt 怎么写

任务文件就是一段自然语言。因为 DeepSeek 要靠「执行事实」判断是否完成，
任务描述里**给出可验证的完成标准**会显著提高判定质量：

```markdown
把项目里的日志输出统一改造为结构化日志（JSON 格式），要求：

1. 替换所有 print 调试输出，改用 logging
2. 输出格式为 JSON，字段包含 timestamp / level / message
3. 更新 README 中的日志说明
4. 运行现有测试并确保全部通过
5. 提交到当前分支并推送

完成后说明改了哪些文件、测试结果如何。
```

反例：`优化一下代码` —— 没有可验证的完成标准，DeepSeek 只能保守地
一直判定未完成，循环会一直跑下去。

## 命令行参数

```
python claude_watcher.py --help
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-p`, `--project` | 当前工作目录 | 被监督的项目目录 |
| `-t`, `--task` | 自动发现 | 任务 prompt 文件：完整路径 / watching 内文件名 / 唯一主干名 |
| `--list-tasks` | — | 列出可发现的任务文件后退出，不启动 watcher |
| `--claude-command` | `claude` | Claude CLI 命令名或路径 |
| `--max-turns` | 50 | 单轮 Claude 的最大 turns |
| `--model` | `deepseek-flash` | DeepSeek 模型名 |
| `--min-confidence` | 0.90 | 判定任务完成的置信度门槛 |
| `--idle-kill-seconds` | 600 | 多少秒收不到事件就强杀 Claude |
| `--base-delay` / `--max-delay` | 10 / 300 | 失败退避的起点与上限（秒） |
| `--max-consecutive-failures` | 8 | 同一 session 连续失败多少次后丢弃重开 |
| `--stuck-rounds` | 3 | 连续多少轮 git 状态无变化判定为停滞 |

## 任务文件自动发现

被监督项目的 `.claude/watching/` 下的选择规则：

1. 若存在 `task_prompt.md`（不带日期）→ **优先使用它**，用于把当前任务钉住
2. 否则取 `task_prompt*.md` 中**字典序最大**的一个

文件名形如 `task_prompt_20260911_01.md`，**字典序即时间序**。
因此换任务只需要新建一个任务文件，不必改任何代码。

## 环境变量

配置优先级：**命令行 > 环境变量 > 默认值**

| 变量 | 等价参数 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | DeepSeek 鉴权，**必填** |
| `WATCHER_PROJECT_DIR` | `--project` | 目标项目目录 |
| `WATCHER_TASK_PROMPT` | `--task` | 任务 prompt 文件 |
| `WATCHER_MODEL` | `--model` | DeepSeek 模型名 |

## 可靠性机制

设计前提：**无限循环是需求**（Claude 侧网络不稳定）。在这个前提下，风险不是
「跑太多轮」，而是另外两件事：

1. **循环不收敛** —— 看起来一直在跑，实际零进展
2. **监工自己死掉** —— 无人值守时进程退出且无人知晓

以下机制都围绕这两点，且都不削弱无限循环。

| 机制 | 触发条件 | 行为 |
| --- | --- | --- |
| 单轮异常隔离 | 任意未预期异常 | 只影响一轮：记录类型 + traceback、退避、继续，绝不 `sys.exit` |
| 输出编码健壮 | stdout 被重定向到文件（Windows 下为 gbk） | 强制 UTF-8 且编码失败降级为替换字符，并在 `print` 外再包一层保护。否则 Claude 输出里的 emoji 会抛 `UnicodeEncodeError`，导致本轮 Claude 被强杀 |
| session 自愈 | 同一 session 连续失败 8 次 | 丢弃 session、退回原始任务重开。网络中断/被强杀时 Claude 来不及输出 `result` 事件，光靠文本匹配清不掉坏 session，必须靠这个计数兜底 |
| 重试语义保护 | 失败重试 / 恢复上次中断留下的 session | 自动附加前缀：先检查 `git status`、`git log`、`git tag`、已有 Release，**禁止重复提升版本号、重复打 tag、重复发版** |
| 原地打转检测 | 连续 3 轮 git 指纹（HEAD + 工作区 + 提交时间）完全不变 | 下一轮附加自诊断指令，要求列出完成度、给出卡住原因与证据。**只升级 prompt，不停止循环** |
| 停止不误报 | 收到 Ctrl+C / SIGTERM | 识别为「被打断」而非执行失败，不再留下假的 `PROCESS_FAILURE` 与退避日志，避免事后复盘误判 |
| 无事件 watchdog | 600 秒收不到任何事件 | 强杀 Claude 进程并按失败重试——这是网络卡死时的主要解套手段 |

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | DeepSeek 判定 `done=true` 且置信度达标，任务确认完成；或 `--list-tasks` 正常列出 |
| `1` | 配置无法解析（项目目录不存在、找不到任务文件），或 `claude` CLI 不在 PATH |
| `2` | Claude 认证失败（需要先执行 `/login`） |
| `130` | 收到停止请求（Ctrl+C / SIGTERM） |

## 目录结构

```
watching-claude/
├── claude_watcher.py       # 全部实现，单文件，无包结构
├── requirements.txt        # 运行时依赖（仅 openai）
├── tests/
│   ├── harness.py          # 测试夹具
│   ├── run_all.py          # 运行器
│   └── case_*.py           # 7 个用例
├── .github/workflows/      # CI
└── README.md
```

## 测试

```bash
python tests/run_all.py        # 汇总
python tests/run_all.py -v     # 显示每个用例完整输出
python tests/run_all.py case_stop   # 只跑一个用例
```

测试完全离线：夹具把配置解析以外的两个外部边界替换掉——
`run_claude_streaming`（用脚本化的轮次剧本）和 `ask_deepseek`（用脚本化的
验收结论）。**不需要网络、不需要安装 claude CLI、不需要 `DEEPSEEK_API_KEY`。**

| 用例 | 覆盖 |
| --- | --- |
| `case_config` | 配置解析：自动发现、`task_prompt.md` 钉住、`--task` 三种写法、三级优先级、错误提示、`--list-tasks` |
| `case_encoding` | emoji 不再击穿 watcher（stdout 重定向到文件的 gbk 场景） |
| `case_isolation` | 单轮未预期异常不得终止 watcher |
| `case_session_heal` | 连续失败达到阈值必须丢弃 session 并回到原始任务 |
| `case_retry_guard` | 失败重试 / 恢复中断会话时附加保护前缀，且不逐轮累积 |
| `case_stuck` | 原地打转检测：只升级 prompt，不停止循环 |
| `case_stop` | Ctrl+C 停止不得留下假的 `PROCESS_FAILURE` 与退避日志 |

用例的设计方式：每个用例都会在**修复前的版本**上复跑一遍，确认它会 FAIL。
把修复前的脚本取出后用 `CW_MODULE` 指过去即可：

```bash
git show <旧提交>:claude_watcher.py > /tmp/old.py
CW_MODULE=/tmp/old.py python tests/case_stop.py     # 应当 FAILED
```

## 设计取舍与已知限制

- **不设轮次上限是刻意的**。因为主要故障形态是 Claude 侧网络不稳定，
  设上限等于把「跑不完」变成必然。代价是可能在无人看管时持续消耗额度，
  请自行评估。
- **验收判据偏保守**。`done=true` 需要置信度 ≥ 0.90，宁可多跑几轮也不误判完成。
  代价是任务实际已完成时可能继续空转——这正是「原地打转检测」要缓解的问题。
- **停滞检测依赖 git**。目标目录不是 git 仓库时无法取指纹，此时该机制
  自动放弃（宁可漏判也不误判）。
- **DeepSeek 的 `json_object` 模式要求 prompt 中出现 “json” 字样**，
  否则 API 直接返回 400。当前靠 `DEEPSEEK_SYSTEM_PROMPT` 结尾的
  “只输出 JSON：”满足该要求——**改写该 system prompt 时不要删掉这句**。
- 脚本会以 `--dangerously-skip-permissions` 运行 Claude，见上文安全提示。

## License

尚未添加许可证文件。若你打算公开分发，请先补上 `LICENSE`
（MIT / Apache-2.0 等），否则默认保留所有权利。
