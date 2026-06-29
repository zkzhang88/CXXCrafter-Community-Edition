import os
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.environ["HOME"] = tempfile.gettempdir()
os.environ["CXXCRAFTER_LLM_MODEL"] = "gpt-test"
os.environ["CXXCRAFTER_API_KEY"] = "test-key"
os.environ["CXXCRAFTER_SEARCH_ENABLED"] = "false"


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        pass


class _DummyDockerAPIClient:
    def __init__(self, *args, **kwargs):
        pass


class _DummyEncoding:
    def encode(self, message):
        return list(str(message))


sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=_DummyOpenAI))
sys.modules.setdefault("docker", types.SimpleNamespace(APIClient=_DummyDockerAPIClient))
sys.modules.setdefault(
    "tiktoken",
    types.SimpleNamespace(get_encoding=lambda name: _DummyEncoding()),
)
_package = types.ModuleType("cxxcrafter")
_package.__path__ = [str(_ROOT / "src" / "cxxcrafter")]
sys.modules.setdefault("cxxcrafter", _package)


from cxxcrafter.search_module import (  # noqa: E402
    SearchClient,
    build_repair_search_query,
    format_search_results,
)


_NULL_LOGGER = logging.getLogger("cxxcrafter-test-search")
_NULL_LOGGER.disabled = True


class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body.encode("utf-8")


class SearchModuleTests(unittest.TestCase):
    def test_disabled_search_returns_empty_without_request(self):
        client = SearchClient(
            enabled=False,
            api_url="https://example.invalid/search",
            logger=_NULL_LOGGER,
        )
        with patch("cxxcrafter.search_module.urllib.request.urlopen") as urlopen:
            self.assertEqual(client.search("zlib cmake"), [])
            urlopen.assert_not_called()

    def test_successful_search_parses_content_and_snippet(self):
        body = """
        {
          "results": [
            {"title": "Build docs", "url": "https://example.com/build", "content": "Install cmake."},
            {"title": "Issue", "url": "https://example.com/issue", "snippet": "Install zlib1g-dev."}
          ]
        }
        """
        client = SearchClient(
            enabled=True,
            api_url="https://example.invalid/search",
            api_key="secret",
            max_results=2,
            logger=_NULL_LOGGER,
        )

        with patch(
            "cxxcrafter.search_module.urllib.request.urlopen",
            return_value=_FakeResponse(body),
        ) as urlopen:
            results = client.search("project build")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["content"], "Install cmake.")
        self.assertEqual(results[1]["content"], "Install zlib1g-dev.")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")

    def test_search_failure_degrades_to_empty_results(self):
        client = SearchClient(
            enabled=True,
            api_url="https://example.invalid/search",
            max_results=2,
            logger=_NULL_LOGGER,
        )

        with patch(
            "cxxcrafter.search_module.urllib.request.urlopen",
            return_value=_FakeResponse("not json"),
        ):
            self.assertEqual(client.search("project build"), [])

    def test_format_search_results_and_repair_query_extract_hint(self):
        formatted = format_search_results([
            {
                "title": "Missing zlib",
                "url": "https://example.com/zlib",
                "content": "Install zlib1g-dev on Ubuntu.",
            }
        ])
        self.assertIn("Web Search Results:", formatted)
        self.assertIn("Missing zlib", formatted)

        query = build_repair_search_query(
            "demo",
            "CMake",
            "fatal error: zlib.h: No such file or directory",
        )
        self.assertIn("demo CMake Docker build error", query)
        self.assertIn("zlib.h", query)


if __name__ == "__main__":
    unittest.main()
