import contextlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
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

_package = types.ModuleType("cxxcrafter")
_package.__path__ = [str(_ROOT / "src" / "cxxcrafter")]
sys.modules.setdefault("cxxcrafter", _package)


from cxxcrafter.memory_module.dependency_cli import (  # noqa: E402
    _load_registry_settings,
    run_dependency_cli,
)
from cxxcrafter.memory_module.dependency_parser import (  # noqa: E402
    detect_dockerfile_environment,
    parse_verified_dockerfile,
)
from cxxcrafter.memory_module.dependency_registry import DependencyRegistry  # noqa: E402


class DependencyParserTests(unittest.TestCase):
    def test_extracts_apt_and_continuous_wget_install_block(self):
        dockerfile = """
        FROM ubuntu:22.04
        RUN apt-get update
        RUN apt-get install -y --no-install-recommends libssl-dev zlib1g-dev=1.2.11
        RUN wget -O boost.tar.gz https://archives.boost.io/release/1.65.1/source/boost_1_65_1.tar.gz
        RUN tar -xzf boost.tar.gz
        WORKDIR /boost_1_65_1
        RUN ./bootstrap.sh && ./b2 install
        RUN echo unrelated
        RUN make should-not-be-included
        """

        solutions = parse_verified_dockerfile(dockerfile)
        by_target = {
            (item.manager, item.package_name or item.canonical_name): item
            for item in solutions
        }
        ssl = by_target[("apt-get", "libssl-dev")]
        self.assertEqual(ssl.canonical_name, "ssl")
        self.assertIn("RUN apt-get update", ssl.dockerfile_snippet)
        self.assertEqual(ssl.coinstalled_packages, ("libssl-dev", "zlib1g-dev"))

        zlib = by_target[("apt-get", "zlib1g-dev")]
        self.assertEqual(zlib.package_version, "1.2.11")

        boost = by_target[("wget", "boost")]
        self.assertEqual(boost.dependency_version, "1.65.1")
        self.assertEqual(boost.integrity_level, "pinned_build_verified")
        self.assertIn("./b2 install", boost.dockerfile_snippet)
        self.assertNotIn("echo unrelated", boost.dockerfile_snippet)
        self.assertNotIn("should-not-be-included", boost.dockerfile_snippet)
        self.assertEqual(
            detect_dockerfile_environment(dockerfile),
            ("ubuntu", "22.04", "ubuntu:22.04"),
        )

    def test_download_integrity_sensitive_urls_and_stage_boundaries(self):
        checksum = "a" * 64
        dockerfile = f"""
        FROM ubuntu:24.04 AS builder
        RUN curl -o tool.tar.gz https://example.com/tool-latest.tar.gz
        RUN echo "{checksum}  tool.tar.gz" | sha256sum -c -
        RUN tar -xzf tool.tar.gz && make install
        FROM debian:bookworm
        RUN wget https://example.com/other-2.0.tar.gz?token=secret
        """

        solutions = parse_verified_dockerfile(dockerfile)
        self.assertEqual(len(solutions), 1)
        tool = solutions[0]
        self.assertEqual(tool.manager, "curl")
        self.assertEqual(tool.integrity_level, "checksum_verified")
        self.assertEqual(tool.checksum, f"sha256:{checksum}")
        self.assertNotIn("FROM debian", tool.dockerfile_snippet)

    def test_mutable_download_is_kept_without_checksum(self):
        dockerfile = """
        FROM ubuntu:22.04
        RUN curl -L https://github.com/acme/widget/archive/master.tar.gz -o widget.tar.gz
        RUN tar -xzf widget.tar.gz && cd widget-master && make install
        """
        solutions = parse_verified_dockerfile(dockerfile)
        self.assertEqual(len(solutions), 1)
        self.assertEqual(solutions[0].canonical_name, "widget")
        self.assertEqual(solutions[0].integrity_level, "mutable_build_verified")

    def test_shell_terminator_does_not_turn_cleanup_into_apt_packages(self):
        solutions = parse_verified_dockerfile(
            "FROM ubuntu:22.04\n"
            "RUN apt-get install -y zlib1g-dev; rm -rf /var/lib/apt/lists/*\n"
        )
        self.assertEqual(len(solutions), 1)
        self.assertEqual(solutions[0].package_name, "zlib1g-dev")

    def test_dynamic_apt_package_skips_the_whole_install_group(self):
        solutions, diagnostics = parse_verified_dockerfile(
            "FROM ubuntu:22.04\n"
            "ARG EXTRA_PACKAGE\n"
            "RUN apt-get install -y zlib1g-dev ${EXTRA_PACKAGE}\n",
            include_diagnostics=True,
        )
        self.assertEqual(solutions, [])
        self.assertEqual(diagnostics[0]["reason"], "apt_install_not_statically_parseable")

    def test_parser_reports_rejected_sensitive_download_without_leaking_query(self):
        solutions, diagnostics = parse_verified_dockerfile(
            "FROM ubuntu:22.04\n"
            "RUN wget 'https://example.com/lib.tar.gz?token=secret'\n",
            include_diagnostics=True,
        )
        self.assertEqual(solutions, [])
        self.assertEqual(diagnostics[0]["reason"], "unsafe_or_nonliteral_url")
        self.assertNotIn("secret", diagnostics[0]["url"])


class DependencyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "registry.sqlite3")
        self.registry = DependencyRegistry(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_dockerfile(self, name, content):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "w", encoding="utf-8") as output:
            output.write(content)
        return path

    def test_ingest_is_idempotent_and_environment_query_isolated(self):
        ubuntu_path = self._write_dockerfile(
            "Dockerfile.ubuntu",
            "FROM ubuntu:22.04\nRUN apt-get install -y libssl-dev\n",
        )
        debian_path = self._write_dockerfile(
            "Dockerfile.debian",
            "FROM debian:bookworm\nRUN apt-get install -y libssl-dev\n",
        )
        self.registry.ingest_verified_dockerfile(ubuntu_path, "project-a")
        self.registry.ingest_verified_dockerfile(ubuntu_path, "project-a")
        self.registry.ingest_verified_dockerfile(debian_path, "project-b")

        results = self.registry.get("libssl-dev", os_family="ubuntu", os_release="22.04")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].os_family, "ubuntu")
        self.assertEqual(results[0].environment_match, "exact")
        self.assertEqual(results[0].verification_count, 1)
        self.assertEqual(self.registry.stats()["solutions"], 2)

    def test_same_distribution_release_fallback(self):
        dockerfile_path = self._write_dockerfile(
            "Dockerfile",
            "FROM ubuntu:20.04\nRUN apt-get install -y libssl-dev\n",
        )
        self.registry.ingest_verified_dockerfile(dockerfile_path, "demo")
        result = self.registry.get("ssl", os_family="ubuntu", os_release="22.04")[0]
        self.assertEqual(result.environment_match, "same_distribution_release_fallback")

    def test_unique_last_repair_learns_error_alias(self):
        history_dir = os.path.join(self.temp_dir.name, "history")
        os.makedirs(history_dir)
        with open(os.path.join(history_dir, "Dockerfile-v1"), "w", encoding="utf-8") as output:
            output.write("FROM ubuntu:22.04\nRUN apt-get install -y cmake\n")
        with open(os.path.join(history_dir, "error_message-v1"), "w", encoding="utf-8") as output:
            output.write("fatal error: zlib.h: No such file or directory")
        final_path = self._write_dockerfile(
            "Dockerfile",
            "FROM ubuntu:22.04\nRUN apt-get install -y cmake\nRUN apt-get install -y zlib1g-dev\n",
        )

        report = self.registry.ingest_verified_dockerfile(
            final_path,
            "demo",
            history_dir=history_dir,
        )
        self.assertEqual(report["learned_aliases"], 1)
        results = self.registry.get("zlib.h", os_family="ubuntu", os_release="22.04")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].package_name, "zlib1g-dev")

    def test_multiple_new_dependencies_do_not_learn_error_alias(self):
        history_dir = os.path.join(self.temp_dir.name, "history")
        os.makedirs(history_dir)
        with open(os.path.join(history_dir, "Dockerfile-v1"), "w", encoding="utf-8") as output:
            output.write("FROM ubuntu:22.04\n")
        with open(os.path.join(history_dir, "error_message-v1"), "w", encoding="utf-8") as output:
            output.write("fatal error: zlib.h: No such file or directory")
        final_path = self._write_dockerfile(
            "Dockerfile",
            "FROM ubuntu:22.04\nRUN apt-get install -y zlib1g-dev libssl-dev\n",
        )
        report = self.registry.ingest_verified_dockerfile(final_path, "demo", history_dir=history_dir)
        self.assertEqual(report["learned_aliases"], 0)
        self.assertEqual(self.registry.get("zlib.h"), [])

    def test_json_dry_run_round_trip_and_manual_source_solution(self):
        import_path = os.path.join(self.temp_dir.name, "import.json")
        export_path = os.path.join(self.temp_dir.name, "export.json")
        payload = {
            "schema_version": 1,
            "dependencies": [{
                "name": "boost",
                "aliases": ["boostorg"],
                "solutions": [{
                    "manager": "source",
                    "dependency_version": "1.85.0",
                    "os_family": "ubuntu",
                    "os_release": "22.04",
                    "dockerfile_snippet": "RUN ./bootstrap.sh && ./b2 install",
                    "source_url": "https://github.com/boostorg/boost",
                    "integrity_level": "pinned_build_verified",
                    "transport": "https"
                }]
            }]
        }
        with open(import_path, "w", encoding="utf-8") as output:
            json.dump(payload, output)

        dry_run = self.registry.import_json(import_path, dry_run=True)
        self.assertEqual(dry_run["solutions"], 1)
        self.assertEqual(dry_run["new"]["solutions"], 1)
        self.assertEqual(dry_run["merged"]["solutions"], 0)
        self.assertEqual(dry_run["errors"], 0)
        self.assertEqual(self.registry.stats()["solutions"], 0)

        self.registry.import_json(import_path)
        result = self.registry.get(
            "boostorg",
            dependency_version="1.85.0",
            os_family="ubuntu",
            os_release="22.04",
        )[0]
        self.assertEqual(result.manager, "source")

        self.registry.export_json(export_path)
        second_db = os.path.join(self.temp_dir.name, "second.sqlite3")
        second_registry = DependencyRegistry(second_db)
        second_registry.import_json(export_path)
        self.assertEqual(second_registry.stats()["solutions"], 1)

    def test_version_environment_integrity_and_transport_ranking(self):
        import_path = os.path.join(self.temp_dir.name, "ranking.json")
        base = {
            "manager": "source",
            "dependency_version": "1.0",
            "os_family": "ubuntu",
            "os_release": "22.04",
            "dockerfile_snippet": "RUN make install",
            "integrity_level": "pinned_build_verified",
        }
        solutions = [
            {**base, "source_url": "http://example.com/lib-1.0.tar.gz", "transport": "http"},
            {**base, "source_url": "https://example.com/lib-1.0.tar.gz", "transport": "https"},
            {
                **base,
                "source_url": "https://example.com/lib-1.0-checked.tar.gz",
                "transport": "https",
                "checksum": f"sha256:{'a' * 64}",
                "integrity_level": "checksum_verified",
            },
        ]
        with open(import_path, "w", encoding="utf-8") as output:
            json.dump({
                "schema_version": 1,
                "dependencies": [{"name": "ranked-lib", "aliases": [], "solutions": solutions}],
            }, output)
        self.registry.import_json(import_path)

        results = self.registry.get(
            "ranked-lib",
            dependency_version="1.0",
            os_family="ubuntu",
            os_release="22.04",
        )
        self.assertEqual(results[0].integrity_level, "checksum_verified")
        self.assertEqual(results[1].transport, "https")
        self.assertEqual(results[2].transport, "http")
        self.assertEqual(self.registry.get("ranked"), [])

    def test_concurrent_evidence_writes(self):
        dockerfile_path = self._write_dockerfile(
            "Dockerfile",
            "FROM ubuntu:22.04\nRUN apt-get install -y libssl-dev\n",
        )

        def ingest(project_name):
            DependencyRegistry(self.db_path).ingest_verified_dockerfile(dockerfile_path, project_name)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(ingest, ("project-a", "project-b")))

        result = self.registry.get("ssl", os_family="ubuntu", os_release="22.04")[0]
        self.assertEqual(result.verification_count, 2)
        self.assertEqual(result.verified_project_count, 2)

    def test_dependency_cli_get_and_stats(self):
        dockerfile_path = self._write_dockerfile(
            "Dockerfile",
            "FROM ubuntu:22.04\nRUN apt-get install -y libssl-dev\n",
        )
        self.registry.ingest_verified_dockerfile(dockerfile_path, "demo")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = run_dependency_cli([
                "--database",
                self.db_path,
                "get",
                "ssl",
                "--os",
                "ubuntu",
                "--release",
                "22.04",
            ])
        self.assertEqual(exit_code, 0)
        self.assertIn("libssl-dev", output.getvalue())

    def test_dependency_cli_environment_overrides_file_settings(self):
        config_path = os.path.join(self.temp_dir.name, "config.json")
        with open(config_path, "w", encoding="utf-8") as output:
            json.dump({
                "dependency_registry_path": "/file/registry.sqlite3",
                "dependency_registry_result_limit": 2,
            }, output)
        environment_path = os.path.join(self.temp_dir.name, "environment.sqlite3")
        with patch.dict(os.environ, {
            "CXXCRAFTER_CONFIG": config_path,
            "CXXCRAFTER_DEPENDENCY_REGISTRY_PATH": environment_path,
            "CXXCRAFTER_DEPENDENCY_REGISTRY_RESULT_LIMIT": "4",
        }):
            database, limit = _load_registry_settings()
        self.assertEqual(database, environment_path)
        self.assertEqual(limit, 4)


if __name__ == "__main__":
    unittest.main()
