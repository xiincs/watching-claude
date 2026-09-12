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

   - 从 `.claude/watching/task_prompt_20260911_01.md` 获取任务目标。

2. 启动 Claude Code

   - 在项目目录 `E:\Project202608\dsh-desktop` 中运行 Claude。
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