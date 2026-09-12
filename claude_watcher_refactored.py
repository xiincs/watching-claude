import json
import os
import queue
import random
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI


# ============================================================
# 配置
# ============================================================

PROJECT_DIR = Path(r"E:\Project202608\dsh-desktop")

TASK_PROMPT_FILE = (
    PROJECT_DIR
    / ".claude"
    / "watching"
    / "task_prompt_20260911_01.md"
)

WATCHING_DIR = PROJECT_DIR / ".claude" / "watching"
SESSION_FILE = WATCHING_DIR / "last_session_id.txt"
STREAM_LOG_DIR = WATCHING_DIR / "stream_logs"
WATCHER_LOG_FILE = WATCHING_DIR / "watcher.log"

# Claude CLI
CLAUDE_COMMAND = "claude"
CLAUDE_MAX_TURNS = 50

# Claude 无事件 watchdog
IDLE_WARNING_SECONDS = 180
IDLE_KILL_SECONDS = 600

# 失败重试
BASE_DELAY = 10
MAX_DELAY = 300
BACKOFF_FACTOR = 2
JITTER = 0.3

# 同一个 session 连续失败达到该次数，即判定 session 不可用并丢弃重开。
# 网络中断 / watchdog 强杀时 Claude 可能来不及输出 result 事件，
# 此时 session_invalid 永远不会被置位，必须靠这个计数兜底恢复。
MAX_CONSECUTIVE_FAILURES = 5

# DeepSeek
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_TIMEOUT = 120
DEEPSEEK_MIN_CONFIDENCE = 0.90


# ============================================================
# 全局状态
# ============================================================

STOP_REQUESTED = False


# ============================================================
# stdout 编码
# ============================================================

def configure_stdout() -> None:
    """把 stdout / stderr 强制为 UTF-8，并把编码失败降级为替换字符。

    无人值守运行时 stdout 通常被重定向到文件或管道。Windows 下此时
    Python 使用 ANSI 代码页（gbk/cp936）且 errors=surrogateescape，
    打印 Claude 输出、工具结果里常见的 emoji（✅ ✔ ⚠）会抛
    UnicodeEncodeError。

    该异常会从事件处理中冒出来，导致本轮 Claude 进程被判定失败并强杀。
    如果 Claude 每轮都在同一位置输出 emoji，watcher 就会永远卡在
    “启动 → 被杀 → 重试”的循环里，永不收敛。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            pass


configure_stdout()


# ============================================================
# 日志
# ============================================================

def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    # 打印失败（编码、管道断开、句柄被回收）绝不能影响主流程。
    # 文件日志仍会写入完整内容。
    try:
        print(line, flush=True)
    except Exception:
        pass

    try:
        WATCHING_DIR.mkdir(parents=True, exist_ok=True)
        with WATCHER_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_raw(message: str) -> None:
    try:
        print(message, end="", flush=True)
    except Exception:
        pass


# ============================================================
# Ctrl+C / signal
# ============================================================

def handle_signal(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log("收到停止信号，准备退出...")


try:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
except Exception:
    pass


# ============================================================
# 文件工具
# ============================================================

def ensure_directories() -> None:
    WATCHING_DIR.mkdir(parents=True, exist_ok=True)
    STREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_task_prompt() -> str:
    if not TASK_PROMPT_FILE.exists():
        raise FileNotFoundError(
            f"任务 prompt 不存在:\n{TASK_PROMPT_FILE}"
        )
    return TASK_PROMPT_FILE.read_text(encoding="utf-8")


def load_session_id() -> Optional[str]:
    if not SESSION_FILE.exists():
        return None

    try:
        sid = SESSION_FILE.read_text(encoding="utf-8").strip()
        return sid or None
    except Exception as e:
        log(f"[Session] 读取失败: {e}")
        return None


def save_session_id(session_id: Optional[str]) -> None:
    if not session_id:
        return

    try:
        WATCHING_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(session_id, encoding="utf-8")
    except Exception as e:
        log(f"[Session] 保存失败: {e}")


def clear_session_id() -> None:
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        log("[Session] 已清除旧 session_id")
    except Exception as e:
        log(f"[Session] 清除失败: {e}")


# ============================================================
# Claude Execution
# ============================================================

class ClaudeExecution:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.returncode: Optional[int] = None

        self.result: Optional[str] = None
        self.result_event: Optional[dict[str, Any]] = None

        self.stop_reason: Optional[str] = None
        self.subtype: Optional[str] = None
        self.num_turns: Optional[int] = None
        self.is_error = False

        self.api_error_status: Optional[Any] = None
        self.terminal_reason: Optional[str] = None
        self.total_cost_usd: Optional[float] = None

        self.tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.assistant_text: list[str] = []
        self.thinking_seen = False

        self.errors: list[str] = []

        self.events_count = 0
        self.last_event_time = time.time()
        self.last_event_description = ""

        self.auth_failed = False
        self.session_invalid = False

        self.idle_warning_sent = False

    def process_event(self, event: dict[str, Any]) -> None:
        self.events_count += 1
        self.last_event_time = time.time()

        event_type = event.get("type")
        self.last_event_description = str(event_type or "unknown")

        sid = event.get("session_id")
        if sid:
            self.session_id = sid

        if event_type == "system":
            self.process_system_event(event)
            return

        if event_type == "stream_event":
            inner = event.get("event", {})
            if isinstance(inner, dict):
                self.process_stream_event(inner)
            return

        if event_type == "assistant":
            self.process_assistant_message(event)
            return

        if event_type == "user":
            self.process_user_message(event)
            return

        if event_type == "result":
            self.process_result(event)
            return

        if event_type == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            status = info.get("status")
            log(f"[Claude] rate_limit={status}")
            return

    def process_system_event(self, event: dict[str, Any]) -> None:
        subtype = event.get("subtype")

        if subtype:
            self.last_event_description = f"system/{subtype}"

        if subtype == "init":
            tools = event.get("tools", [])
            mcp_servers = event.get("mcp_servers", [])
            model = event.get("model")

            if self.session_id:
                log(f"[Claude] session={self.session_id}")
                save_session_id(self.session_id)

            log(
                f"[Claude] model={model}, "
                f"tools={len(tools)}, "
                f"MCP={len(mcp_servers)}"
            )

            for mcp in mcp_servers:
                log(
                    f"[Claude] MCP {mcp.get('name')} "
                    f"status={mcp.get('status')}"
                )

        elif subtype == "status":
            log(f"[Claude] status={event.get('status')}")

    def process_stream_event(self, inner: dict[str, Any]) -> None:
        event_type = inner.get("type")

        if event_type == "message_start":
            message = inner.get("message", {})
            model = message.get("model")
            if model:
                log(f"[Claude] 模型开始响应: {model}")
            return

        if event_type == "content_block_start":
            block = inner.get("content_block", {})
            block_type = block.get("type")

            if block_type == "thinking":
                self.thinking_seen = True
                log("[Claude] thinking...")

            elif block_type == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_id = block.get("id")

                self.tool_calls.append(
                    {
                        "id": tool_id,
                        "name": tool_name,
                        "input": {},
                    }
                )

                log(f"[Claude] 调用工具: {tool_name}")

            return

        if event_type == "content_block_delta":
            delta = inner.get("delta", {})
            delta_type = delta.get("type")

            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    self.assistant_text.append(text)
                    log_raw(text)

            elif delta_type == "thinking_delta":
                self.thinking_seen = True

            return

        if event_type == "message_delta":
            delta = inner.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                self.stop_reason = stop_reason
            return

        if event_type == "message_stop":
            log("")

    def process_assistant_message(self, event: dict[str, Any]) -> None:
        message = event.get("message", {})
        content = message.get("content", [])

        for block in content:
            block_type = block.get("type")

            if block_type == "text":
                # stream_event 已经实时收集过文本。
                # 这里仅在没有实时文本时补充，避免重复。
                text = block.get("text", "")
                if text and not self.assistant_text:
                    self.assistant_text.append(text)

            elif block_type == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_id = block.get("id")
                tool_input = block.get("input", {})

                found = False
                for call in self.tool_calls:
                    if call.get("id") == tool_id:
                        call["input"] = tool_input
                        found = True
                        break

                if not found:
                    self.tool_calls.append(
                        {
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input,
                        }
                    )

            elif block_type == "thinking":
                self.thinking_seen = True

    def process_user_message(self, event: dict[str, Any]) -> None:
        message = event.get("message", {})
        content = message.get("content", [])

        for block in content:
            if block.get("type") != "tool_result":
                continue

            tool_use_id = block.get("tool_use_id")
            content_value = block.get("content", "")
            is_error = block.get("is_error", False)

            self.tool_results.append(
                {
                    "tool_use_id": tool_use_id,
                    "content": content_value,
                    "is_error": is_error,
                }
            )

            preview = str(content_value).strip()
            if len(preview) > 500:
                preview = preview[:500] + "..."

            if is_error:
                log(f"[Tool ERROR] {tool_use_id}: {preview}")
            else:
                log(f"[Tool RESULT] {preview}")

    def process_result(self, event: dict[str, Any]) -> None:
        self.result_event = event

        self.result = event.get("result")
        self.subtype = event.get("subtype")
        self.stop_reason = event.get("stop_reason", self.stop_reason)
        self.num_turns = event.get("num_turns")

        self.is_error = bool(event.get("is_error", False))
        self.api_error_status = event.get("api_error_status")
        self.terminal_reason = event.get("terminal_reason")
        self.total_cost_usd = event.get("total_cost_usd")

        result_lower = str(self.result or "").lower()

        if (
            "not logged in" in result_lower
            or "authentication" in result_lower
            or "please run /login" in result_lower
        ):
            self.auth_failed = True

        if any(
            phrase in result_lower
            for phrase in (
                "no conversation found",
                "session not found",
                "conversation not found",
                "invalid session",
            )
        ):
            self.session_invalid = True

        if self.result:
            log(f"[Claude RESULT] {self.result}")

        log(
            f"[Claude END] "
            f"subtype={self.subtype}, "
            f"stop_reason={self.stop_reason}, "
            f"terminal_reason={self.terminal_reason}, "
            f"turns={self.num_turns}, "
            f"error={self.is_error}"
        )


# ============================================================
# Claude CLI
# ============================================================

def build_claude_command(
    prompt: str,
    session_id: Optional[str],
) -> list[str]:
    cmd = [CLAUDE_COMMAND]

    if session_id:
        cmd += ["--resume", session_id]

    cmd += [
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--max-turns",
        str(CLAUDE_MAX_TURNS),
        "--dangerously-skip-permissions",
    ]

    return cmd


def create_stream_log_path() -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    random_part = random.randint(1000, 9999)
    return STREAM_LOG_DIR / f"claude_{timestamp}_{random_part}.jsonl"


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    try:
        proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass

    try:
        proc.kill()
    except Exception:
        pass


def _reader_thread(
    stdout,
    output_queue: queue.Queue,
) -> None:
    try:
        for line in iter(stdout.readline, ""):
            output_queue.put(("line", line))
    except Exception as e:
        output_queue.put(("reader_error", e))
    finally:
        output_queue.put(("eof", None))


def run_claude_streaming(
    prompt: str,
    session_id: Optional[str] = None,
) -> ClaudeExecution:
    execution = ClaudeExecution()
    cmd = build_claude_command(prompt, session_id)

    log(
        "[Claude] 启动进程 "
        f"(session={session_id or 'NEW'})"
    )
    log(f"[Claude] prompt长度={len(prompt)}")

    stream_log_path = create_stream_log_path()
    log(f"[Claude] 原始事件日志: {stream_log_path.name}")

    try:
        claude_path = shutil.which(CLAUDE_COMMAND) or CLAUDE_COMMAND

        proc = subprocess.Popen(
            [claude_path] + cmd[1:],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_DIR),
            shell=False,
            bufsize=1,
        )
    except FileNotFoundError:
        execution.errors.append("Claude CLI not found")
        log("[Claude ERROR] 找不到 claude 命令")
        return execution
    except Exception as e:
        execution.errors.append(str(e))
        log(f"[Claude ERROR] 启动失败: {e}")
        return execution

    execution.last_event_time = time.time()

    output_queue: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=_reader_thread,
        args=(proc.stdout, output_queue),
        daemon=True,
    )
    reader.start()

    eof_received = False

    try:
        with stream_log_path.open("w", encoding="utf-8") as raw_log:
            while True:
                if STOP_REQUESTED:
                    log("[Claude] 收到停止请求，终止 Claude")
                    terminate_process(proc)
                    break

                try:
                    kind, value = output_queue.get(timeout=0.2)
                except queue.Empty:
                    kind = None
                    value = None

                if kind == "line":
                    line = value
                    execution.last_event_time = time.time()
                    execution.idle_warning_sent = False

                    raw_log.write(line)
                    raw_log.flush()

                    stripped = line.rstrip("\r\n")
                    if not stripped:
                        continue

                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        execution.errors.append(
                            f"非JSON输出: {stripped[:500]}"
                        )
                        log(
                            "[Claude WARN] 非JSON输出: "
                            f"{stripped[:500]}"
                        )
                        continue

                    execution.process_event(event)
                    continue

                if kind == "reader_error":
                    error_text = str(value)
                    execution.errors.append(
                        f"stream reader error: {error_text}"
                    )
                    log(f"[Claude ERROR] stream reader: {error_text}")
                    continue

                if kind == "eof":
                    eof_received = True

                # process 退出且 stdout EOF，正常结束
                if eof_received and proc.poll() is not None:
                    break

                # 真正有效的 watchdog：
                # 主线程没有被 readline() 阻塞。
                idle = time.time() - execution.last_event_time

                if (
                    idle >= IDLE_WARNING_SECONDS
                    and not execution.idle_warning_sent
                ):
                    execution.idle_warning_sent = True
                    log(
                        "[Claude WATCHDOG] "
                        f"已经 {idle:.0f}s 没有收到事件，"
                        f"last={execution.last_event_description or 'unknown'}"
                    )

                if idle >= IDLE_KILL_SECONDS:
                    log(
                        "[Claude WATCHDOG] "
                        f"超过 {IDLE_KILL_SECONDS}s 无事件，"
                        "终止 Claude 进程"
                    )
                    terminate_process(proc)
                    break

    except KeyboardInterrupt:
        log("[Claude] KeyboardInterrupt")
        terminate_process(proc)

    except Exception as e:
        execution.errors.append(str(e))
        log(f"[Claude ERROR] 读取 stream 失败: {e}")
        terminate_process(proc)

    finally:
        try:
            execution.returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("[Claude] 等待退出超时，kill")
            try:
                proc.kill()
            except Exception:
                pass

            try:
                execution.returncode = proc.wait(timeout=5)
            except Exception:
                execution.returncode = -1
        except Exception:
            execution.returncode = proc.returncode

    if execution.session_id:
        save_session_id(execution.session_id)

    return execution


# ============================================================
# Git
# ============================================================

def run_git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_DIR),
            shell=False,
            timeout=30,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stderr:
            return f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"

        return stdout

    except subprocess.TimeoutExpired:
        return "git command timeout"

    except Exception as e:
        return f"git error: {e}"


def get_git_status() -> str:
    return run_git(["status", "--short"])


def get_git_log() -> str:
    return run_git(["log", "--oneline", "-10"])


# ============================================================
# DeepSeek context
# ============================================================

def build_execution_summary(
    execution: ClaudeExecution,
) -> str:
    lines: list[str] = []

    lines.append("=== CLAUDE EXECUTION ===")
    lines.append(f"session_id: {execution.session_id}")
    lines.append(f"returncode: {execution.returncode}")
    lines.append(f"subtype: {execution.subtype}")
    lines.append(f"stop_reason: {execution.stop_reason}")
    lines.append(f"terminal_reason: {execution.terminal_reason}")
    lines.append(f"num_turns: {execution.num_turns}")
    lines.append(f"is_error: {execution.is_error}")
    lines.append(f"auth_failed: {execution.auth_failed}")
    lines.append(f"session_invalid: {execution.session_invalid}")
    lines.append(f"events: {execution.events_count}")

    if execution.total_cost_usd is not None:
        lines.append(f"cost_usd: {execution.total_cost_usd}")

    lines.append("")
    lines.append("=== TOOL CALLS ===")

    if not execution.tool_calls:
        lines.append("(none)")
    else:
        for i, call in enumerate(execution.tool_calls, 1):
            name = call.get("name", "unknown")
            tool_input = call.get("input", {})
            input_text = json.dumps(
                tool_input,
                ensure_ascii=False,
            )

            if len(input_text) > 1500:
                input_text = input_text[:1500] + "..."

            lines.append(
                f"{i}. {name}: {input_text}"
            )

    lines.append("")
    lines.append("=== TOOL RESULTS ===")

    if not execution.tool_results:
        lines.append("(none)")
    else:
        for i, result in enumerate(execution.tool_results, 1):
            content = str(result.get("content", ""))

            if len(content) > 2000:
                content = content[:2000] + "..."

            lines.append(
                f"{i}. error={result.get('is_error')}\n"
                f"{content}"
            )

    lines.append("")
    lines.append("=== ASSISTANT OUTPUT ===")

    assistant_text = "".join(execution.assistant_text).strip()

    if assistant_text:
        if len(assistant_text) > 6000:
            assistant_text = assistant_text[-6000:]
        lines.append(assistant_text)
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("=== ERRORS ===")

    if execution.errors:
        lines.extend(execution.errors)
    else:
        lines.append("(none)")

    return "\n".join(lines)


# ============================================================
# DeepSeek
# ============================================================

DEEPSEEK_SYSTEM_PROMPT = """
你是一个严格的 Claude Code 任务监工。

你的任务不是评价 Claude 的回答写得好不好，而是判断：
1. 原始任务是否真正完成；
2. Claude 是否还有未完成的实际工作；
3. 如果没有完成，下一轮应该让 Claude 做什么。

必须基于提供的执行事实判断。

特别注意：
- Claude 说“完成了”不等于任务真的完成。
- 只执行了部分步骤不能判定 done=true。
- 如果任务要求修改代码，应该结合 git status、工具调用、工具结果判断。
- 如果 Claude 因认证、网络、进程错误而失败，不能判定任务完成。
- 如果信息不足，默认 done=false。
- next_prompt 必须是可以直接喂给 Claude Code 的具体行动指令。
- 不要要求 Claude 重复已经成功完成的工作。
- 如果 Claude 已经完成所有要求，next_prompt 应为空字符串。

只输出 JSON：

{
  "done": true 或 false,
  "confidence": 0.0 到 1.0,
  "reason": "简短说明为什么",
  "next_prompt": "下一步直接给 Claude Code 的指令；如果完成则为空字符串"
}
""".strip()


def ask_deepseek(
    task_prompt: str,
    execution: ClaudeExecution,
) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        log("[DeepSeek ERROR] 环境变量 DEEPSEEK_API_KEY 未设置")
        return {
            "done": False,
            "confidence": 0.0,
            "reason": "DEEPSEEK_API_KEY 未设置",
            "next_prompt": "",
        }

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=DEEPSEEK_TIMEOUT,
    )

    execution_summary = build_execution_summary(execution)
    git_status = get_git_status()
    git_log = get_git_log()

    context = f"""
=== ORIGINAL TASK ===

{task_prompt}

{execution_summary}

=== GIT STATUS ===

{git_status}

=== RECENT GIT COMMITS ===

{git_log}
""".strip()

    log("[DeepSeek] 开始判断任务状态...")

    try:
        response = (
            client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": DEEPSEEK_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": context,
                    },
                ],
                response_format={"type": "json_object"},
            )
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError("DeepSeek 返回空内容")

        verdict_raw = json.loads(content)

        done = bool(verdict_raw.get("done", False))

        try:
            confidence = float(
                verdict_raw.get("confidence", 0)
            )
        except (TypeError, ValueError):
            confidence = 0.0

        confidence = max(0.0, min(1.0, confidence))

        reason = str(
            verdict_raw.get("reason", "")
        )

        next_prompt = str(
            verdict_raw.get("next_prompt", "")
            or ""
        ).strip()

        verdict = {
            "done": done,
            "confidence": confidence,
            "reason": reason,
            "next_prompt": next_prompt,
        }

        log(
            "[DeepSeek] "
            f"done={done}, "
            f"confidence={confidence:.2f}"
        )
        log(f"[DeepSeek] reason={reason}")

        return verdict

    except Exception as e:
        log(f"[DeepSeek ERROR] {e}")

        # DeepSeek 出错绝对不能判定任务完成。
        return {
            "done": False,
            "confidence": 0.0,
            "reason": f"DeepSeek 判断失败: {e}",
            "next_prompt": "",
        }


# ============================================================
# 失败分类
# ============================================================

def classify_execution_failure(
    execution: ClaudeExecution,
) -> str:
    if execution.auth_failed:
        return "AUTH_FAILURE"

    if execution.returncode is None:
        return "START_FAILURE"

    if execution.returncode != 0:
        return "PROCESS_FAILURE"

    if execution.is_error:
        return "CLAUDE_ERROR"

    if execution.terminal_reason in (
        "aborted_streaming",
        "error",
        "api_error",
    ):
        return "STREAM_ERROR"

    return "UNKNOWN"


def failure_backoff(delay: float) -> float:
    sleep_time = min(delay, MAX_DELAY) * (
        1 + random.uniform(-JITTER, JITTER)
    )

    log(
        f"[任务失败] {sleep_time:.0f}s 后重试"
    )

    end_time = time.time() + sleep_time

    while time.time() < end_time:
        if STOP_REQUESTED:
            break
        time.sleep(min(1.0, end_time - time.time()))

    return min(
        delay * BACKOFF_FACTOR,
        MAX_DELAY,
    )


# ============================================================
# 单轮执行
# ============================================================

# run_round() 的返回值：把控制流交回 main()，由 main() 独占跨轮状态。
ROUND_CONTINUE = "continue"                  # 本轮正常结束，任务尚未完成
ROUND_FAILED = "failed"                      # Claude 本轮失败，可退避重试
ROUND_SESSION_INVALID = "session_invalid"    # session 不可用，需清除后重开
ROUND_DONE = "done"                          # 任务确认完成
ROUND_FATAL = "fatal"                        # 不可恢复（例如认证失败）

# DeepSeek 未给出 next_prompt 时的保守兜底指令。
DEFAULT_CONTINUATION_PROMPT = (
    "继续检查当前任务进度。"
    "请读取当前工作区状态，"
    "结合之前已经完成的工作，"
    "继续执行尚未完成的任务。"
    "不要重复已经成功完成的步骤。"
    "如果任务已经真正完成，请明确说明完成。"
)

# 失败重试 / 恢复中断会话时附加在 prompt 前面的保护前缀。
#
# 为什么需要它：Claude 可能已经把工作做完，却在汇报之前因网络中断、
# watchdog 强杀或进程异常而退出。此时直接重复下达同一条指令，会让它
# 把已经完成的动作再做一遍——在这类“改版本号 + 打 tag + 发 Release”
# 的任务上，重复副作用是不可逆的（重复 bump、tag 冲突、重复发版）。
RETRY_GUARD_PROMPT = (
    "【重要：本轮是中断后的恢复执行，可能已有部分工作完成】\n"
    "在动手之前，必须先检查仓库与产物的真实状态：\n"
    "git status、git log --oneline -10、git tag、已有的 Release、"
    "以及版本号文件的实际取值。\n"
    "严禁重复执行已经完成的动作，尤其是：\n"
    "不要重复提升版本号；不要重复创建已存在的 tag；"
    "不要重复发布已存在的 Release。\n"
    "如果检查后发现任务其实已经全部完成，请直接明确说明完成，"
    "不要再做任何改动。"
)


def run_round(
    prompt: str,
    session_id: Optional[str],
    task_prompt: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """执行一轮：Claude 执行 → 必要时让 DeepSeek 判断。

    本函数只负责“一轮”，不持有任何跨轮状态；退避、连续失败计数、
    停止判定全部留在 main()。这样即使本函数抛出未预期异常，
    跨轮状态也不会被改坏。

    返回 (action, session_id, next_prompt)。
    """
    # 注意：这里故意没有 wait_for_network()。
    # Claude CLI 本身就是实际的网络 / API 健康检查。
    execution = run_claude_streaming(
        prompt=prompt,
        session_id=session_id,
    )

    if execution.session_id:
        session_id = execution.session_id
        save_session_id(session_id)

    # --------------------------------------------------------
    # 认证失败
    # --------------------------------------------------------

    if execution.auth_failed:
        log(
            "[FATAL] Claude 认证失败。"
            "请在当前环境执行 /login 后再运行 watcher。"
        )
        return ROUND_FATAL, session_id, prompt

    # --------------------------------------------------------
    # session 无效
    # --------------------------------------------------------

    if execution.session_invalid:
        return ROUND_SESSION_INVALID, session_id, prompt

    # --------------------------------------------------------
    # Claude 失败
    # --------------------------------------------------------

    failed = (
        execution.returncode != 0
        or execution.is_error
        or execution.terminal_reason in (
            "aborted_streaming",
            "error",
            "api_error",
        )
    )

    if failed:
        failure = classify_execution_failure(execution)

        log(
            f"[Claude FAILURE] "
            f"type={failure}, "
            f"returncode={execution.returncode}, "
            f"terminal_reason={execution.terminal_reason}"
        )

        # 网络 / API 错误、进程错误等都交给实际 CLI 结果处理，
        # 不再提前做一个独立的网络探测。
        return ROUND_FAILED, session_id, prompt

    # --------------------------------------------------------
    # DeepSeek 判断
    # --------------------------------------------------------

    verdict = ask_deepseek(
        task_prompt=task_prompt,
        execution=execution,
    )

    # --------------------------------------------------------
    # 完成判定
    # --------------------------------------------------------

    if (
        verdict.get("done", False)
        and verdict.get("confidence", 0.0)
        >= DEEPSEEK_MIN_CONFIDENCE
    ):
        log("")
        log("=" * 70)
        log("✅ 任务确认完成")
        log(
            f"confidence="
            f"{verdict.get('confidence', 0):.2f}"
        )
        log(
            f"reason="
            f"{verdict.get('reason', '')}"
        )
        log("=" * 70)
        return ROUND_DONE, session_id, prompt

    # --------------------------------------------------------
    # 未完成：获取下一轮 prompt
    # --------------------------------------------------------

    candidate_prompt = str(
        verdict.get("next_prompt", "")
        or ""
    ).strip()

    if candidate_prompt:
        preview = candidate_prompt.replace("\n", " ")

        log("[WATCHER] DeepSeek 生成下一步 prompt:")
        log(f"[WATCHER] {preview[:300]}")

        next_prompt = candidate_prompt
    else:
        # DeepSeek 没有生成 next_prompt。
        # 使用保守 continuation，不让 Claude 空转。
        next_prompt = DEFAULT_CONTINUATION_PROMPT

        log(
            "[WATCHER] DeepSeek 未提供 next_prompt，"
            "使用默认 continuation prompt"
        )

    log(
        "[WATCHER] Claude 本轮正常结束，但任务尚未完成，"
        "下一轮将使用 --resume"
    )

    return ROUND_CONTINUE, session_id, next_prompt


# ============================================================
# Main
# ============================================================

def main() -> int:
    # 兜底：import 之后 stdout 仍可能被替换（重定向、外层包装器），再设一次。
    configure_stdout()
    ensure_directories()

    if not PROJECT_DIR.exists():
        log(
            f"[FATAL] 项目目录不存在: {PROJECT_DIR}"
        )
        return 1

    if not TASK_PROMPT_FILE.exists():
        log(
            f"[FATAL] Task prompt 不存在: "
            f"{TASK_PROMPT_FILE}"
        )
        return 1

    # 启动前检查 claude 命令是否存在。
    # 这不是网络检测，只是本机 CLI 检查。
    if shutil.which(CLAUDE_COMMAND) is None:
        log(
            "[FATAL] 找不到 claude CLI。"
            "请确认 claude 已加入 PATH。"
        )
        return 1

    task_prompt = load_task_prompt()
    session_id = load_session_id()

    if session_id:
        log(
            f"[启动] 使用已有 session: {session_id}"
        )
        log(
            "[启动] 该 session 来自上次中断，"
            "首轮将附加防止重复动作的保护前缀"
        )
    else:
        log(
            "[启动] 没有旧 session，将创建新 session"
        )

    next_prompt = task_prompt
    task_fail_delay = BASE_DELAY
    consecutive_failures = 0

    # 恢复一个上次中断留下的 session 时，本轮按“重试”对待：
    # 上次会话在未知进度上被打断，直接重复原任务极易造成重复副作用。
    retry_after_failure = bool(session_id)

    iteration = 0

    while not STOP_REQUESTED:
        iteration += 1

        log("")
        log("=" * 70)
        log(f"[WATCHER] 第 {iteration} 轮")
        log("=" * 70)

        # 失败重试 / 恢复中断会话时，附加保护前缀，避免重复副作用。
        # 注意 next_prompt 本身不含前缀，因此不会逐轮累积。
        round_prompt = next_prompt

        if retry_after_failure:
            round_prompt = RETRY_GUARD_PROMPT + "\n\n" + next_prompt

        try:
            action, session_id, next_prompt = run_round(
                prompt=round_prompt,
                session_id=session_id,
                task_prompt=task_prompt,
            )

        except KeyboardInterrupt:
            log("[WATCHER] 收到 KeyboardInterrupt，停止")
            break

        except Exception as e:
            # 无人值守下，任何未预期异常都只允许影响一轮：
            # 记录、退避、继续，绝不让 watcher 自己退出。
            log(
                f"[WATCHER ERROR] 第 {iteration} 轮未预期异常: "
                f"{type(e).__name__}: {e}"
            )
            log(traceback.format_exc())
            task_fail_delay = failure_backoff(task_fail_delay)
            continue

        # ----------------------------------------------------
        # 不可恢复：认证失败等
        # ----------------------------------------------------

        if action == ROUND_FATAL:
            return 2

        # ----------------------------------------------------
        # 任务完成
        # ----------------------------------------------------

        if action == ROUND_DONE:
            return 0

        # ----------------------------------------------------
        # session 无效
        # ----------------------------------------------------

        if action == ROUND_SESSION_INVALID:
            log(
                "[Session] 旧 session 无效，"
                "清除后下一轮创建新 session"
            )

            clear_session_id()
            session_id = None

            # 新 session 必须重新使用原始任务。
            next_prompt = task_prompt
            task_fail_delay = BASE_DELAY
            consecutive_failures = 0

            # 新 session 对已完成的工作一无所知，重复副作用的风险更高。
            retry_after_failure = True

            if STOP_REQUESTED:
                break

            time.sleep(2)
            continue

        # ----------------------------------------------------
        # Claude 失败
        # ----------------------------------------------------

        if action == ROUND_FAILED:
            consecutive_failures += 1

            # 兜底恢复：网络中断、watchdog 强杀等情况下 Claude 来不及
            # 输出 result 事件，session_invalid 不会被置位，仅靠结果文本
            # 匹配永远清不掉可能已经损坏的 session，会一直 --resume 下去。
            # 这里用连续失败次数兜底：达到阈值就丢弃 session，
            # 退回原始任务 prompt 重新开一个干净 session。
            if (
                session_id
                and consecutive_failures >= MAX_CONSECUTIVE_FAILURES
            ):
                log(
                    f"[Session] 同一 session 连续失败 "
                    f"{consecutive_failures} 次，判定为不可用，"
                    "丢弃后重开新 session"
                )

                clear_session_id()
                session_id = None
                consecutive_failures = 0

                # 新 session 必须重新使用原始任务。
                next_prompt = task_prompt
                task_fail_delay = BASE_DELAY

            # 下一轮是“失败重试”而不是 DeepSeek 驱动的续跑：
            # 必须防止 Claude 把中断前已完成的工作重做一遍。
            retry_after_failure = True

            log(
                "[WATCHER] 下一轮为失败重试，"
                "将附加“先检查状态、禁止重复动作”的保护前缀"
            )

            task_fail_delay = failure_backoff(task_fail_delay)
            continue

        # ----------------------------------------------------
        # 本轮正常结束，任务尚未完成
        # ----------------------------------------------------

        # 正常执行后，重置失败 backoff 与连续失败计数。
        task_fail_delay = BASE_DELAY
        consecutive_failures = 0

        # 本轮已经正常收到 Claude 的汇报，后续由 DeepSeek 的
        # next_prompt 驱动，不再需要重复动作保护。
        retry_after_failure = False

    log("[WATCHER] 已停止")
    return 130


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        log("用户中断 watcher")
        sys.exit(130)

    except Exception as e:
        log(f"[FATAL] 未处理异常: {e}")
        sys.exit(1)
