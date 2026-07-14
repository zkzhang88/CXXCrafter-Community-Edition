from .dockerfile_template import dockerfile_template


TEST_READY_DOCKERFILE_REQUIREMENTS = """
        Test-ready requirements:
            - Generate a Dockerfile that can build local unit/integration test targets in addition to the main project.
            - Do not disable tests, unit-test modules, or test build targets merely to make the Dockerfile build.
            - Prefer installing or building missing test dependencies over turning tests off.
            - Account for common C/C++ test dependencies such as Boost.Test / libboost_unit_test_framework, GTest/GMock, CTest/CMake BUILD_TESTING, Autotools --enable-unit-tests, and Python test harness dependencies.
            - The Dockerfile does not need to run the full test suite during docker build; it only needs to make test binaries and test dependencies available.
"""



def get_initial_prompt(
    project_name,
    user_intention,
    environment_requirement,
    dependency,
    docs,
    web_search_results="",
    test_ready=False,
    dependency_solutions="",
):
    """
    Get the initial prompt.
    """
    if len(dependency) > 10:
        potential_dependency = {k: dependency[k] for k in list(dependency)[:10]}
    else:
        potential_dependency = dependency

    web_search_section = ""
    if web_search_results:
        web_search_section = f"""
            {web_search_results}
            Use the web search results only when they are relevant to this build.
            Do not invent links, package versions, or commands that are not supported by the project context or search results.
        """
    dependency_solution_section = ""
    if dependency_solutions:
        dependency_solution_section = f"""
            {dependency_solutions}
            Prefer an exact environment match. Treat mutable or environment-fallback solutions as candidates only.
            These snippets were observed in successful builds, but must still be checked against this project's requirements.
        """
    test_ready_section = TEST_READY_DOCKERFILE_REQUIREMENTS if test_ready else ""

    prompt_template = f"""
        Please generate dockerfile which build the project {project_name} from source code accordind to the dockerfile template {dockerfile_template}.
        The source code is located at {"./"+project_name}. Move it to the docker container temp directory and build.
        Requirements:
            1. Install commands must be executed one at a time.
            2. Avoid repeating identical RUN commands.
            3. Please adhere to Dockerfile syntax. For example, ensure that comments and commands are on separate lines. Comments should start with a # and be placed independently of commands.
        {user_intention}
        {test_ready_section}
        Some useful information:
            Environment requirement: {environment_requirement}
            Docs: {docs}
            Potential Dependencies (skip installation if useless): {potential_dependency}
            {dependency_solution_section}
            {web_search_section}
    """
    return prompt_template




prompt_template_for_modification = """
    Solve the Dockerfile build problem according to the provided current Dockerfile, error message, and optional web search results.
    
    Additionally, take note of the following items:
    1. If the error message indicates a network issue, do not make any modifications to the Dockerfile. 
    2. Please return a complete dockerfile rather than just providing advice.
    3. Try to keep the beginning of the Dockerfile unchanged and make minimal modifications towards the end of the file.
    4. In the dockerfile, commands must be executed one at a time.
    5. If some unnecessary modules, such as the testing module, are causing issues, they should be disabled through build options.
    6. If required packages, tools, or dependencies are missing, proceed with installing them rather than just verifying their presence.
    7. In case errors arise due to specific dependency versions, attempt to acquire and install the exact version of the software that is required.
    8. If a 404 error occurs while attempting to download a specific dependency version, verify the correctness of the download link and make any necessary corrections.
    9. Use web search results only when they are relevant. Do not invent links, package versions, or commands that are not supported by the current Dockerfile, error message, or search results.
    10. Prefer exact-environment verified dependency solutions when provided. Treat mutable or environment-fallback solutions as candidates, not as proof for the current environment.
    11. Return the complete Dockerfile in a fenced code block whose language is dockerfile.
    """


prompt_template_for_test_ready_modification = """
    Solve the Dockerfile build problem according to the provided current Dockerfile, error message, and optional web search results.

    Additionally, take note of the following items:
    1. If the error message indicates a network issue, do not make any modifications to the Dockerfile.
    2. Please return a complete dockerfile rather than just providing advice.
    3. Try to keep the beginning of the Dockerfile unchanged and make minimal modifications towards the end of the file.
    4. In the dockerfile, commands must be executed one at a time.
    5. If required packages, tools, or dependencies are missing, proceed with installing them rather than just verifying their presence.
    6. In case errors arise due to specific dependency versions, attempt to acquire and install the exact version of the software that is required.
    7. If a 404 error occurs while attempting to download a specific dependency version, verify the correctness of the download link and make any necessary corrections.
    8. Use web search results only when they are relevant. Do not invent links, package versions, or commands that are not supported by the current Dockerfile, error message, or search results.
    9. Prefer exact-environment verified dependency solutions when provided. Treat mutable or environment-fallback solutions as candidates, not as proof for the current environment.
    10. Return the complete Dockerfile in a fenced code block whose language is dockerfile.
    """


def get_modification_prompt(test_ready=False):
    if not test_ready:
        return prompt_template_for_modification
    return f"""
{prompt_template_for_test_ready_modification}

When test-ready mode is enabled, also follow these requirements:
{TEST_READY_DOCKERFILE_REQUIREMENTS}
    """
