"""用例：emoji 不再击穿 watcher（stdout 被重定向到文件时的 gbk 场景）。

无人值守必然重定向输出，Windows 下此时 stdout 编码是 gbk 且
errors=surrogateescape，打印 emoji 会抛 UnicodeEncodeError。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

cw = H.load()
argv = H.setup(cw)

checks = []
ex = cw.ClaudeExecution()


def probe(name, fn):
    try:
        fn()
        checks.append((name, True, ""))
    except Exception as e:
        checks.append((name, False, f"{type(e).__name__}: {str(e)[:60]}"))


probe("Claude 流式正文里的 emoji", lambda: ex.process_event(
    {"type": "stream_event", "event": {"type": "content_block_delta",
     "delta": {"type": "text_delta", "text": "\u2705 emoji in text\n"}}}))

probe("工具结果里的 emoji", lambda: ex.process_event(
    {"type": "user", "message": {"content": [{"type": "tool_result",
     "tool_use_id": "t1", "content": "\u2705 12 tests passed", "is_error": False}]}}))

probe("非 JSON 行里的 emoji", lambda: cw.log("[Claude WARN] \u26a0 x"))

probe("成功日志里的 emoji", lambda: cw.log("\u2705 任务确认完成"))

# 强制退回 gbk，验证即使 reconfigure 被绕过，print 保护也兜得住
sys.stdout.reconfigure(encoding="gbk", errors="surrogateescape")
probe("强制 gbk 下 log(emoji)", lambda: cw.log("\u2705 forced gbk"))
probe("强制 gbk 下 log_raw(emoji)", lambda: cw.log_raw("\u2705 forced gbk raw\n"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

content = H.log_text()
checks.append(("文件日志完整保留 emoji（UTF-8）", "\u2705" in content, ""))

sys.exit(H.report("CASE: emoji / 编码健壮性", checks))
