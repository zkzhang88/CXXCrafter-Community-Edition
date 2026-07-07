import os
import multiprocessing as mp

from cxxcrafter import CXXCrafter
from cxxcrafter.config import MP_POOL_SIZE
from cxxcrafter.init import get_solution_base_dir


def build_one_repo(repo_path, force_overwrite=False, test_ready=False):
    cxxcrafter = CXXCrafter(repo_path, force_overwrite=force_overwrite, test_ready=test_ready)
    project_name, flag = cxxcrafter.run()


def run_with_file_list(file_path, force_overwrite=False, test_ready=False):
    with open(file_path, "r") as f:
        lines = f.readlines()
    repos = [os.path.abspath(os.path.normpath(line.strip())) for line in lines if line.strip()]
    built_repos = os.listdir(get_solution_base_dir())
    repos = [item for item in repos if os.path.basename(item) not in built_repos]
    pool_size = MP_POOL_SIZE if isinstance(MP_POOL_SIZE, int) else 10
    with mp.Pool(processes=pool_size) as pool:
        pool.starmap(build_one_repo, [(repo, force_overwrite, test_ready) for repo in reversed(repos)])
