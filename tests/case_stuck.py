"""用例：原地打转检测——只升级 prompt，绝不停止循环。

在沙箱目标目录里建一个真实 git 仓库，用脚本化轮次驱动：
  r1 无改动（首次记录指纹）
  r2 产生真实改动（进展，计数重置）
  r3 无改动
  r4 无改动 -> 连续 2 轮无变化，命中阈值（STUCK_ROUNDS 压到 2）
  r5 应携带自诊断前缀，并正常完成
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

TASK = "测试任务：更新内核并发布新版本。\n"

cw = H.load()
argv = H.setup(cw, task_text=TASK)
argv += ["--stuck-rounds", "2"]

proj = H.PROJ


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=str(proj),
        capture_output=True, text=True, encoding="utf-8",
    )


git("init", "-b", "main")
git("config", "user.email", "t@example.com")
git("config", "user.name", "tester")
(proj / "version.txt").write_text("1.0.0\n", encoding="utf-8")
git("add", "-A")
git("commit", "-m", "init")

_fp = getattr(cw, "get_progress_fingerprint", None)
print("sandbox is git repo:",
      (_fp() is not None) if _fp else "N/A (该版本无 get_progress_fingerprint)")


def make_progress(prompt, session_id):
    (proj / "progress.txt").write_text(str(time.time()), encoding="utf-8")
    return H.ok_execution(cw, session_id="sess-1")


calls = H.install_script(cw, [
    lambda p, s: H.ok_execution(cw),     # r1 无改动
    make_progress,                        # r2 有改动
    lambda p, s: H.ok_execution(cw),     # r3 无改动
    lambda p, s: H.ok_execution(cw),     # r4 无改动 -> 命中
    lambda p, s: H.ok_execution(cw),     # r5 完成
])
H.install_verdicts(cw, [
    {"done": False, "confidence": 0.3, "reason": "a", "next_prompt": "继续第1步"},
    {"done": False, "confidence": 0.3, "reason": "b", "next_prompt": "继续第2步"},
    {"done": False, "confidence": 0.3, "reason": "c", "next_prompt": "继续第3步"},
    {"done": False, "confidence": 0.3, "reason": "d", "next_prompt": "继续第4步"},
    {"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""},
])

rc = cw.main(argv)
log = H.log_text()
prompts = calls["prompts"]
# 旧版本没有这个常量；用 getattr 让它退化为 FAIL 而不是崩溃
S = getattr(cw, "STUCK_ESCALATION_PROMPT", None)
_has_feature = S is not None
S = S or "\x00NONEXISTENT\x00"

checks = [
    ("版本存在原地打转检测（STUCK_ESCALATION_PROMPT）", _has_feature, ""),
    ("main() 返回 0（循环未被卡死检测中断）", rc == 0, f"rc={rc}"),
    ("共执行 5 轮", len(prompts) == 5, f"n={len(prompts)}"),
    ("r2 有真实进展时不计入停滞", "原地打转" not in log.split("第 3 轮")[0], ""),
    ("r3 不携带自诊断前缀（尚未达阈值）",
     len(prompts) > 3 and S not in prompts[3], ""),
    ("r5 携带自诊断前缀（连续 2 轮无变化后升级）",
     len(prompts) > 4 and S in prompts[4], ""),
    ("自诊断前缀不逐轮累积",
     len(prompts) > 4 and prompts[4].count(S) == 1, f"count={prompts[4].count(S) if len(prompts) > 4 else 'N/A'}"),
    ("日志记录了打转判定与指纹",
     "判定为原地打转" in log and "指纹" in log, ""),
    ("日志明确说明循环不停止", "循环继续，不停止" in log, ""),
    ("自诊断轮仍能完成并正常退出", "任务确认完成" in log, ""),
]

tail = "\n".join([l for l in log.splitlines() if "打转" in l or "第 " in l][-8:])
sys.exit(H.report("CASE: 原地打转检测", checks, tail))
