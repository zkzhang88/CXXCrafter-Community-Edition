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


from cxxcrafter.generation_module import DockerfileModifier, postprocess_test_ready_dockerfile  # noqa: E402
from cxxcrafter.generation_module.template.prompt_template import get_initial_prompt, get_modification_prompt  # noqa: E402


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

    def test_initial_prompt_includes_test_ready_requirements_only_when_enabled(self):
        default_prompt = get_initial_prompt(
            "demo",
            "/tmp/demo",
            "Build system's name is CMake.",
            {},
            "Use cmake to build.",
        )
        test_ready_prompt = get_initial_prompt(
            "demo",
            "/tmp/demo",
            "Build system's name is CMake.",
            {},
            "Use cmake to build.",
            test_ready=True,
        )

        self.assertNotIn("Test-ready requirements", default_prompt)
        self.assertIn("Test-ready requirements", test_ready_prompt)
        self.assertIn("Boost.Test", test_ready_prompt)
        self.assertIn("GTest/GMock", test_ready_prompt)

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

    def test_modifier_prompt_includes_test_ready_requirements_when_enabled(self):
        modifier = DockerfileModifier.__new__(DockerfileModifier)
        DockerfileModifier.__init__(modifier, test_ready=True)

        self.assertIn("Test-ready requirements", modifier.system_prompt)
        self.assertIn("Boost.Test", modifier.system_prompt)

    def test_test_ready_modification_prompt_does_not_disable_testing_modules(self):
        default_prompt = get_modification_prompt()
        test_ready_prompt = get_modification_prompt(test_ready=True)

        disabling_guidance = "testing module, are causing issues, they should be disabled"
        self.assertIn(disabling_guidance, default_prompt)
        self.assertNotIn(disabling_guidance, test_ready_prompt)

    def test_boost_source_build_adds_test_library_in_test_ready_postprocess(self):
        dockerfile = (
            "RUN wget https://archives.boost.io/release/1.65.1/source/boost_1_65_1.tar.gz && "
            "./bootstrap.sh --with-python=/usr/bin/python3 "
            "--with-libraries=system,program_options,thread,filesystem --prefix=/usr/local\n"
        )

        processed = postprocess_test_ready_dockerfile(dockerfile)

        self.assertIn("--with-libraries=system,program_options,thread,filesystem,test", processed)

    def test_boost_source_build_does_not_duplicate_test_library(self):
        dockerfile = "RUN cd /tmp/boost && ./bootstrap.sh --with-libraries=system,test,filesystem --prefix=/usr/local\n"

        processed = postprocess_test_ready_dockerfile(dockerfile)

        self.assertIn("--with-libraries=system,test,filesystem", processed)
        self.assertNotIn("test,test", processed)

    def test_boost_source_build_preserves_quoted_library_list(self):
        dockerfile = 'RUN cd /tmp/boost && ./bootstrap.sh --with-libraries="system,filesystem" --prefix=/usr/local\n'

        processed = postprocess_test_ready_dockerfile(dockerfile)

        self.assertIn('--with-libraries="system,filesystem,test"', processed)

    def test_non_boost_dockerfile_is_unchanged_by_test_ready_postprocess(self):
        dockerfile = "FROM ubuntu:22.04\nRUN cmake --build build\n"

        self.assertEqual(dockerfile, postprocess_test_ready_dockerfile(dockerfile))


if __name__ == "__main__":
    unittest.main()
