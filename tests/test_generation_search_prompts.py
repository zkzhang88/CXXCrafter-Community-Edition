import os
from pathlib import Path
import sys
import tempfile
import types
import unittest


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


from cxxcrafter.generation_module import DockerfileModifier  # noqa: E402
from cxxcrafter.generation_module.template.prompt_template import get_initial_prompt  # noqa: E402


class GenerationSearchPromptTests(unittest.TestCase):
    def test_initial_prompt_includes_web_search_results(self):
        prompt = get_initial_prompt(
            "demo",
            "/tmp/demo",
            "Build system's name is CMake.",
            {"zlib": "unknown"},
            "Use cmake to build.",
            "Web Search Results:\n1. Install zlib\n   URL: https://example.com/zlib",
        )

        self.assertIn("Web Search Results:", prompt)
        self.assertIn("Install zlib", prompt)
        self.assertIn("Use the web search results only when they are relevant", prompt)

    def test_modifier_prompt_includes_dockerfile_error_and_search_results(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as dockerfile:
            dockerfile.write("FROM ubuntu:22.04\nRUN cmake --build build\n")
            dockerfile_path = dockerfile.name

        try:
            modifier = DockerfileModifier.__new__(DockerfileModifier)
            prompt = modifier.generate_prompt(
                dockerfile_path,
                "fatal error: zlib.h: No such file or directory",
                "Web Search Results:\n1. Install zlib1g-dev",
            )
        finally:
            os.unlink(dockerfile_path)

        self.assertIn("Current Dockerfile:", prompt)
        self.assertIn("FROM ubuntu:22.04", prompt)
        self.assertIn("Error Message:", prompt)
        self.assertIn("fatal error: zlib.h", prompt)
        self.assertIn("Relevant Web Search Results:", prompt)
        self.assertIn("Install zlib1g-dev", prompt)


if __name__ == "__main__":
    unittest.main()
