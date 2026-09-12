"""运行全部用例。

用法：
    python tests/run_all.py            # 只显示汇总，失败时打印该用例输出
    python tests/run_all.py -v         # 显示每个用例的完整输出
    python tests/run_all.py case_stop  # 只跑指定用例

为什么用子进程逐个跑：用例会修改模块全局、os.environ、cwd，
隔离开才能保证互不影响，也便于单个用例独立调试。
"""

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def discover():
    return sorted(p.stem for p in TESTS_DIR.glob("case_*.py"))


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    selected = [a for a in argv if not a.startswith("-")]

    cases = discover()
    if selected:
        unknown = [c for c in selected if c not in cases]
        if unknown:
            print(f"没有这些用例: {', '.join(unknown)}")
            print(f"可用用例: {', '.join(cases)}")
            return 2
        cases = selected

    results = []

    for name in cases:
        proc = subprocess.run(
            [sys.executable, str(TESTS_DIR / f"{name}.py")],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        passed = proc.returncode == 0
        results.append((name, passed, proc.stdout + proc.stderr))

        print(f"  {'PASS' if passed else 'FAIL'}  {name}")

        if verbose or not passed:
            print("-" * 60)
            print((proc.stdout + proc.stderr).rstrip())
            print("-" * 60)

    failed = [name for name, ok, _ in results if not ok]

    print()
    print(f"{len(results) - len(failed)}/{len(results)} cases passed")

    if failed:
        print(f"failed: {', '.join(failed)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
