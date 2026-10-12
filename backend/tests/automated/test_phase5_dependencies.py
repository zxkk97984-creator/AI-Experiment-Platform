"""Phase 5 production-image dependency boundary regressions."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
DEV_ONLY = {"pytest", "fakeredis", "moto"}
# 运行时静态分析工具：AI 评分链路（analyze_python）在后端与 AI Worker 镜像内真实执行，
# 因此它们必须随 requirements.txt 安装，而不是开发专用依赖。
RUNTIME_ANALYSIS_TOOLS = {"ruff", "radon"}


def _package_names(path: Path) -> set[str]:
    names = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        names.add(line.split("[", 1)[0].split("==", 1)[0].lower())
    return names


def test_production_requirements_exclude_test_and_audit_tools():
    runtime = _package_names(BACKEND / "requirements.txt")
    development = _package_names(BACKEND / "requirements-dev.txt")

    assert runtime.isdisjoint(DEV_ONLY)
    assert DEV_ONLY <= development
    # 分析器是运行依赖：必须在 runtime 里、且不在 dev 里重复声明
    assert RUNTIME_ANALYSIS_TOOLS <= runtime, runtime
    assert RUNTIME_ANALYSIS_TOOLS.isdisjoint(development), development


def test_runtime_analysis_tools_are_version_pinned():
    """固定版本：判题/AI 镜像内的静态分析结果必须可复现。"""
    lines = (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pinned[name.lower()] = version
    assert pinned.get("ruff") == "0.12.5"
    assert pinned.get("radon") == "6.0.1"


def test_ci_installs_complete_backend_test_requirements():
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pip install -r requirements-dev.txt" in workflow
    assert "pip install pytest fakeredis" not in workflow


def test_backend_image_installs_runtime_requirements_only():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY requirements-dev.txt" not in dockerfile
    assert "pip install --no-cache-dir -r requirements.txt" in dockerfile
