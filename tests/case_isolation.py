"""用例：单轮未预期异常不得终止 watcher。

第 1 轮让 run_claude_streaming 抛 RuntimeError，
第 2 轮正常返回且 DeepSeek 判定完成。

修复前：异常从 main() 冒出去，watcher 直接死掉（顶层 handler -> sys.exit(1)）。
修复后：第 1 轮被记录 + 退避，第 2 轮正常完成，main() 返回 0。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

cw = H.load()
argv = H.setup(cw)


def boom(prompt, session_id):
    raise RuntimeError("boom: 模拟未预期异常")


H.install_script(cw, [
    boom,
    lambda p, s: H.ok_execution(cw, session_id="sess-1"),
])
H.install_verdicts(cw, [
    {"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""},
])

rc = cw.main(argv)
log = H.log_text()

checks = [
    ("main() 正常返回 0（未被异常终止）", rc == 0, f"returncode={rc}"),
    ("第 1 轮异常被记录", "未预期异常" in log and "RuntimeError: boom" in log, ""),
    ("未预期异常被归类为 watcher 错误", "[WATCHER ERROR]" in log, ""),
    ("第 2 轮确实执行了", "第 2 轮" in log, ""),
    ("任务被判定完成", "任务确认完成" in log, ""),
    ("异常未逃逸到顶层 FATAL handler", "[FATAL] 未处理异常" not in log, ""),
]

tail = "\n".join(log.splitlines()[-14:])
sys.exit(H.report("CASE: 单轮异常隔离", checks, tail))
