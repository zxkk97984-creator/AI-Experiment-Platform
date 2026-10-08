"""DeepSeek OpenAI 兼容客户端——重试、脱敏、JSON 提取"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger("dai.ai_client")

_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
_NON_RETRYABLE_HTTP_STATUS = {401, 403}

# TASK-028/F-22：各操作 completion 预算（max_tokens）——防止无界输出与成本失控。
# 注意：deepseek-v4-flash 是 reasoning 模型，reasoning_content 与最终 content
# 共用 max_tokens；预算必须给两者同时留空间，否则会出现“推理耗尽 token、content 为空”。
# 新操作必须在此登记预算后才能调用 chat_json（未知操作 fail-closed）。
OPERATION_MAX_TOKENS: dict[str, int] = {
    "lab_grading": 10000,
    "lab_rubric": 8000,
    # 1500 实测不足：reasoning 模型在复杂 Rubric 下推理 token 与 JSON 正文
    # 相加超限 → 输出被截断（Expecting ',' delimiter）甚至 content 为空
    # （finish_reason=length）。max_tokens 是上限不是花费，放宽不影响成功短响应的成本。
    "ai_grading": 8000,             # 单份作业/考试提交评分
    "rubric_generation": 8000,      # Rubric 生成（教师触发/发布门禁；2000 同样被推理耗尽）
    "test_group_generation": 12000, # 测试组生成（推理 + F/R 用例代码，成本最高）
}


class AIServiceError(RuntimeError):
    """AI 服务异常，含可重试标记"""

    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def sanitize_ai_error(text: str) -> str:
    """删除错误文本中的 Bearer token 和疑似 API Key"""
    # 删除 Authorization header 值
    text = re.sub(r"Bearer\s+\S+", "Bearer ***", text)
    # 删除 sk- 开头的 key
    text = re.sub(r"sk-[a-zA-Z0-9]+", "sk-***", text)
    # 截断过长文本
    if len(text) > 1000:
        text = text[:1000]
    return text


def normalize_chat_endpoint(base_url: str) -> str:
    """规范化聊天补全端点"""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def extract_json_object(text: str) -> dict:
    """从文本中提取 JSON 对象——支持 markdown fence 和纯 JSON"""
    text = text.strip()
    # 尝试 markdown fence ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # 尝试提取最外层 {...}
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        text = brace_match.group(0)
    return json.loads(text)


class DeepSeekClient:
    """DeepSeek OpenAI 兼容聊天客户端"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        metrics_sink=None,
    ):
        self._settings = settings
        self._endpoint = normalize_chat_endpoint(settings.ai_base_url)
        # metrics_sink：TASK-029 用量指标回调（op_metrics.ai_metrics_sink），
        # 只接收 {operation, prompt_tokens, completion_tokens}，不含任何原文。
        self._metrics_sink = metrics_sink
        # trust_env=False：出站 AI 调用不读环境代理变量（socks/http 代理注入会
        # 导致请求失败甚至构造期崩溃）；如需代理应作为显式配置项提供。
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.ai_timeout_seconds),
            transport=transport,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return (
            f"DeepSeekClient(endpoint={self._endpoint!r}, "
            f"model={self._settings.ai_model!r})"
        )

    def chat_json(self, messages: list[dict[str, str]], *, operation: str) -> dict:
        """发送聊天请求并返回解析后的 JSON 对象。

        operation 必须在 OPERATION_MAX_TOKENS 中登记（TASK-028）：未登记的操作
        fail-closed 拒绝调用，保证每次 AI 调用都有明确的 completion 预算。
        """
        max_tokens = OPERATION_MAX_TOKENS.get(operation)
        if max_tokens is None:
            raise ValueError(f"AI 操作 {operation!r} 未登记 completion 预算（OPERATION_MAX_TOKENS）")

        # 每操作超时/重试覆盖。测试组生成是同步教师请求：单次放宽到 120s，
        # 模型层不自动重试（业务层还有一次修复生成），避免 60s×4 次重试把
        # 请求拖到前端超时之后。
        operation_timeouts: dict[str, float] = {
            "test_group_generation": self._settings.ai_test_group_timeout_seconds,
        }
        operation_max_retries: dict[str, int] = {
            "test_group_generation": self._settings.ai_test_group_max_retries,
        }
        timeout_seconds = operation_timeouts.get(
            operation, self._settings.ai_timeout_seconds
        )
        max_attempts = 1 + operation_max_retries.get(
            operation, self._settings.ai_max_retries
        )

        request_id = uuid.uuid4().hex[:12]
        payload = {
            "model": self._settings.ai_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._settings.ai_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None

        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            start = time.monotonic()
            try:
                response = self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=httpx.Timeout(timeout_seconds),
                )
                elapsed = time.monotonic() - start

                # 400 且使用了 response_format：降级重试一次（无 response_format）
                if response.status_code == 400 and payload.get("response_format") is not None:
                    logger.warning("AI 不支持 response_format，降级重试")
                    del payload["response_format"]
                    # 兼容性降级不占业务重试预算：允许在 max_retries=0 时再发一次
                    max_attempts += 1
                    continue

                if response.status_code in _NON_RETRYABLE_HTTP_STATUS:
                    body = sanitize_ai_error(response.text[:500])
                    raise AIServiceError(
                        f"http_{response.status_code}",
                        f"AI 认证失败: {body}",
                        retryable=False,
                    )

                if response.status_code in _RETRYABLE_HTTP_STATUS:
                    body = sanitize_ai_error(response.text[:500])
                    raise AIServiceError(
                        f"http_{response.status_code}",
                        f"AI 服务暂时不可用 ({response.status_code}): {body}",
                        retryable=True,
                    )

                response.raise_for_status()
                data = response.json()

                usage = data.get("usage") or {}
                choices = data.get("choices", [])
                logger.info(
                    "ai_chat_completed",
                    extra={
                        "request_id": request_id,
                        "operation": operation,
                        "model": self._settings.ai_model,
                        "status": response.status_code,
                        "finish_reason": choices[0].get("finish_reason")
                        if choices else None,
                        "elapsed_ms": round(elapsed * 1000),
                        "attempts": attempt,
                        # TASK-028：usage 缺失时记录 None，不阻断核算
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                        "max_tokens": max_tokens,
                    },
                )

                if self._metrics_sink is not None:
                    try:
                        self._metrics_sink({
                            "operation": operation,
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                        })
                    except Exception:  # 指标路径绝不阻断业务
                        logger.debug("AI 指标回调失败（忽略）", exc_info=True)

                if not choices:
                    raise AIServiceError(
                        "empty_choices",
                        "AI 返回空的 choices 列表",
                        retryable=True,
                    )

                message = choices[0].get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    finish_reason = choices[0].get("finish_reason")
                    hint = "（finish_reason=length，reasoning token 可能耗尽预算）" if finish_reason == "length" else ""
                    raise AIServiceError(
                        "bad_json",
                        f"AI 返回空 content{hint}",
                        retryable=True,
                    )

                return extract_json_object(content)

            except AIServiceError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
                logger.warning(
                    "ai_retryable_error",
                    extra={
                        "request_id": request_id,
                        "operation": operation,
                        "attempt": attempt,
                        "code": exc.code,
                    },
                )
            except httpx.TimeoutException:
                last_error = AIServiceError(
                    "timeout",
                    f"AI 请求超时 (尝试 {attempt}/{max_attempts})",
                    retryable=True,
                )
                logger.warning(
                    "ai_timeout",
                    extra={"request_id": request_id, "operation": operation, "attempt": attempt},
                )
            except httpx.NetworkError as exc:
                last_error = AIServiceError(
                    "network_error",
                    f"AI 网络错误: {exc}",
                    retryable=True,
                )
                logger.warning(
                    "ai_network_error",
                    extra={"request_id": request_id, "operation": operation, "attempt": attempt},
                )
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = AIServiceError(
                    "bad_json",
                    f"AI 返回非 JSON 内容: {exc}",
                    retryable=True,
                )
                logger.warning(
                    "ai_bad_json",
                    extra={"request_id": request_id, "operation": operation, "attempt": attempt},
                )
            except Exception as exc:
                last_error = AIServiceError(
                    "unknown",
                    f"AI 未知错误: {exc}",
                    retryable=False,
                )
                logger.error(
                    "ai_unknown_error",
                    extra={"request_id": request_id, "attempt": attempt},
                )
                raise last_error

            if attempt < max_attempts:
                backoff = min(2 ** (attempt - 1), 8)
                logger.info(
                    "ai_retrying",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt,
                        "backoff_s": backoff,
                    },
                )
                time.sleep(backoff)

        # 重试耗尽
        logger.error(
            "ai_retries_exhausted",
            extra={
                "request_id": request_id,
                "operation": operation,
                "attempts": max_attempts,
            },
        )
        raise last_error  # type: ignore[misc]

    def close(self) -> None:
        """Release the transport when a bounded authoring/grading request ends."""
        self._client.close()
