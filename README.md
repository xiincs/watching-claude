简单总结：**这是一个“Claude Code 自动任务监工 / 自动续跑脚本”**。

它的核心目的不是自己完成开发任务，而是：

> **让 Claude Code 持续执行一个任务 → 观察执行结果 → 判断是否完成 → 如果没完成，让 DeepSeek 生成下一步指令 → 继续让 Claude Code 执行。**

整体可以理解成一个 **Claude Code 外部 Supervisor（监工）**。

### 它的工作流程

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
实时读取 Claude 输出
    │
    ├── Claude 执行成功 ──────┐
    │                        │
    ├── Claude 执行失败       │
    │      │                 │
    │      ▼                 │
    │   等待 + 重试            │
    │                        │
    ▼                        │
收集 Claude 本轮结果          │
+ Git 最近提交记录            │
+ 原始任务目标                │
    │                        │
    ▼                        │
调用 DeepSeek                 │
    │
    ├── done = true
    │       ↓
    │     停止
    │
    └── done = false
            ↓
       获取 next_prompt
            │
            ▼
      继续 Claude Code
            │
            └───────────────↺
```

### 主要有 6 个职责

1. 读取任务

   - 从目标项目下 `.claude/watching/` 里获取任务目标；具体用哪个文件由
     运行时解析决定（详见下文「运行方式」与「任务文件自动发现」）。

2. 启动 Claude Code

   - 在目标项目目录中运行 Claude（默认取当前工作目录，可用 `--project` 指定）。
   - 支持 `--resume`，因此可以继续之前的 Claude session。
   - 使用 `--dangerously-skip-permissions`，让 Claude 不需要人工逐项确认权限。

3. 实时监控 Claude

   - 使用 `stream-json` + `include-partial-messages`。
   - 不再等 Claude 全部结束后才看到输出，而是实时打印 Claude 的执行事件、工具调用、文本等。
   - 通过独立 reader thread + queue 避免 `readline()` 阻塞导致 watchdog 失效。

4. 处理 Claude 异常

   - Claude 返回非 0 → 认为本轮失败。

   - 根据失败次数进行指数退避，例如：

     ```
     10s
     20s
     40s
     80s
     ...
     ```

   - session 无效时，可以清掉旧 session，重新启动。

5. 让 DeepSeek 当“任务验收员”

   - 把 Claude 最近输出、原始任务、Git 提交记录等交给 DeepSeek。

   - DeepSeek 返回类似：

     ```
     {
       "done": false,
       "confidence": 0.85,
       "reason": "核心功能已经完成，但测试尚未通过",
       "next_prompt": "继续运行测试并修复失败项..."
     }
     ```

   - `done=true` 才真正结束整个 watcher。

6. 循环执行直到任务完成

   - Claude → DeepSeek 判断 → 下一步 Prompt → Claude
   - 所以它本质上是在实现一个简单的 **Agent Loop / Supervisor Loop**。

### 一句话定位

如果给这个脚本起一个准确的名字，我会叫：

> **Claude Code 自动续跑 + DeepSeek 验收的任务监工（Supervisor）**

它解决的主要问题是：**Claude Code 一轮执行结束后，不需要你人工判断“做完了吗、下一步是什么”，脚本自动判断并继续推进。**

---

## 无人值守可靠性机制

设计前提：**无限循环是需求**。因为 Claude 侧的网络不稳定，所以循环不设轮次上限。
在这个前提下，风险不是“跑太多轮”，而是另外两件事：

1. **循环不收敛** —— 看起来一直在跑，实际零进展；
2. **监工自己死掉** —— 无人值守时进程退出且无人知晓。

下面这些机制都是围绕这两点，且都不削弱无限循环。

### 机制一览

| 机制 | 位置 / 配置 | 行为 |
| --- | --- | --- |
| stdout 强制 UTF-8 | `configure_stdout()` | 重定向输出时 Windows 会用 gbk，打印 emoji 会抛 `UnicodeEncodeError`，进而强杀本轮 Claude。现在强制 UTF-8 + `errors="replace"`，`log()`/`log_raw()` 的 print 另有异常保护 |
| 单轮异常隔离 | `main()` 的 try/except + `run_round()` | 任何未预期异常只影响一轮：记录类型与 traceback、退避、继续，绝不 `sys.exit` |
| session 自愈 | `--max-consecutive-failures`（默认 8） | 网络中断/被强杀时 Claude 来不及输出 `result` 事件，光靠文本匹配清不掉坏 session。现在用连续失败计数兜底：达到阈值就丢弃 session、退回原始任务重开 |
| 重试语义保护 | `RETRY_GUARD_PROMPT` | 失败重试、或恢复上次中断留下的 session 时，自动附加“先检查 git status / tag / Release，禁止重复 bump 版本、重复打 tag、重复发版”的前缀 |
| 原地打转检测 | `--stuck-rounds`（默认 3） + `STUCK_ESCALATION_PROMPT` | 连续 N 轮 git 指纹（HEAD + 工作区 + 提交时间）完全不变，判定为停滞，下一轮附加自诊断指令。**只升级 prompt，不停止循环** |
| 停止不误报 | `ROUND_STOPPED` | Ctrl+C 会让进程退出码为 1，以前会被记成 `PROCESS_FAILURE` 并打印退避，误导事后复盘；现在识别为“被打断”而非失败 |

### 退出码语义

| 退出码 | 含义 |
| --- | --- |
| `0` | DeepSeek 判定 `done=true` 且 `confidence >= min-confidence`（默认 0.90），任务确认完成；或 `--list-tasks` 正常列出 |
| `1` | 配置无法解析（项目目录不存在、找不到任务文件），或 `claude` CLI 不在 PATH |
| `2` | Claude 认证失败（需要先 `/login`） |
| `130` | 收到停止请求（Ctrl+C / SIGTERM） |

### 运行方式

目标项目与任务文件都不再写在代码里，运行时解析。优先级：
**命令行 > 环境变量 > 默认值**。

```bash
# 最简：cd 到目标项目，任务文件自动发现
cd E:\Project202608\dsh-desktop
python E:\Projects202609\watching-claude\claude_watcher_refactored.py

# 指定目标项目
python claude_watcher_refactored.py --project E:\Project202608\dsh-desktop

# 指定任务文件（完整路径 / 文件名 / 唯一主干名 都可以）
python claude_watcher_refactored.py -t task_prompt_20260911_01.md
python claude_watcher_refactored.py -t task_prompt_20260911_01

# 看看有哪些任务文件可选（不启动 watcher）
python claude_watcher_refactored.py --list-tasks

# 全量参数
python claude_watcher_refactored.py --help
```

### 任务文件自动发现

`<project>/.claude/watching/` 下的选择规则：

1. 若存在 `task_prompt.md`（不带日期）→ **优先使用它**，用于把当前任务钉住；
2. 否则取 `task_prompt*.md` 中**字典序最大**的一个。

文件名形如 `task_prompt_20260911_01.md`，字典序即时间序，
所以**换任务只需要新建一个任务文件，不必改任何代码**。

### 环境变量

| 变量 | 等价参数 | 说明 |
| --- | --- | --- |
| `WATCHER_PROJECT_DIR` | `--project` | 目标项目目录 |
| `WATCHER_TASK_PROMPT` | `--task` | 任务 prompt 文件 |
| `WATCHER_MODEL` | `--model` | DeepSeek 模型名 |
| `DEEPSEEK_API_KEY` | — | DeepSeek 鉴权，必填 |

### 可调参数

全部集中在 `Config` dataclass 里，路径由 `project_dir` 派生，
状态文件（`last_session_id.txt` / `stream_logs/` / `watcher.log`）位置
只在 `Config` 的属性里定义一次。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--claude-command` | `claude` | Claude CLI 命令名或路径 |
| `--max-turns` | 50 | 单轮最大 turns |
| `--min-confidence` | 0.90 | 判定完成的置信度门槛 |
| `--idle-kill-seconds` | 600 | 多少秒收不到事件就强杀 Claude（网络卡死的解套手段） |
| `--base-delay` / `--max-delay` | 10 / 300 | 失败退避的起点与上限 |
| `--max-consecutive-failures` | 8 | 连续失败多少次后丢弃 session |
| `--stuck-rounds` | 3 | 连续多少轮无 git 变化判定为停滞 |

### 运行前提

- 环境变量 `DEEPSEEK_API_KEY`（注意：设在 User 作用域后需要**新开终端**才能读到）
- `claude` CLI 在 PATH 中（或用 `--claude-command` 指定）
- 目标项目下存在任务 prompt 文件（可用 `--list-tasks` 确认）

### 维护提醒

`ask_deepseek()` 使用 `response_format={"type": "json_object"}`，而 DeepSeek 要求
prompt 中必须出现 “json” 字样，否则直接 400。当前 `DEEPSEEK_SYSTEM_PROMPT` 结尾的
“只输出 JSON：”正好满足该要求——**改写 system prompt 时不要删掉这句**。
