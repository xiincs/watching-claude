"""用例：连续失败达到阈值必须丢弃 session 并回到原始任务。

场景对应真实故障：网络不稳导致 Claude 中途断流 / 被 watchdog 强杀，
进程来不及输出 result 事件，session_invalid 永远不会被置位。

剧本：已有 session "sess-old" -> 连续 3 次失败 -> 第 4 轮成功且判定完成。
阈值 MAX_CONSECUTIVE_FAILURES 压到 3 便于观察。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

TASK = "测试任务：更新内核并发布新版本。\n"

cw = H.load()
argv = H.setup(cw, task_text=TASK)
argv += ["--max-consecutive-failures", "3"]

# 预置一个已有 session，模拟“延续上次未完成的工作”
H.session_file().write_text("sess-old", encoding="utf-8")

calls = H.install_script(cw, [
    lambda p, s: H.fail_execution(cw, session_id="sess-old"),
    lambda p, s: H.fail_execution(cw, session_id="sess-old"),
    lambda p, s: H.fail_execution(cw, session_id="sess-old"),
    lambda p, s: H.ok_execution(cw, session_id="sess-new"),
])
H.install_verdicts(cw, [
    {"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""},
])

rc = cw.main(argv)
log = H.log_text()
sessions = calls["sessions"]
prompts = calls["prompts"]

checks = [
    ("main() 返回 0", rc == 0, f"rc={rc}"),
    ("前 3 轮都用旧 session",
     sessions[:3] == ["sess-old"] * 3, str(sessions[:3])),
    ("第 3 次失败触发丢弃：第 4 轮拿到 session_id=None",
     len(sessions) > 3 and sessions[3] is None,
     f"sessions={sessions}"),
    ("丢弃后回到原始任务 prompt",
     len(prompts) > 3 and prompts[3].endswith(TASK), repr(prompts[3])[-30:] if len(prompts) > 3 else "N/A"),
    ("丢弃后的重试轮带重复动作保护前缀",
     len(prompts) > 3 and prompts[3].startswith(cw.RETRY_GUARD_PROMPT) and prompts[3].count(cw.RETRY_GUARD_PROMPT) == 1,
     ""),
    ("日志明确记录丢弃原因", "连续失败 3 次" in log and "丢弃后重开新 session" in log, ""),
    ("新 session 被正常保存",
     H.session_file().read_text(encoding="utf-8") == "sess-new",
     ""),
]

tail = "\n".join([l for l in log.splitlines() if "Session" in l or "第 " in l][-10:])
sys.exit(H.report("CASE: session 连续失败自愈", checks, tail))
