import argparse

from cxxcrafter.runner import build_one_repo, run_with_file_list


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CXXCrafter-Community Runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--repo', type=str, help='Specify the path of a single repo to build.')
    group.add_argument('--repo-list', type=str, help='Specify the path of a repo list file.')
    parser.add_argument(
        '--force-overwrite',
        action='store_true',
        help='Regenerate and overwrite an existing playground Dockerfile instead of resuming it.',
    )
    parser.add_argument(
        '--test-ready',
        action='store_true',
        help='Generate a Dockerfile that also preserves and builds local test targets/dependencies.',
    )
    parser.add_argument(
        '--search-query-count',
        type=int,
        choices=range(1, 6),
        default=None,
        metavar='{1,2,3,4,5}',
        help='Set the total number of web search queries per failed build.',
    )
    args = parser.parse_args()

    if args.repo:
        build_one_repo(
            args.repo,
            force_overwrite=args.force_overwrite,
            test_ready=args.test_ready,
            search_query_count=args.search_query_count,
        )
    elif args.repo_list:
        run_with_file_list(
            args.repo_list,
            force_overwrite=args.force_overwrite,
            test_ready=args.test_ready,
            search_query_count=args.search_query_count,
        )
