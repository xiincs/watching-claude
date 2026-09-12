"""用例：配置解析（自动发现 / 优先级 / 错误提示）。

覆盖：
  1. 目标项目：命令行 > 环境变量 > 当前工作目录
  2. 任务文件：自动取字典序最大者；task_prompt.md 可“钉住”；--task 三种写法
  3. 解析失败时给出可操作的错误
  4. --list-tasks 的输出与退出码
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness as H

checks = []

cw = H.load()
argv = H.setup(cw, extra_tasks=[("task_prompt_20260912_01.md", "更新的任务\n")])


def parse(extra=()):
    return cw.build_arg_parser().parse_args(["--project", str(H.PROJ), *extra])


def resolve(extra=()):
    return cw.resolve_config(parse(extra))


def expect_error(extra=(), project=None):
    """返回错误信息（没抛错则返回 None）。"""
    try:
        args = cw.build_arg_parser().parse_args(
            (["--project", str(project)] if project else ["--project", str(H.PROJ)]) + list(extra)
        )
        cw.resolve_config(args)
        return None
    except cw.ConfigError as e:
        return str(e)


# ---- 1. 任务文件自动发现 ----
cfg = resolve()
checks.append((
    "自动取字典序最大的任务文件",
    cfg.task_prompt_file.name == "task_prompt_20260912_01.md",
    cfg.task_prompt_file.name,
))

# ---- 2. --task 的三种写法 ----
for label, value, expected in (
    ("--task 传文件名", "task_prompt_20260911_01.md", "task_prompt_20260911_01.md"),
    ("--task 传主干名", "task_prompt_20260911_01", "task_prompt_20260911_01.md"),
    ("--task 传完整路径", str(H.WATCHING / "task_prompt_20260912_01.md"), "task_prompt_20260912_01.md"),
):
    got = resolve(["--task", value]).task_prompt_file.name
    checks.append((label, got == expected, got))

# ---- 3. 派生路径都跟着 project_dir 走 ----
cfg = resolve()
checks.append((
    "派生路径跟随 project_dir",
    cfg.watching_dir == H.WATCHING and cfg.session_file.parent == H.WATCHING
    and cfg.stream_log_dir.parent == H.WATCHING and cfg.watcher_log_file.parent == H.WATCHING,
    str(cfg.watching_dir),
))

# ---- 4. 优先级：命令行 > 环境变量 > cwd ----
# 只测项目目录解析：任务文件的存在性不该干扰优先级判定。
os.environ["WATCHER_PROJECT_DIR"] = str(H.PROJ)
try:
    from_env = cw.resolve_project_dir(cw.build_arg_parser().parse_args([]))
    checks.append((
        "无参数时用环境变量 WATCHER_PROJECT_DIR",
        from_env == H.PROJ.resolve(),
        str(from_env),
    ))

    cli_wins = cw.resolve_project_dir(
        cw.build_arg_parser().parse_args(["--project", str(H.SANDBOX)])
    )
    checks.append((
        "命令行覆盖环境变量",
        cli_wins == H.SANDBOX.resolve(),
        str(cli_wins),
    ))
finally:
    os.environ.pop("WATCHER_PROJECT_DIR", None)

old_cwd = Path.cwd()
try:
    os.chdir(H.PROJ)
    from_cwd = cw.resolve_project_dir(cw.build_arg_parser().parse_args([]))
    checks.append((
        "无参数且无环境变量时用当前工作目录",
        from_cwd == H.PROJ.resolve(),
        str(from_cwd),
    ))
finally:
    os.chdir(old_cwd)

# ---- 5. 错误提示 ----
missing_project = H.SANDBOX / "not-a-project"
msg = expect_error(project=missing_project)
checks.append((
    "项目目录不存在时给出可操作提示",
    msg is not None and "不存在" in msg and "--project" in msg,
    (msg or "").splitlines()[0] if msg else "未抛错",
))

msg = expect_error(["--task", "没有这个文件.md"])
checks.append((
    "任务文件找不到时列出候选",
    msg is not None and "找不到任务 prompt" in msg and "task_prompt_20260912_01.md" in msg,
    "ok" if msg else "未抛错",
))

# 临时清空任务文件，验证“一个都没有”的提示
saved = {p.name: p.read_text(encoding="utf-8") for p in H.WATCHING.glob("task_prompt*.md")}
for p in H.WATCHING.glob("task_prompt*.md"):
    p.unlink()
msg = expect_error()
checks.append((
    "没有任务文件时提示如何创建",
    msg is not None and "没有找到任务 prompt" in msg and "task_prompt*.md" in msg,
    "ok" if msg else "未抛错",
))
for name, text in saved.items():
    (H.WATCHING / name).write_text(text, encoding="utf-8")

# ---- 6. task_prompt.md 钉住 ----
(H.WATCHING / "task_prompt.md").write_text("钉住的任务\n", encoding="utf-8")
cfg = resolve()
checks.append((
    "task_prompt.md 优先于带日期的任务文件",
    cfg.task_prompt_file.name == "task_prompt.md",
    cfg.task_prompt_file.name,
))
(H.WATCHING / "task_prompt.md").unlink()

# ---- 7. --list-tasks ----
rc = cw.main(["--project", str(H.PROJ), "--list-tasks"])
checks.append(("--list-tasks 退出码为 0", rc == 0, f"rc={rc}"))

# ---- 8. main() 在配置错误时返回 1 而不是抛异常 ----
rc = cw.main(["--project", str(missing_project)])
checks.append(("配置错误时 main() 返回 1", rc == 1, f"rc={rc}"))

sys.exit(H.report("CASE: 配置解析", checks))
