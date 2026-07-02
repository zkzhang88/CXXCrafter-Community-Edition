import logging, shutil, os
from .template.prompt_template import get_initial_prompt, prompt_template_for_modification
from .utils import save_dockerfile, resave_dockerfile, extract_dockerfile_content
from cxxcrafter.audit import append_audit
from cxxcrafter.llm.bot import GPTBot
from cxxcrafter.init import get_playground_dir


class DockerfileGenerator:
    def __init__(self, project_name, project_path, environment_requirement, dependency, docs, web_search_results=""):
        self.project_name = project_name
        self.project_path = project_path
        self.environment_requirement = environment_requirement
        self.dependency = dependency
        self.docs = docs
        self.web_search_results = web_search_results
        self.logger = logging.getLogger(__name__)
        self.logger.disabled = False

    def generate_system_prompt(self):
        self.logger.info('Generating system prompt...')
        return get_initial_prompt(
            self.project_name,
            self.project_path,
            self.environment_requirement,
            self.dependency,
            self.docs,
            self.web_search_results,
        )

    def perform_inference(self, system_prompt):
        self.logger.info('Performing inference...')
        append_audit("dockerfile_generation_llm_prompt", {
            "project_name": self.project_name,
            "stage": "initial_generation",
            "system_prompt": system_prompt,
            "user_prompt": "",
        })
        bot = GPTBot(system_prompt)
        return bot.inference()

    def extract_dockerfile(self, response):
        self.logger.info('Extracting Dockerfile content...')
        return extract_dockerfile_content(response)

    def check_dockerfile(self, dockerfile_content):
        prompt = """
        Please review the Dockerfile to ensure it meets the following requirements. If it doesn't, make the necessary modifications:
        1. Each install command should be executed individually.
        2. Avoid duplicating identical RUN commands.
        3. Follow proper Dockerfile syntax, such as placing comments and commands on separate lines. Comments should begin with a # and be on their own line.
        """
        append_audit("dockerfile_generation_llm_prompt", {
            "project_name": self.project_name,
            "stage": "dockerfile_syntax_review",
            "system_prompt": prompt,
            "user_prompt": dockerfile_content,
        })
        bot = GPTBot(prompt)
        review_response = bot.inference(dockerfile_content)
        try:
            return self.extract_dockerfile(review_response)
        except ValueError as e:
            self.logger.warning(
                "Dockerfile syntax review did not return Dockerfile content; keeping the generated Dockerfile."
            )
            append_audit("dockerfile_syntax_review_fallback", {
                "project_name": self.project_name,
                "error": str(e),
                "review_response": review_response,
                "kept_dockerfile": dockerfile_content,
            })
            return dockerfile_content
    
    def generate_dockerfile(self):
        self.logger.info('Starting Dockerfile generation process...')
        system_prompt = self.generate_system_prompt()
        response = self.perform_inference(system_prompt)
        dockerfile_content = self.extract_dockerfile(response)
        dockerfile_content = self.check_dockerfile(dockerfile_content)

        # Create dockerfile playground directory
        project_dir = os.path.join(get_playground_dir(), self.project_name)

        save_dockerfile(project_dir, dockerfile_content)
        self.logger.info('Starting Copying the Repo to Dockerfile_Playground')
        temp = os.path.join(project_dir, self.project_name)

        try:
            if not os.path.exists(temp):
                shutil.copytree(self.project_path, temp)
        except Exception as e:
            self.logger.error(
                f"Error copying the repo: {e}. Params: self.project_path: {self.project_path}; temp: {temp}")
            raise e

        self.logger.info('Finish Copying')
        self.logger.info('Finish generating the initial dockerfile')
    

class DockerfileModifier:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.info('Begin to modify the dockerfile')
        self.bot = GPTBot(prompt_template_for_modification)

    def generate_prompt(self, dockerfile_path, error_message, web_search_results=""):
        with open(dockerfile_path, "r") as f:
            dockerfile_content = f.read()
        prompt_parts = [
            "Current Dockerfile:",
            "```dockerfile",
            dockerfile_content,
            "```",
            "Error Message:",
            "```text",
            str(error_message),
            "```",
        ]
        if web_search_results:
            prompt_parts.extend([
                "Relevant Web Search Results:",
                web_search_results,
            ])
        return "\n".join(prompt_parts)
    
    def modify_dockerfile(self, dockerfile_path, error_message, web_search_results=""):
        """
        """
        dockerfile_content = self.generate_prompt(dockerfile_path, error_message, web_search_results)
        append_audit("dockerfile_repair_llm_prompt", {
            "dockerfile_path": dockerfile_path,
            "stage": "repair",
            "system_prompt": prompt_template_for_modification,
            "user_prompt": dockerfile_content,
        })
        response = self.bot.inference(dockerfile_content)
        if '```dockerfile' in response.lower():
            dockerfile_content = extract_dockerfile_content(response)
            resave_dockerfile(dockerfile_path, dockerfile_content)
