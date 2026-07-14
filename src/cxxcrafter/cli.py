import os
import logging
import re
from datetime import datetime
from cxxcrafter.audit import append_audit
from cxxcrafter.log_utils import setup_logging, log_the_dockerfile, log_the_error_message
from cxxcrafter.generation_module import DockerfileGenerator, DockerfileModifier
from cxxcrafter.utils import save_successful_dockerfile
from cxxcrafter.parsing_module import parser
from cxxcrafter.execution_module import executor
from cxxcrafter.init import get_log_dir, get_playground_dir, get_solution_base_dir
from cxxcrafter.llm.bot import get_sdk_token_counts
from cxxcrafter.config import (
    DEPENDENCY_REGISTRY_ENABLED,
    DEPENDENCY_REGISTRY_PATH,
    DEPENDENCY_REGISTRY_RESULT_LIMIT,
    MAX_RETRY_TIMES,
    SEARCH_QUERY_COUNT,
)
from cxxcrafter.memory_module.dependency_registry import (
    DependencyRegistry,
    format_dependency_solutions,
    query_dependency_solutions,
    read_dockerfile_environment,
)
from cxxcrafter.search_module import (
    SearchClient,
    SearchContext,
    build_repair_search_queries,
    build_search_context,
    extract_search_hints,
    format_search_results,
    get_repository_url,
)


class CXXCrafter:
    def __init__(
        self,
        project_path,
        force_overwrite=False,
        test_ready=False,
        search_query_count=None,
    ):
        self.project_path = os.path.abspath(os.path.normpath(project_path))
        self.project_name = os.path.basename(self.project_path)
        self.logger = logging.getLogger(__name__)
        self.force_overwrite = force_overwrite
        self.test_ready = test_ready
        self.search_query_count = (
            SEARCH_QUERY_COUNT if search_query_count is None else int(search_query_count)
        )
        if not 1 <= self.search_query_count <= 5:
            raise ValueError("search_query_count must be between 1 and 5.")
        self.start_time = datetime.now().strftime('%Y%m%d_%H%M')
        self.dockerfile_path = os.path.join(get_playground_dir(), self.project_name, 'Dockerfile')
        self.log_file = f"{get_log_dir()}/{self.project_name}_{self.start_time}.log"
        self.history_dir = None
        self.flag_version = 1
        self.modifier = DockerfileModifier(test_ready=self.test_ready)

        setup_logging(self.log_file, self.project_name)
        self.logger.disabled = False
        self.dependency_registry = None
        if DEPENDENCY_REGISTRY_ENABLED:
            try:
                self.dependency_registry = DependencyRegistry(
                    DEPENDENCY_REGISTRY_PATH,
                    result_limit=DEPENDENCY_REGISTRY_RESULT_LIMIT,
                )
            except Exception as e:
                self.logger.warning(
                    "Dependency registry unavailable; continuing without it: %s",
                    e,
                )

    def __del__(self):
        self.logger.info(f"Building process of project <{self.project_name}> ended.\n"
                         f"Overall input tokens count: {get_sdk_token_counts()[0]}.\n"
                         f"Overall output tokens count: {get_sdk_token_counts()[1]}.")


    def parse_project(self):
        self.logger.info('Parsing Module Starts')
        (self.project_name, 
        self.project_path, 
        self.environment_requirement,
         self.build_system_name,
         self.entry_file,
        self.potential_dependency, 
        self.docs) = parser(self.project_path)
        self.logger.info('Parsing Module Finishes')

    def _get_web_search_results(self, error_message):
        try:
            search_client = SearchClient(logger=self.logger)
            self.logger.info(f"Web search enabled: {search_client.enabled}")
            if not search_client.enabled or not search_client.api_url:
                context = SearchContext(
                    project_name=self.project_name,
                    build_system_name=self.build_system_name,
                )
                search_client.search_many([], context)
                return ""

            context = build_search_context(
                self.project_name,
                self.project_path,
                self.build_system_name,
                error_message,
            )
            queries = build_repair_search_queries(
                context,
                self.search_query_count,
            )
            results = search_client.search_many(queries, context)
            return format_search_results(results)
        except Exception as e:
            self.logger.warning(f"Web search failed unexpectedly; continuing without search results: {e}")
            return ""

    def _get_initial_dependency_solutions(self):
        if not self.dependency_registry:
            return ""
        try:
            os_family, os_release = _infer_initial_environment(self.environment_requirement)
            results = query_dependency_solutions(
                self.dependency_registry,
                self.potential_dependency.items(),
                os_family=os_family,
                os_release=os_release,
            )
            return format_dependency_solutions(results)
        except Exception as e:
            self.logger.warning(
                "Dependency registry lookup failed during initial generation: %s",
                e,
            )
            return ""

    def _get_repair_dependency_solutions(self, error_message):
        if not self.dependency_registry:
            return ""
        try:
            hints = extract_search_hints(error_message, use_llm=False)
            names = []
            for group in (
                hints.headers,
                hints.libraries,
                hints.packages,
                hints.commands,
                hints.cmake_components,
            ):
                names.extend(group)
            names = list(dict.fromkeys(name for name in names if name))
            if not names:
                return ""
            os_family, os_release, _ = read_dockerfile_environment(self.dockerfile_path)
            results = query_dependency_solutions(
                self.dependency_registry,
                ((name, "") for name in names),
                os_family=os_family,
                os_release=os_release,
            )
            return format_dependency_solutions(results)
        except Exception as e:
            self.logger.warning(
                "Dependency registry lookup failed during repair: %s",
                e,
            )
            return ""

    def _ingest_successful_dependencies(self):
        if not self.dependency_registry:
            return
        try:
            self.dependency_registry.ingest_verified_dockerfile(
                self.dockerfile_path,
                self.project_name,
                project_repository=get_repository_url(self.project_path),
                history_dir=self.history_dir,
            )
        except Exception as e:
            self.logger.warning(
                "Successful build could not be added to dependency registry: %s",
                e,
            )
            try:
                append_audit("dependency_registry_ingest_failed", {
                    "project_name": self.project_name,
                    "dockerfile_path": self.dockerfile_path,
                    "error": str(e),
                })
            except Exception as audit_error:
                self.logger.warning(
                    "Dependency registry failure audit could not be written: %s",
                    audit_error,
                )

    def generate_dockerfile(self):
        self.logger.info('Generation Module Starts')
        project_dir = os.path.dirname(self.dockerfile_path)
        copied_project_dir = os.path.join(project_dir, self.project_name)

        if (
            not self.force_overwrite
            and os.path.exists(self.dockerfile_path)
            and os.path.isdir(copied_project_dir)
        ):
            self.logger.info('Existing Dockerfile and copied project found; resuming from playground')
            self.history_dir = os.path.join(project_dir, f'history-{self.start_time}')
            os.makedirs(self.history_dir, exist_ok=True)
            log_the_dockerfile(self.dockerfile_path, self.flag_version, self.history_dir)
            self.logger.info('Generation Module Finishes')
            return

        web_search_results = ""
        dependency_solutions = self._get_initial_dependency_solutions()

        dockerfile_generator = DockerfileGenerator(
            self.project_name, self.project_path, 
            self.environment_requirement, self.potential_dependency, 
            self.docs, web_search_results,
            test_ready=self.test_ready,
            dependency_solutions=dependency_solutions)
        
        dockerfile_generator.generate_dockerfile()
        self.logger.info('Generation Module Finishes')

        # Create a directory to store the history
        self.history_dir = os.path.join(project_dir, f'history-{self.start_time}')
        os.makedirs(self.history_dir, exist_ok=True)
        log_the_dockerfile(self.dockerfile_path, self.flag_version, self.history_dir)
    
    
    def modify_dockerfile(self, error_message):
        self.logger.info('Modifier Module Starts')
        web_search_results = self._get_web_search_results(error_message)
        dependency_solutions = self._get_repair_dependency_solutions(error_message)
        self.modifier.modify_dockerfile(
            self.dockerfile_path,
            error_message,
            web_search_results,
            dependency_solutions,
        )
        self.logger.info('Modifier Module Finishes')

        self.flag_version += 1
        log_the_dockerfile(self.dockerfile_path, self.flag_version, self.history_dir)


    def execute_dockerfile(self):
        self.logger.info('Execution Module Starts')
        flag, error = executor(
            os.path.dirname(self.dockerfile_path),
            build_system_name=self.build_system_name,
            test_ready=self.test_ready,
        )
        self.logger.info('Execution Module Finishes')
        return flag, error


    
    def run(self):
        self.parse_project()
        self.generate_dockerfile()
        while True:
            flag_success, error_message = self.execute_dockerfile()
            if not flag_success:
                self.logger.error(f"Execution failed with error: {error_message}")
                log_the_error_message(error_message, self.flag_version, self.history_dir)
                if self.flag_version >= MAX_RETRY_TIMES:
                    self.logger.info(f"\nTry over {MAX_RETRY_TIMES} times")
                    return self.project_name, flag_success
                self.modify_dockerfile(error_message)
            else:
                save_successful_dockerfile(self.dockerfile_path, self.project_name, get_solution_base_dir())
                self._ingest_successful_dependencies()
                self.logger.info(f"{self.project_name} is good!")
                return self.project_name, flag_success


def _infer_initial_environment(environment_requirement):
    text = str(environment_requirement or "")
    match = re.search(r"\bubuntu(?:\s*[:=]?\s*)(\d+\.\d+)", text, flags=re.IGNORECASE)
    return "ubuntu", match.group(1) if match else ""
    
