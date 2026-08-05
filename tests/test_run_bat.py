from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_run_bat_uses_runtime_errorlevel_and_verifies_created_environment():
    script = (PROJECT_ROOT / "run.bat").read_text(encoding="utf-8")
    normalized = script.casefold()

    assert "%errorlevel%" not in normalized
    assert "if not errorlevel 1" in normalized
    assert "if errorlevel 1" in normalized
    assert normalized.count('if not exist ".venv\\scripts\\python.exe"') >= 2
    assert "python 3를 찾을 수 없습니다" in normalized
    assert "chcp 65001" in normalized
    assert "exit /b 2" in normalized
