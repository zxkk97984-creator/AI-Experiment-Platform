"""Validate teacher-uploaded Python as text; never import or execute it."""
import ast

from app.errors import api_error

MAX_CHECK_SCRIPT_BYTES = 20000


def validate_check_script(filename: str | None, content: bytes) -> str:
    if not (filename or '').lower().endswith('.py') or len(content) > MAX_CHECK_SCRIPT_BYTES:
        raise api_error(422, 'LAB_CHECK_SCRIPT_INVALID', '请上传不超过 20,000 字节的 .py 文件')
    try:
        script = content.decode('utf-8')
        ast.parse(script)
    except (UnicodeError, SyntaxError, ValueError):
        raise api_error(422, 'LAB_CHECK_SCRIPT_INVALID', '检查脚本须为有效的 UTF-8 Python 代码')
    return script
