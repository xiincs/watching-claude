"""用例：失败重试 / 恢复中断会话时附加“禁止重复动作”保护前缀。

场景 A（恢复中断会话）：启动时已有 session，首轮就必须带保护前缀；
之后一轮正常完成 → 前缀消失（改由 DeepSeek 驱动）；
再失败一轮 → 前缀重新出现，且不得逐轮累积。

场景 B（全新启动）：没有旧 session 也没有失败，首轮不应带前缀。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

TASK = "测试任务：更新内核并发布新版本。\n"
checks = []

# ---------------- 场景 A：恢复中断会话 ----------------
cw = H.load()
argv = H.setup(cw, task_text=TASK)
H.session_file().write_text("sess-old", encoding="utf-8")

calls = H.install_script(cw, [
    lambda p, s: H.ok_execution(cw, session_id="sess-1"),   # 1 正常
    lambda p, s: H.ok_execution(cw, session_id="sess-1"),   # 2 正常
    lambda p, s: H.fail_execution(cw, session_id="sess-1"),  # 3 失败
    lambda p, s: H.ok_execution(cw, session_id="sess-1"),   # 4 完成
])
H.install_verdicts(cw, [
    {"done": False, "confidence": 0.3, "reason": "还差一步", "next_prompt": "执行第二步"},
    {"done": False, "confidence": 0.3, "reason": "还差一步", "next_prompt": "执行第三步"},
    {"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""},
])

rc = cw.main(argv)
log = H.log_text()
prompts = calls["prompts"]
# 旧版本没有这个常量；用 getattr 让它退化为 FAIL 而不是崩溃
G = getattr(cw, "RETRY_GUARD_PROMPT", None)
if G is None:
    checks.append(("A: 版本存在 RETRY_GUARD_PROMPT 常量", False, "AttributeError"))
    G = "\x00NONEXISTENT\x00"

checks += [
    ("A: main() 返回 0", rc == 0, f"rc={rc}"),
    ("A: 首轮（恢复中断会话）带保护前缀",
     len(prompts) > 0 and prompts[0].startswith(G) and prompts[0].endswith(TASK), ""),
    ("A: 正常续跑轮不带保护前缀",
     len(prompts) > 1 and prompts[1] == "执行第二步", repr(prompts[1])[:30] if len(prompts) > 1 else "N/A"),
    ("A: 失败后重试轮重新带保护前缀",
     len(prompts) > 3 and prompts[3].startswith(G) and prompts[3].endswith("执行第三步"), ""),
    ("A: 保护前缀不逐轮累积（只出现一次）",
     len(prompts) > 3 and prompts[3].count(G) == 1, f"count={prompts[3].count(G) if len(prompts) > 3 else 'N/A'}"),
    ("A: 日志记录了重试保护", "下一轮为失败重试" in log, ""),
]

# ---------------- 场景 B：全新启动 ----------------
cw2 = H.load()
argv2 = H.setup(cw2, task_text=TASK)
calls2 = H.install_script(cw2, [lambda p, s: H.ok_execution(cw2, session_id="sess-1")])
H.install_verdicts(cw2, [{"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""}])

rc2 = cw2.main(argv2)
p2 = calls2["prompts"]

checks += [
    ("B: 全新启动 main() 返回 0", rc2 == 0, f"rc={rc2}"),
    ("B: 首轮不带保护前缀（无中断、无失败）",
     len(p2) > 0 and p2[0] == TASK and getattr(cw2, "RETRY_GUARD_PROMPT", G) not in p2[0],
     repr(p2[0])[:40] if p2 else "N/A"),
]

# ---------------- 场景 C：连续多次失败 ----------------
# 这是场景 A 的加强版：只失败一次时前缀不会叠加，连续失败才会暴露
# “失败分支把带前缀的 prompt 回传给 next_prompt”这个缺陷。
cw3 = H.load()
argv3 = H.setup(cw3, task_text=TASK)
H.session_file().write_text("sess-old", encoding="utf-8")

calls3 = H.install_script(cw3, [
    lambda p, s: H.fail_execution(cw3, session_id="sess-old"),  # 1 失败
    lambda p, s: H.fail_execution(cw3, session_id="sess-old"),  # 2 失败
    lambda p, s: H.fail_execution(cw3, session_id="sess-old"),  # 3 失败
    lambda p, s: H.ok_execution(cw3, session_id="sess-old"),    # 4 完成
])
H.install_verdicts(cw3, [{"done": True, "confidence": 0.99, "reason": "完成", "next_prompt": ""}])

rc3 = cw3.main(argv3)
p3 = calls3["prompts"]
G3 = getattr(cw3, "RETRY_GUARD_PROMPT", G)
counts = [x.count(G3) for x in p3]
lens = [len(x) for x in p3]

checks += [
    ("C: 连续 3 次失败后仍能完成", rc3 == 0, f"rc={rc3}"),
    ("C: 每轮保护前缀都只出现一次", all(c == 1 for c in counts), f"counts={counts}"),
    ("C: prompt 长度不随失败次数增长", len(set(lens[:3])) == 1, f"lens={lens}"),
]

sys.exit(H.report("CASE: 失败重试语义保护", checks))
