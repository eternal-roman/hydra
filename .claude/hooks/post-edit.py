"""Path-scoped post-edit hook for Hydra (Windows-native, no bash dependency).

Runs the narrowest possible verification step based on which file was edited.
Disable for rapid iteration: set HYDRA_POSTEDIT_HOOK_DISABLED=1
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def main():
    if os.environ.get("HYDRA_POSTEDIT_HOOK_DISABLED") == "1":
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ti = data.get("tool_input") or {}
    filepath = ti.get("file_path") or ti.get("path") or ""
    if not filepath:
        return

    f = filepath.replace("\\", "/")

    if f.endswith("hydra_engine.py") or f.endswith("hydra_tuner.py"):
        print("[post-edit] running tests/test_engine.py + tests/test_tuner.py")
        subprocess.run([sys.executable, "tests/test_engine.py"],
                       cwd=REPO_ROOT, timeout=60)
        subprocess.run([sys.executable, "tests/test_tuner.py"],
                       cwd=REPO_ROOT, timeout=60)

    elif f.endswith("hydra_agent.py"):
        print("[post-edit] running execution-path harness (smoke)")
        subprocess.run([sys.executable, "tests/live_harness/harness.py", "--mode", "smoke"],
                       cwd=REPO_ROOT, timeout=60)

    elif "hydra_companions/" in f and f.endswith(".py"):
        print("[post-edit] running companion test subset")
        subprocess.run([sys.executable, "-m", "pytest", "tests/test_companion_soul.py",
                        "tests/test_companion_chat.py", "-x", "--tb=short"],
                       cwd=REPO_ROOT, timeout=60)

    elif any(f.endswith(s) for s in ("hydra_backtest.py", "hydra_backtest_metrics.py",
             "hydra_backtest_server.py", "hydra_backtest_tool.py",
             "hydra_experiments.py")):
        print("[post-edit] running backtest test subset")
        subprocess.run([sys.executable, "-m", "pytest",
                        "tests/test_backtest_engine.py", "tests/test_backtest_metrics.py",
                        "tests/test_experiments.py", "-x", "--tb=short"],
                       cwd=REPO_ROOT, timeout=60)

    elif f.endswith(".py"):
        print(f"[post-edit] py_compile: {filepath}")
        subprocess.run([sys.executable, "-m", "py_compile", filepath],
                       cwd=REPO_ROOT, timeout=15)

    elif any(f.endswith(ext) for ext in (".jsx", ".js", ".css")) and "dashboard/src/" in f:
        dashboard_dir = os.path.join(REPO_ROOT, "dashboard")
        print(f"[post-edit] eslint: {filepath}")
        subprocess.run(["npx", "--no-install", "eslint", "--no-warn-ignored", filepath],
                       cwd=dashboard_dir, timeout=30, shell=True)


if __name__ == "__main__":
    main()
