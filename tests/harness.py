"""watcher 端到端测试夹具。

设计原则：
- 不 monkeypatch 任何路径全局变量。配置由命令行解析，所以夹具像真实用户
  一样传 argv 给 main()，这本身也会覆盖配置解析这条路径。
- 只替换两个真正的“外部边界”：
    run_claude_streaming  -> 脚本化的轮次剧本
    ask_deepseek          -> 脚本化的验收结论
  因此整个测试不需要网络、不需要 claude CLI、不需要 DEEPSEEK_API_KEY。

环境变量：
  CW_MODULE         指向另一个版本的 watcher 脚本，用于验证用例本身的有效性
                    （例如从 git 取出修复前的版本，确认用例会 FAIL）
  CW_TEST_SANDBOX   覆盖沙箱目录位置
"""

import importlib.util
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

MODULE = Path(
    os.environ.get("CW_MODULE") or (REPO_ROOT / "claude_watcher.py")
).resolve()

# 沙箱放在仓库内的隐藏目录（已被 .gitignore 忽略）。
# 这样测试产物可检查、可复现；可用 CW_TEST_SANDBOX 指向别处。
SANDBOX = Path(
    os.environ.get("CW_TEST_SANDBOX") or (REPO_ROOT / ".test-sandbox")
).resolve()

PROJ = SANDBOX / "proj"
WATCHING = PROJ / ".claude" / "watching"

DEFAULT_TASK = "测试任务：更新内核并发布新版本。\n"
DEFAULT_TASK_NAME = "task_prompt_20260911_01.md"


def load():
    """加载被测模块（每个用例加载一份独立实例，避免相互污染）。"""
    spec = importlib.util.spec_from_file_location("cw", MODULE)
    cw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cw)
    return cw


def setup(cw, task_text=DEFAULT_TASK, task_name=DEFAULT_TASK_NAME, extra_tasks=()):
    """建立沙箱项目与任务文件，返回应当传给 main() 的 argv。

    extra_tasks: [(文件名, 内容), ...]，用于验证“取字典序最大者”。
    """
    if PROJ.exists():
        shutil.rmtree(PROJ, ignore_errors=True)

    WATCHING.mkdir(parents=True, exist_ok=True)

    task_file = WATCHING / task_name
    task_file.write_text(task_text, encoding="utf-8")

    for name, text in extra_tasks:
        (WATCHING / name).write_text(text, encoding="utf-8")

    # 预装一份等价配置：供 main() 之前就要用到路径的检查使用
    # （例如 case_stuck 建好 git 仓库后直接调用 get_progress_fingerprint）。
    cw.CONFIG = cw.Config(
        project_dir=PROJ,
        task_prompt_file=task_file,
        base_delay=0.2,
        max_delay=0.5,
    )

    return [
        "--project", str(PROJ),
        "--base-delay", "0.2", "--max-delay", "0.5",
        # 用当前解释器冒充 claude：启动前置检查只要求命令存在，
        # 真正的执行已经被 run_claude_streaming 替身接管。
        "--claude-command", sys.executable,
    ]


def ok_execution(cw, session_id="sess-1", text="本轮工作完成"):
    ex = cw.ClaudeExecution()
    ex.session_id = session_id
    ex.returncode = 0
    ex.subtype = "success"
    ex.is_error = False
    ex.assistant_text = [text]
    return ex


def fail_execution(cw, session_id="sess-1", returncode=1):
    ex = cw.ClaudeExecution()
    ex.session_id = session_id
    ex.returncode = returncode
    ex.is_error = True
    ex.assistant_text = ["失败了"]
    return ex


def install_script(cw, script):
    """script: [callable(prompt, session_id) -> ClaudeExecution 或抛异常, ...]"""
    calls = {"n": 0, "prompts": [], "sessions": []}

    def fake_run(prompt, session_id=None):
        calls["prompts"].append(prompt)
        calls["sessions"].append(session_id)
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            raise AssertionError(f"剧本用完：第 {i + 1} 轮没有定义行为")
        return script[i](prompt, session_id)

    cw.run_claude_streaming = fake_run
    return calls


def install_verdicts(cw, verdicts):
    """verdicts: [DeepSeek 返回值 dict, ...]，用完后一律返回 done=False。"""
    calls = {"n": 0}

    def fake_ask(task_prompt, execution):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(verdicts):
            return {
                "done": False, "confidence": 0.0,
                "reason": "剧本用完", "next_prompt": "",
            }
        return verdicts[i]

    cw.ask_deepseek = fake_ask
    return calls


def log_text():
    f = WATCHING / "watcher.log"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def session_file():
    return WATCHING / "last_session_id.txt"


def report(title, checks, extra=None):
    """打印检查结果，返回进程退出码。"""
    print("=" * 60)
    print(title)
    print("=" * 60)

    for name, passed, detail in checks:
        mark = "  PASS  " if passed else "  FAIL  "
        print(mark + name + (f"  [{detail}]" if detail else ""))

    if extra:
        print("-" * 60)
        print(extra)

    ok = all(c[1] for c in checks)
    print("RESULT:", "ALL PASS" if ok else "FAILED")
    return 0 if ok else 1
