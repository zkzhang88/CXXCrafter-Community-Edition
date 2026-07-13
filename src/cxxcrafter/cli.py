import os
import logging
from datetime import datetime
from cxxcrafter.log_utils import setup_logging, log_the_dockerfile, log_the_error_message
from cxxcrafter.generation_module import DockerfileGenerator, DockerfileModifier
from cxxcrafter.utils import save_successful_dockerfile
from cxxcrafter.parsing_module import parser
from cxxcrafter.execution_module import executor
from cxxcrafter.init import get_log_dir, get_playground_dir, get_solution_base_dir
from cxxcrafter.llm.bot import get_sdk_token_counts
from cxxcrafter.config import MAX_RETRY_TIMES, SEARCH_QUERY_COUNT
from cxxcrafter.search_module import (
    SearchClient,
    SearchContext,
    build_repair_search_queries,
    build_search_context,
    format_search_results,
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

        dockerfile_generator = DockerfileGenerator(
            self.project_name, self.project_path, 
            self.environment_requirement, self.potential_dependency, 
            self.docs, web_search_results, test_ready=self.test_ready)
        
        dockerfile_generator.generate_dockerfile()
        self.logger.info('Generation Module Finishes')

        # Create a directory to store the history
        self.history_dir = os.path.join(project_dir, f'history-{self.start_time}')
        os.makedirs(self.history_dir, exist_ok=True)
        log_the_dockerfile(self.dockerfile_path, self.flag_version, self.history_dir)
    
    
    def modify_dockerfile(self, error_message):
        self.logger.info('Modifier Module Starts')
        web_search_results = self._get_web_search_results(error_message)
        self.modifier.modify_dockerfile(self.dockerfile_path, error_message, web_search_results)
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
                self.logger.info(f"{self.project_name} is good!")
                return self.project_name, flag_success
    
