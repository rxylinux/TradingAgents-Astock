"""A02: 供应商默认端点与 backend_url 优先级。

通用 ``backend_url`` 默认必须是 None——写死任何一家（原为智谱 Coding 端点）
都会在切换供应商后把请求与认证信息发到错误端点。GLM Coding 计划的默认端
点只放在 GLM 专属配置，不影响 DeepSeek/OpenAI/Ollama。
"""

import ast
from pathlib import Path

import pytest

from tradingagents.llm_clients.openai_client import OpenAIClient


def _endpoint_of(llm) -> str:
    return str(llm.openai_api_base or "").rstrip("/")


@pytest.fixture(autouse=True)
def _no_endpoint_env(monkeypatch):
    """隔离端点相关环境变量，测试只反映显式参数。"""
    for var in ("BACKEND_URL", "GLM_API_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


# 测试用假模型名（test-model / gpt-test）会触发"不在已知模型列表"的
# RuntimeWarning——与端点断言无关，压掉以免污染全量输出的 warning 信号。
pytestmark = pytest.mark.filterwarnings(
    "ignore:Model .* is not in the known model list:RuntimeWarning"
)


@pytest.mark.parametrize(
    "provider,expected",
    [
        # GLM 专属默认：智谱 Coding 计划端点（项目默认模型属于该套餐）
        ("glm", "https://open.bigmodel.cn/api/coding/paas/v4"),
        # 其他供应商一律各自官方端点，绝不沿用智谱地址
        ("deepseek", "https://api.deepseek.com"),
        ("ollama", "http://localhost:11434/v1"),
    ],
)
def test_unset_backend_url_uses_provider_default(provider, expected):
    """未显式指定地址时使用所选供应商的默认端点。"""
    llm = OpenAIClient("test-model", None, provider=provider).get_llm()
    assert _endpoint_of(llm) == expected.rstrip("/"), (
        f"{provider} 未显式指定地址时不得沿用其他供应商的端点"
    )


def test_openai_unset_backend_url_keeps_official_default():
    """OpenAI 未显式指定地址时保持官方默认（api.openai.com）。"""
    llm = OpenAIClient("gpt-test", None, provider="openai").get_llm()
    endpoint = _endpoint_of(llm)
    assert "bigmodel" not in endpoint, endpoint
    assert "api.openai.com" in endpoint or endpoint == "", endpoint


@pytest.mark.parametrize("provider", ["glm", "deepseek", "ollama", "openai"])
def test_explicit_gateway_overrides_default(provider):
    """显式网关配置仍然有效（优先级高于一切默认值）。"""
    llm = OpenAIClient(
        "test-model", "https://my-gateway.example/v1", provider=provider
    ).get_llm()
    assert _endpoint_of(llm) == "https://my-gateway.example/v1"


def test_glm_env_var_overrides_coding_default(monkeypatch):
    """GLM_API_BASE_URL 可覆盖 GLM 专属默认端点。"""
    monkeypatch.setenv("GLM_API_BASE_URL", "https://glm-relay.example/v1")
    llm = OpenAIClient("test-model", None, provider="glm").get_llm()
    assert _endpoint_of(llm) == "https://glm-relay.example/v1"


def test_explicit_url_beats_glm_env_var(monkeypatch):
    """优先级：显式 base_url > GLM_API_BASE_URL > GLM Coding 默认。"""
    monkeypatch.setenv("GLM_API_BASE_URL", "https://glm-relay.example/v1")
    llm = OpenAIClient(
        "test-model", "https://explicit.example/v1", provider="glm"
    ).get_llm()
    assert _endpoint_of(llm) == "https://explicit.example/v1"


def test_default_config_backend_url_is_none_without_env(monkeypatch):
    """DEFAULT_CONFIG 的通用默认地址必须是 None（不绑死任何供应商）。

    通过 AST 重新求值模块中的赋值表达式，排除 import 时 dotenv/.env 的
    影响——与审核用例 docs/audit_repros_2026_09_05.py 的方式一致。
    """
    monkeypatch.delenv("BACKEND_URL", raising=False)
    path = Path("tradingagents/default_config.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defaults = next(
        n.value for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" for t in n.targets)
    )
    expr = next(
        v for k, v in zip(defaults.keys, defaults.values)
        if k.value == "backend_url"
    )
    url = eval(compile(ast.Expression(expr), "default_config.py", "eval"), {"os": __import__("os")})
    assert url is None, f"通用 backend_url 默认必须是 None，实际: {url!r}"


def test_web_blank_url_falls_back_to_none(monkeypatch):
    """Web 侧栏地址留空 + 无 BACKEND_URL → backend_url=None（用所选供应商默认）。

    用 AST 只执行 web/app.py 的 _build_config 函数体，不启动 Streamlit
    顶层 UI（与审核用例方式一致）。DEFAULT_CONFIG 显式替换为未被 .env 的
    BACKEND_URL 污染的版本——该文件可能含有用户自己的显式网关配置。
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.delenv("BACKEND_URL", raising=False)
    path = Path("web/app.py")
    node = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_config"
    )
    from types import SimpleNamespace

    ns = {
        "DEFAULT_CONFIG": {**DEFAULT_CONFIG, "backend_url": None},
        "os": __import__("os"),
        "st": SimpleNamespace(session_state={}),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    ns["st"].session_state = {"llm_provider": "deepseek", "llm_base_url": ""}
    config = ns["_build_config"]()
    assert config["backend_url"] is None, config["backend_url"]


def test_web_env_var_gateway_still_wins(monkeypatch):
    """BACKEND_URL 环境变量（含用户 .env）在 Web 留空时仍然生效——显式配置
    的优先级高于供应商默认，不能被本次修复破坏。"""
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setenv("BACKEND_URL", "https://env-gateway.example/v1")
    path = Path("web/app.py")
    node = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_config"
    )
    from types import SimpleNamespace

    ns = {
        "DEFAULT_CONFIG": {**DEFAULT_CONFIG, "backend_url": None},
        "os": __import__("os"),
        "st": SimpleNamespace(session_state={}),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    ns["st"].session_state = {"llm_provider": "deepseek", "llm_base_url": ""}
    config = ns["_build_config"]()
    assert config["backend_url"] == "https://env-gateway.example/v1"
