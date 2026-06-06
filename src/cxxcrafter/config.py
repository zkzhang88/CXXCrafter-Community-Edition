import json
import os


DEFAULT_CONFIG_PATH = "~/exps/CXXCrafter-Community-Edition/cxxcrafter.config.json"
CONFIG_PATH = os.path.expanduser(os.getenv("CXXCRAFTER_CONFIG", DEFAULT_CONFIG_PATH))

CONFIG_DEFAULTS = {
    "llm_model": "",
    "api_key": "",
    "base_url": "",
    "deepseek_thinking_enabled": True,
    "deepseek_reasoning_effort": "high",
    "mp_pool_size": 10,
    "max_retry_times": 10,
}


def _load_file_config():
    if not os.path.exists(CONFIG_PATH):
        return {}

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not isinstance(config, dict):
        raise ValueError(f"CXXCrafter config file must contain a JSON object: {CONFIG_PATH}")

    return config


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _first_env(*names):
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return None


def _config_value(name):
    return FILE_CONFIG.get(name, CONFIG_DEFAULTS[name])


FILE_CONFIG = _load_file_config()

# Environment variables have the highest priority, followed by the external
# config file and then safe defaults.
CONFIG_LLM_MODEL = _first_env("CXXCRAFTER_LLM_MODEL", "LLM_MODEL") or _config_value("llm_model")
CONFIG_API_KEY = _first_env("CXXCRAFTER_API_KEY") or _config_value("api_key")
CONFIG_BASE_URL = _first_env("CXXCRAFTER_BASE_URL") or _config_value("base_url")
CONFIG_DEEPSEEK_THINKING_ENABLED = _env_bool(
    "DEEPSEEK_THINKING_ENABLED",
    bool(_config_value("deepseek_thinking_enabled")),
)
CONFIG_DEEPSEEK_REASONING_EFFORT = (
    os.getenv("DEEPSEEK_REASONING_EFFORT") or _config_value("deepseek_reasoning_effort")
)

MP_POOL_SIZE = int(os.getenv("CXXCRAFTER_MP_POOL_SIZE", _config_value("mp_pool_size")))
MAX_RETRY_TIMES = int(os.getenv("CXXCRAFTER_MAX_RETRY_TIMES", _config_value("max_retry_times")))

if MAX_RETRY_TIMES < 1:
    raise ValueError("max_retry_times must be greater than 0.")


# === Automatic Check ===

LLM_MODEL = CONFIG_LLM_MODEL

if not LLM_MODEL:
    raise ValueError(
        "LLM_MODEL is not configured. Set CXXCRAFTER_LLM_MODEL or configure "
        f"'llm_model' in {CONFIG_PATH}."
    )

if "gpt" in LLM_MODEL.lower():
    LLM_API_KEY = _first_env("OPENAI_API_KEY", "CXXCRAFTER_API_KEY") or CONFIG_API_KEY
    LLM_BASE_URL = (
        _first_env("OPENAI_BASE_URL", "CXXCRAFTER_BASE_URL")
        or CONFIG_BASE_URL
        or "https://api.openai.com/v1"
    )
    LLM_THINKING_ENABLED = None
    LLM_REASONING_EFFORT = None

elif "deepseek" in LLM_MODEL.lower():
    LLM_API_KEY = _first_env("DEEPSEEK_API_KEY", "CXXCRAFTER_API_KEY") or CONFIG_API_KEY
    LLM_BASE_URL = (
        _first_env("DEEPSEEK_BASE_URL", "CXXCRAFTER_BASE_URL")
        or CONFIG_BASE_URL
        or "https://api.deepseek.com/v1"
    )
    LLM_THINKING_ENABLED = CONFIG_DEEPSEEK_THINKING_ENABLED
    LLM_REASONING_EFFORT = CONFIG_DEEPSEEK_REASONING_EFFORT

elif "qwen" in LLM_MODEL.lower():
    LLM_API_KEY = _first_env("DASHSCOPE_API_KEY", "CXXCRAFTER_API_KEY") or CONFIG_API_KEY
    LLM_BASE_URL = (
        _first_env("DASHSCOPE_BASE_URL", "CXXCRAFTER_BASE_URL")
        or CONFIG_BASE_URL
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    LLM_THINKING_ENABLED = None
    LLM_REASONING_EFFORT = None

else:
    raise ValueError(
        f"Unknown model type for '{LLM_MODEL}'. CXXCrafter only supports "
        "OpenAI, Deepseek or Qwen model series so far."
    )

if not LLM_API_KEY:
    raise ValueError(
        f"API key for '{LLM_MODEL}' is not configured. Set the provider API key "
        f"environment variable or configure 'api_key' in {CONFIG_PATH}."
    )

if not LLM_BASE_URL:
    raise ValueError(
        f"Base URL for '{LLM_MODEL}' is not configured. Set the provider base URL "
        f"environment variable or configure 'base_url' in {CONFIG_PATH}."
    )

if LLM_REASONING_EFFORT and LLM_REASONING_EFFORT not in {"high", "max"}:
    raise ValueError("DEEPSEEK_REASONING_EFFORT must be 'high' or 'max'.")
