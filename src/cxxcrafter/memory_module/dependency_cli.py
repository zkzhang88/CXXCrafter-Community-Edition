import argparse
import json
import os

from cxxcrafter.memory_module.dependency_registry import (
    DEFAULT_DEPENDENCY_REGISTRY_PATH,
    DependencyRegistry,
)


def run_dependency_cli(argv=None):
    default_database, default_limit = _load_registry_settings()
    parser = argparse.ArgumentParser(
        prog="python -m cxxcrafter dependency",
        description="Query and manage the verified dependency solution registry.",
    )
    parser.add_argument(
        "--database",
        default=default_database,
        help="Override the dependency registry SQLite path.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Get verified solutions for a dependency.")
    get_parser.add_argument("name")
    get_parser.add_argument("--version", dest="dependency_version", default="")
    get_parser.add_argument("--os", dest="os_family", default="")
    get_parser.add_argument("--release", dest="os_release", default="")
    get_parser.add_argument("--limit", type=int, choices=range(1, 11), default=None)
    get_parser.add_argument("--json", action="store_true", dest="as_json")

    list_parser = subparsers.add_parser("list", help="List dependencies in the registry.")
    list_parser.add_argument("--query", default="")
    list_parser.add_argument(
        "--manager",
        choices=("apt", "apt-get", "curl", "wget", "source"),
        default=None,
    )
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    import_parser = subparsers.add_parser("import", help="Import registry JSON.")
    import_parser.add_argument("path")
    import_parser.add_argument("--dry-run", action="store_true")

    export_parser = subparsers.add_parser("export", help="Export registry JSON.")
    export_parser.add_argument("path")

    stats_parser = subparsers.add_parser("stats", help="Show registry statistics.")
    stats_parser.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)
    registry = DependencyRegistry(args.database, result_limit=default_limit)

    if args.command == "get":
        solutions = registry.get(
            args.name,
            dependency_version=args.dependency_version,
            os_family=args.os_family,
            os_release=args.os_release,
            limit=args.limit,
        )
        if args.as_json:
            print(json.dumps([item.to_dict() for item in solutions], ensure_ascii=False, indent=2))
        else:
            _print_solutions(args.name, solutions)
        return 0

    if args.command == "list":
        rows = registry.list(query=args.query, manager=args.manager)
        if args.as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("No dependencies found.")
        else:
            for row in rows:
                print(
                    f"{row['canonical_name']}: {row['solution_count']} solution(s), "
                    f"{row['verification_count']} verification(s)"
                )
        return 0

    if args.command == "import":
        report = registry.import_json(args.path, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        report = registry.export_json(args.path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "stats":
        report = registry.stats()
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for key, value in report.items():
                print(f"{key}: {value}")
        return 0

    return 2


def _load_registry_settings():
    config_path = os.path.expanduser(os.getenv(
        "CXXCRAFTER_CONFIG",
        "~/exps/CXXCrafter-Community-Edition/cxxcrafter.config.json",
    ))
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as source:
            config = json.load(source)
        if not isinstance(config, dict):
            raise ValueError("CXXCrafter config file must contain a JSON object.")
    database = (
        os.getenv("CXXCRAFTER_DEPENDENCY_REGISTRY_PATH")
        or config.get("dependency_registry_path")
        or DEFAULT_DEPENDENCY_REGISTRY_PATH
    )
    result_limit = int(
        os.getenv("CXXCRAFTER_DEPENDENCY_REGISTRY_RESULT_LIMIT")
        or config.get("dependency_registry_result_limit", 3)
    )
    if not 1 <= result_limit <= 10:
        raise ValueError("dependency_registry_result_limit must be between 1 and 10.")
    return os.path.expanduser(database), result_limit


def _print_solutions(name, solutions):
    if not solutions:
        print(f"No verified dependency solutions found for '{name}'.")
        return
    for index, solution in enumerate(solutions, start=1):
        environment = ":".join(filter(None, (solution.os_family, solution.os_release))) or "generic"
        print(f"{index}. {solution.dependency_name} via {solution.manager}")
        print(f"   environment: {environment} ({solution.environment_match})")
        print(f"   integrity: {solution.integrity_level}")
        print(
            f"   verifications: {solution.verification_count} "
            f"across {solution.verified_project_count} project(s)"
        )
        if solution.source_url:
            print(f"   source: {solution.source_url}")
        if solution.checksum:
            print(f"   checksum: {solution.checksum}")
        print("```dockerfile")
        print(solution.dockerfile_snippet)
        print("```")
