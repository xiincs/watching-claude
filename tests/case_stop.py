"""用例：Ctrl+C 停止不得留下假的 PROCESS_FAILURE 与退避日志。

真实故障复现：停止请求会让 run_claude_streaming 强杀 Claude，
进程退出码必然是 1，于是被判为 PROCESS_FAILURE 并打印“Ns 后重试”，
紧接着才打印“已停止”——事后复盘会把一次正常的人工停止误读成故障。

剧本：第 1 轮里模拟收到停止请求（进程被杀、returncode=1）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

TASK = "测试任务：更新内核并发布新版本。\n"

cw = H.load()
argv = H.setup(cw, task_text=TASK)


def stopped_round(prompt, session_id):
    # 模拟：Claude 正在跑的时候用户按下 Ctrl+C，进程被强杀
    cw.STOP_REQUESTED = True
    return H.fail_execution(cw, session_id="sess-1", returncode=1)


calls = H.install_script(cw, [stopped_round])
verdict_calls = H.install_verdicts(cw, [])

rc = cw.main(argv)
log = H.log_text()

checks = [
    ("main() 返回 130（停止退出码）", rc == 130, f"rc={rc}"),
    ("日志说明本轮是被停止请求中断", "本轮因停止请求中断" in log, ""),
    ("不再记为 PROCESS_FAILURE", "[Claude FAILURE]" not in log and "PROCESS_FAILURE" not in log, ""),
    ("不再打印假的退避重试", "[任务失败]" not in log, ""),
    ("循环确实结束", "已停止" in log, ""),
    ("停止的轮次不会再去问 DeepSeek", verdict_calls["n"] == 0, f"n={verdict_calls['n']}"),
]

tail = "\n".join([l for l in log.splitlines() if l.strip()][-6:])
sys.exit(H.report("CASE: Ctrl+C 停止不误报失败", checks, tail))
