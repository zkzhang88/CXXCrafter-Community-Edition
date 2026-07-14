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
    "search_enabled": False,
    "search_provider": "generic",
    "search_api_url": "",
    "search_api_key": "",
    "search_max_results": 5,
    "search_candidate_results": 15,
    "search_query_count": 3,
    "search_official_domains": [],
    "search_source_weights": {},
    "search_blocked_domains": [],
    "search_timeout_seconds": 10,
    "search_retry_times": 0,
    "dependency_registry_enabled": True,
    "dependency_registry_path": "",
    "dependency_registry_result_limit": 3,
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


def _list_value(name, env_name):
    value = os.getenv(env_name)
    if value is not None:
        return [item.strip() for item in value.split(",") if item.strip()]

    value = _config_value(name)
    if not isinstance(value, list):
        raise ValueError(f"'{name}' must be a JSON list.")
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_value(name, env_name):
    value = os.getenv(env_name)
    if value is not None:
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{env_name} must be a JSON object.") from e
    else:
        value = _config_value(name)

    if not isinstance(value, dict):
        raise ValueError(f"'{name}' must be a JSON object.")
    return dict(value)


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

SEARCH_ENABLED = _env_bool("CXXCRAFTER_SEARCH_ENABLED", bool(_config_value("search_enabled")))
SEARCH_PROVIDER = (
    _first_env("CXXCRAFTER_SEARCH_PROVIDER")
    or _config_value("search_provider")
    or "generic"
).strip().lower()
SEARCH_API_URL = _first_env("CXXCRAFTER_SEARCH_API_URL") or _config_value("search_api_url")
SEARCH_API_KEY = _first_env("CXXCRAFTER_SEARCH_API_KEY") or _config_value("search_api_key")
SEARCH_MAX_RESULTS = int(os.getenv("CXXCRAFTER_SEARCH_MAX_RESULTS", _config_value("search_max_results")))
SEARCH_CANDIDATE_RESULTS = int(os.getenv(
    "CXXCRAFTER_SEARCH_CANDIDATE_RESULTS",
    _config_value("search_candidate_results"),
))
SEARCH_QUERY_COUNT = int(os.getenv(
    "CXXCRAFTER_SEARCH_QUERY_COUNT",
    _config_value("search_query_count"),
))
SEARCH_OFFICIAL_DOMAINS = tuple(_list_value(
    "search_official_domains",
    "CXXCRAFTER_SEARCH_OFFICIAL_DOMAINS",
))
SEARCH_BLOCKED_DOMAINS = tuple(_list_value(
    "search_blocked_domains",
    "CXXCRAFTER_SEARCH_BLOCKED_DOMAINS",
))
SEARCH_SOURCE_WEIGHTS = _dict_value(
    "search_source_weights",
    "CXXCRAFTER_SEARCH_SOURCE_WEIGHTS",
)
SEARCH_TIMEOUT_SECONDS = int(os.getenv(
    "CXXCRAFTER_SEARCH_TIMEOUT_SECONDS",
    _config_value("search_timeout_seconds"),
))
SEARCH_RETRY_TIMES = int(os.getenv("CXXCRAFTER_SEARCH_RETRY_TIMES", _config_value("search_retry_times")))

DEPENDENCY_REGISTRY_ENABLED = _env_bool(
    "CXXCRAFTER_DEPENDENCY_REGISTRY_ENABLED",
    bool(_config_value("dependency_registry_enabled")),
)
DEPENDENCY_REGISTRY_PATH = os.path.expanduser(
    _first_env("CXXCRAFTER_DEPENDENCY_REGISTRY_PATH")
    or _config_value("dependency_registry_path")
    or "~/.cxxcrafter/dependency_registry.sqlite3"
)
DEPENDENCY_REGISTRY_RESULT_LIMIT = int(os.getenv(
    "CXXCRAFTER_DEPENDENCY_REGISTRY_RESULT_LIMIT",
    _config_value("dependency_registry_result_limit"),
))

if MAX_RETRY_TIMES < 1:
    raise ValueError("max_retry_times must be greater than 0.")
if not 1 <= SEARCH_QUERY_COUNT <= 5:
    raise ValueError("search_query_count must be between 1 and 5.")
if SEARCH_CANDIDATE_RESULTS < 1:
    raise ValueError("search_candidate_results must be greater than 0.")
if not 1 <= DEPENDENCY_REGISTRY_RESULT_LIMIT <= 10:
    raise ValueError("dependency_registry_result_limit must be between 1 and 10.")
for source_type, weight in SEARCH_SOURCE_WEIGHTS.items():
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise ValueError(f"Search source weight '{source_type}' must be numeric.")
    if not -20 <= weight <= 20:
        raise ValueError(f"Search source weight '{source_type}' must be between -20 and 20.")


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
