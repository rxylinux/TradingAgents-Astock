import logging
import os
import random
import time
from contextvars import ContextVar
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient, normalize_content, warn_if_truncated
from .capabilities import get_capabilities
from .validators import validate_model

logger = logging.getLogger(__name__)

# Invocation-local shared retry budget (B1 v3).
# When set (non-None), NormalizedChatOpenAI.invoke() makes exactly 1 HTTP
# request without internal retry — the caller (invoke_structured_or_freetext)
# manages all retries and decrements this budget. Thread-safe via ContextVar.
_shared_budget: ContextVar[Optional[int]] = ContextVar("llm_shared_budget", default=None)

# ── Retry classification (by exception type, not error-string matching) ──

_RETRYABLE_TYPES: tuple = ()
_NON_RETRYABLE_TYPES: tuple = ()
try:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )
    _RETRYABLE_TYPES = (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)
    _NON_RETRYABLE_TYPES = (AuthenticationError, PermissionDeniedError, BadRequestError)
except ImportError:
    pass


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and invocation-local retries.

    SDK's ``max_retries`` is statically set to 0 — ALL retries are managed
    by ``invoke()`` using the user's configured budget (total HTTP requests
    = 1 + user's max_retries). This eliminates the multiplicative explosion
    and enables the structured-output fallback to share the same budget
    without mutating any shared state.

    The user's ``max_retries`` is preserved in ``self._user_max_retries``
    (private attr, not a Pydantic field) so the SDK gets 0 while invoke()
    uses the correct budget.
    """

    def model_post_init(self, __context):
        """Statically zero SDK retries; save user budget for invoke()."""
        super().model_post_init(__context)
        self.__dict__["_user_max_retries"] = self.max_retries
        object.__setattr__(self, "max_retries", 0)
        try:
            rc = getattr(self, "root_client", None)
            if rc is not None and hasattr(rc, "max_retries"):
                rc.max_retries = 0
        except Exception:
            pass

    def _get_retry_budget(self) -> int:
        """Total HTTP attempts per invocation (1 + user max_retries)."""
        return self.__dict__.get("_user_max_retries", 0) + 1

    def invoke(self, input, config=None, **kwargs):
        """Invoke with retries — total requests = 1 + _user_max_retries.

        When a shared budget is active (set by invoke_structured_or_freetext),
        makes exactly 1 request without retry — the caller manages retries.
        Standalone calls use their own budget from _user_max_retries.
        """
        # Shared budget mode: 1 request, caller manages retries
        if _shared_budget.get() is not None:
            response = super().invoke(input, config, **kwargs)
            warn_if_truncated(response, self.model_name)
            return normalize_content(response)

        # Standalone mode: own retry loop
        max_attempts = self._get_retry_budget()
        for attempt in range(max_attempts):
            try:
                response = super().invoke(input, config, **kwargs)
                warn_if_truncated(response, self.model_name)
                return normalize_content(response)
            except _NON_RETRYABLE_TYPES:
                raise
            except Exception as e:
                if not isinstance(e, _RETRYABLE_TYPES):
                    raise
                if attempt < max_attempts - 1:
                    sleep_time = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        "LLM call retryable for %s (attempt %d/%d). Sleeping %.1fs: %s",
                        self.model_name, attempt + 1, max_attempts, sleep_time, e,
                    )
                    time.sleep(sleep_time)
                else:
                    raise

    def with_structured_output(self, schema, *, method=None, **kwargs):
        capabilities = get_capabilities(self.model_name)
        if capabilities.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available"
            )
        method = method or capabilities.preferred_structured_method
        # DeepSeek V4/reasoner accept the schema as a tool, but reject
        # LangChain's function-spec ``tool_choice`` parameter.
        # Use pop-and-override rather than setdefault: with setdefault an
        # explicitly passed tool_choice survives and the API call still fails,
        # so the declared capability would not actually be enforced.
        if method == "function_calling" and not capabilities.supports_tool_choice:
            caller_value = kwargs.pop("tool_choice", None)
            if caller_value is not None:
                logger.warning(
                    "Dropping tool_choice=%r for %s: this model rejects the "
                    "parameter (see llm_clients/capabilities.py).",
                    caller_value, self.model_name,
                )
            kwargs["tool_choice"] = None
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Two quirks that don't apply to other OpenAI-compatible providers:

    1. **Thinking-mode round-trip.** When DeepSeek's thinking models return
       a response with ``reasoning_content``, that field must be echoed
       back as part of the assistant message on the next turn or the API
       fails with HTTP 400. ``_create_chat_result`` captures the field on
       receive and ``_get_request_payload`` re-attaches it on send.

    2. **DeepSeek reasoning models reject ``tool_choice``.** Their schema is
       still bound as a tool, while the capability-aware base class suppresses
       only the incompatible request parameter.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_)):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", [])
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result

class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax M2.x adapter.

    M2.x embeds reasoning in ``<think>`` blocks by default.  The provider's
    ``reasoning_split`` request flag keeps that internal trace out of the
    user-facing content that downstream agents store and render.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        capabilities = get_capabilities(self.model_name)
        if capabilities.supports_reasoning_split:
            payload.setdefault("reasoning_split", True)
        return payload

# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "max_tokens",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs and API key env vars
_PROVIDER_CONFIG = {
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "glm": ("https://api.z.ai/api/paas/v4/", "ZHIPU_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "minimax": ("https://api.minimax.chat/v1", "MINIMAX_API_KEY"),
}

# GLM 专属默认端点（A02）：智谱 Coding 计划。项目默认模型（glm-5.3/glm-5.2）
# 属于该套餐；仅当 provider=glm 且用户未显式配置 base_url 时采用，
# GLM_API_BASE_URL 可覆盖。其他供应商一律使用各自的官方端点。
_GLM_CODING_DEFAULT_URL = "https://open.bigmodel.cn/api/coding/paas/v4"


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        # Generic OpenAI-compatible relay (#77 / #81): the user supplies the
        # base_url and model themselves, and the API key comes from a generic
        # env var. No vendor defaults — this is the escape hatch for any
        # gateway (9Router, AI Router, self-hosted proxy) that speaks the
        # OpenAI Chat Completions API.
        if self.provider == "openai_compatible":
            if not self.base_url:
                raise RuntimeError(
                    "openai_compatible 需要填写 base_url。请在 Web 侧栏「API Base URL」"
                    "或配置 `backend_url` 里填写你的 OpenAI 兼容网关地址"
                    "（例如 https://your-relay.example/v1）。"
                )
            llm_kwargs["base_url"] = self.base_url
            api_key = (
                os.environ.get("OPENAI_COMPATIBLE_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            )
            if api_key:
                llm_kwargs["api_key"] = api_key
            elif "api_key" not in self.kwargs:
                raise RuntimeError(
                    "未找到 openai_compatible 的 API Key。请在 .env 文件或环境变量中设置 "
                    "`OPENAI_COMPATIBLE_API_KEY=你的key`（也接受 `OPENAI_API_KEY`），"
                    "设置后重启程序。"
                )
        # Provider-specific base URL and auth. An explicit base_url on the
        # client (e.g. a corporate proxy) takes precedence over the
        # provider default so users can route through their own gateway.
        elif self.provider in _PROVIDER_CONFIG:
            default_base, api_key_env = _PROVIDER_CONFIG[self.provider]
            if self.provider == "glm" and not self.base_url:
                # GLM 专属默认（A02）：未显式配置端点的 glm 走 Coding 计划
                # 端点，GLM_API_BASE_URL 可覆盖；显式 base_url 仍最优先。
                default_base = (
                    os.getenv("GLM_API_BASE_URL") or _GLM_CODING_DEFAULT_URL
                )
            llm_kwargs["base_url"] = self.base_url or default_base
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
                elif "api_key" not in self.kwargs:
                    # Without this, ChatOpenAI fails downstream with a confusing
                    # "OPENAI_API_KEY must be set" — but deepseek/qwen/glm/minimax
                    # each need their OWN env var. Name the exact one (#42).
                    raise RuntimeError(
                        f"未找到 {self.provider} 的 API Key。请在 .env 文件或环境变量中设置 "
                        f"`{api_key_env}`（例如 `{api_key_env}=你的key`），设置后重启程序。"
                        f"注意：{self.provider} 用的是 {api_key_env}，不是 OPENAI_API_KEY。"
                    )
            else:
                llm_kwargs["api_key"] = "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # SDK-level retries: user's max_retries is saved by model_post_init
        # as _user_max_retries and the SDK field is zeroed there. Here we
        # just pass the user's value through (model_post_init handles the rest).
        llm_kwargs.setdefault("max_retries", 5)

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        # DeepSeek's thinking-mode quirks live in their own subclass so the
        # base NormalizedChatOpenAI stays free of provider-specific branches.
        if self.provider == "deepseek":
            chat_cls = DeepSeekChatOpenAI
        elif self.provider == "minimax":
            chat_cls = MinimaxChatOpenAI
        else:
            chat_cls = NormalizedChatOpenAI
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
