"""Task 6: 静态分析测试——AST 解析、Ruff 诊断、Radon 圈复杂度"""
from pathlib import Path

from app.services.static_analysis import analyze_python

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_valid_code_returns_parseable():
    """合法代码返回 parseable=True"""
    result = analyze_python("def add(a, b):\n    return a + b\n")
    assert result["parseable"] is True
    assert result["syntax_error"] is None


def test_syntax_error_reported():
    """语法错误的代码返回 parseable=False"""
    result = analyze_python("def add(a, b):\n    return a + \n")
    assert result["parseable"] is False
    assert result["syntax_error"] is not None


def test_static_analysis_includes_basic_metrics():
    """静态分析包含基本指标"""
    code = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
    result = analyze_python(code)
    assert result["parseable"] is True
    assert result["line_count"] == 5


def test_static_analysis_does_not_execute_code():
    """静态分析不执行危险代码"""
    code = "import os; os.system('rm -rf /')\n"
    result = analyze_python(code)
    # 不会实际执行，只是语法分析
    assert "parseable" in result


def test_static_analysis_empty_code():
    """空代码处理"""
    result = analyze_python("")
    assert result["parseable"] is True
    assert result["line_count"] == 0


# ─────────────────────────────────────────────────────────────
# A16：固定独立规则集 + Radon 结构校验
# ─────────────────────────────────────────────────────────────

def test_a16_unused_import_and_variable_are_reported(caplog):
    """F401/F841 必须被报出（旧实现继承仓库配置，在 backend cwd 下得到空诊断）。"""
    code = (
        "import os\n"
        "import json\n"
        "import re\n"
        "import math\n"
        "\n"
        "\n"
        "def solve(values):\n"
        "    unused_local = 1\n"
        "    return len(values)\n"
    )
    result = analyze_python(code)
    assert result["tool_status"]["ruff"] == "ok"
    codes = {item["code"] for item in result["diagnostics"]}
    assert "F401" in codes, f"未使用 import 必须报 F401，实际 {codes}"
    assert "F841" in codes, f"未使用局部变量必须报 F841，实际 {codes}"


def test_a16_ruff_runs_isolated_from_repo_config():
    source = (REPO_ROOT / "backend" / "app" / "services" / "static_analysis.py").read_text(
        encoding="utf-8")
    assert '"--isolated"' in source, "必须忽略仓库 ruff 配置"
    assert "RUFF_SELECT" in source and "F401" in source
    assert "--no-cache" in source


def test_a16_radon_structure_is_validated(monkeypatch):
    """radon 返回语义无关的合法 JSON（如 [1,2,3]）不得记成 ok。"""
    import app.services.static_analysis as module

    class _FakeCompleted:
        returncode = 0
        stdout = "[1, 2, 3]"
        stderr = ""

    def fake_run(argv, **kwargs):
        if any("radon" in str(part) for part in argv):
            return _FakeCompleted()
        return _FakeCompleted()  # ruff 也返回同样的非法结构

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.analyze_python("def solve():\n    return 1\n")
    assert result["tool_status"]["radon"] == "invalid_result"
    assert result["tool_status"]["ruff"] == "invalid_result"
    assert result["status"] != "ok"


def test_a16_radon_valid_output_accepted():
    result = analyze_python("def solve():\n    return 1\n")
    assert result["tool_status"]["radon"] == "ok"
    for blocks in result["complexity"].values():
        for block in blocks:
            assert isinstance(block["complexity"], int) and block["complexity"] >= 1
