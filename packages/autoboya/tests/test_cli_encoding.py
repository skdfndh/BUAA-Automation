import os
import subprocess
import sys


def test_help_runs_in_a_real_process_under_windows_legacy_encoding(tmp_path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252" if os.name == "nt" else "utf-8"
    result = subprocess.run([sys.executable, "-m", "autoboya", "--help"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert "博雅" in result.stdout
