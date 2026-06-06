# Repository Guidelines

## Project Structure & Module Organization

CXXCrafter is a Python package using a `src/` layout. Core code lives in `src/cxxcrafter/`: `cli.py` coordinates the build loop, `parsing_module/` extracts project build requirements, `generation_module/` creates and modifies Dockerfiles, `execution_module/` runs Docker builds, `llm/` wraps model access, and `memory_module/` stores reusable build knowledge. `script/` contains analysis and repository collection utilities. `docs/` holds papers, validation notes, and sample logs. Configuration examples live at `cxxcrafter.config.example.json`; runtime secrets should go in a local `cxxcrafter.config.json`, which must not be committed.

## Build, Test, and Development Commands

- `pip install .`: install the package from the repository.
- `python -m cxxcrafter --repo /path/to/project`: run CXXCrafter on one C/C++ repository.
- `python -m cxxcrafter --repo-list /path/to/repos.txt`: run batch builds from a repo list.
- `python -m compileall -q src/cxxcrafter`: perform a quick syntax check after Python edits.

Docker must be running before executing build workflows. Successful Dockerfiles are written under `~/.cxxcrafter/build_solution_base`; logs and intermediate Dockerfiles are under `~/.cxxcrafter/logs` and `~/.cxxcrafter/dockerfile_playground`.

## Coding Style & Naming Conventions

Use Python 3.9+ and follow the repository’s existing straightforward style: 4-space indentation, snake_case functions and variables, PascalCase classes, and small modules grouped by workflow stage. Prefer explicit imports from `cxxcrafter.*`. Keep comments brief and only where they clarify non-obvious behavior. No formatter is configured; keep diffs focused and avoid unrelated reformatting.

## Testing Guidelines

There is currently no dedicated test suite. For code changes, at minimum run `python -m compileall -q src/cxxcrafter`. For behavior changes, run a small representative build with `python -m cxxcrafter --repo ...` and inspect the generated log. New tests should be placed under `tests/` and named `test_*.py`; prefer small unit tests for config parsing, prompt processing, and Docker output handling.

## Commit & Pull Request Guidelines

Recent history uses short imperative or descriptive commit messages, sometimes in Chinese, such as `Update README.md` or `修改默认的配置文件读取位置为项目目录下`. Keep commits scoped to one change. Pull requests should describe the problem, summarize the implementation, list verification commands, and mention any config or Docker behavior changes. Do not include API keys, generated logs, or large build artifacts.

## Security & Configuration Tips

Use `CXXCRAFTER_CONFIG` to point to a private config file when needed. Environment variables such as `CXXCRAFTER_API_KEY`, `CXXCRAFTER_LLM_MODEL`, `CXXCRAFTER_BASE_URL`, `CXXCRAFTER_MP_POOL_SIZE`, and `CXXCRAFTER_MAX_RETRY_TIMES` override file settings.
