import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from cxxcrafter.audit import append_audit
from cxxcrafter.memory_module.dependency_parser import (
    ExtractedDependencySolution,
    detect_dockerfile_environment,
    normalize_dependency_name,
    parse_verified_dockerfile,
    sanitize_download_url,
)


SCHEMA_VERSION = 1
DEFAULT_DEPENDENCY_REGISTRY_PATH = "~/.cxxcrafter/dependency_registry.sqlite3"
ALLOWED_MANAGERS = {"apt", "apt-get", "curl", "wget", "source"}
ALLOWED_INTEGRITY_LEVELS = {
    "package_manager_verified",
    "checksum_verified",
    "pinned_build_verified",
    "mutable_build_verified",
}
INTEGRITY_RANK = {
    "package_manager_verified": 4,
    "checksum_verified": 3,
    "pinned_build_verified": 2,
    "mutable_build_verified": 1,
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DependencySolution:
    dependency_name: str
    matched_name: str
    manager: str
    dependency_version: str
    package_name: str
    package_version: str
    os_family: str
    os_release: str
    base_image: str
    dockerfile_snippet: str
    source_url: str
    checksum: str
    integrity_level: str
    transport: str
    coinstalled_packages: tuple[str, ...]
    verification_count: int
    verified_project_count: int
    last_verified_at: str
    environment_match: str
    fingerprint: str

    @property
    def is_mutable(self):
        return self.integrity_level == "mutable_build_verified"

    def to_dict(self):
        payload = asdict(self)
        payload["is_mutable"] = self.is_mutable
        return payload


class DependencyRegistry:
    def __init__(self, db_path=None, result_limit=3):
        self.db_path = os.path.expanduser(db_path or DEFAULT_DEPENDENCY_REGISTRY_PATH)
        self.result_limit = int(result_limit)
        if not 1 <= self.result_limit <= 10:
            raise ValueError("Dependency registry result limit must be between 1 and 10.")
        db_dir = os.path.dirname(self.db_path)
        if db_dir and self.db_path != ":memory:":
            os.makedirs(db_dir, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise ValueError(
                    f"Unsupported dependency registry schema version {version}; "
                    f"expected {SCHEMA_VERSION}."
                )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS dependencies (
                    id INTEGER PRIMARY KEY,
                    canonical_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dependency_aliases (
                    id INTEGER PRIMARY KEY,
                    dependency_id INTEGER NOT NULL REFERENCES dependencies(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL COLLATE NOCASE,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(dependency_id, alias_normalized)
                );
                CREATE INDEX IF NOT EXISTS idx_dependency_alias_normalized
                    ON dependency_aliases(alias_normalized);

                CREATE TABLE IF NOT EXISTS solutions (
                    id INTEGER PRIMARY KEY,
                    dependency_id INTEGER NOT NULL REFERENCES dependencies(id) ON DELETE CASCADE,
                    manager TEXT NOT NULL,
                    dependency_version TEXT NOT NULL DEFAULT '',
                    package_name TEXT NOT NULL DEFAULT '',
                    package_version TEXT NOT NULL DEFAULT '',
                    os_family TEXT NOT NULL DEFAULT '',
                    os_release TEXT NOT NULL DEFAULT '',
                    base_image TEXT NOT NULL DEFAULT '',
                    dockerfile_snippet TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    checksum TEXT NOT NULL DEFAULT '',
                    integrity_level TEXT NOT NULL,
                    transport TEXT NOT NULL DEFAULT '',
                    coinstalled_packages TEXT NOT NULL DEFAULT '[]',
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_solutions_dependency_id
                    ON solutions(dependency_id);

                CREATE TABLE IF NOT EXISTS solution_evidence (
                    id INTEGER PRIMARY KEY,
                    solution_id INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
                    project_name TEXT NOT NULL DEFAULT '',
                    project_repository TEXT NOT NULL DEFAULT '',
                    dockerfile_sha256 TEXT NOT NULL DEFAULT '',
                    verification_source TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    UNIQUE(solution_id, project_name, dockerfile_sha256, verification_source)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_solution_id
                    ON solution_evidence(solution_id);
            """)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get(
        self,
        name,
        dependency_version=None,
        os_family=None,
        os_release=None,
        limit=None,
    ):
        normalized_name = normalize_dependency_name(name)
        if not normalized_name:
            return []
        limit = self.result_limit if limit is None else int(limit)
        if not 1 <= limit <= 10:
            raise ValueError("Dependency registry query limit must be between 1 and 10.")

        with self._connection() as connection:
            rows = connection.execute("""
                SELECT
                    d.canonical_name,
                    s.*,
                    COUNT(DISTINCT e.id) AS verification_count,
                    COUNT(DISTINCT NULLIF(e.project_name, '')) AS verified_project_count,
                    COALESCE(MAX(e.verified_at), s.updated_at) AS last_verified_at
                FROM dependencies d
                JOIN solutions s ON s.dependency_id = d.id
                LEFT JOIN dependency_aliases a ON a.dependency_id = d.id
                LEFT JOIN solution_evidence e ON e.solution_id = s.id
                WHERE d.canonical_name = ? OR a.alias_normalized = ?
                GROUP BY s.id
            """, (normalized_name, normalized_name)).fetchall()

        requested_family = normalize_dependency_name(os_family)
        requested_release = str(os_release or "").strip().lower()
        requested_version = _normalize_version(dependency_version)
        ranked = []
        for row in rows:
            solution_family = normalize_dependency_name(row["os_family"])
            if requested_family and solution_family and requested_family != solution_family:
                continue
            solution_version = _normalize_version(row["dependency_version"])
            if requested_version and solution_version and requested_version != solution_version:
                continue

            environment_rank, environment_match = _environment_rank(
                requested_family,
                requested_release,
                solution_family,
                str(row["os_release"] or "").lower(),
            )
            version_rank = 1 if not requested_version else (
                2 if solution_version == requested_version else 1
            )
            transport_rank = 2 if row["transport"] == "https" else (
                1 if row["transport"] in {"repository", ""} else 0
            )
            solution = DependencySolution(
                dependency_name=row["canonical_name"],
                matched_name=normalized_name,
                manager=row["manager"],
                dependency_version=row["dependency_version"],
                package_name=row["package_name"],
                package_version=row["package_version"],
                os_family=row["os_family"],
                os_release=row["os_release"],
                base_image=row["base_image"],
                dockerfile_snippet=row["dockerfile_snippet"],
                source_url=row["source_url"],
                checksum=row["checksum"],
                integrity_level=row["integrity_level"],
                transport=row["transport"],
                coinstalled_packages=tuple(json.loads(row["coinstalled_packages"] or "[]")),
                verification_count=int(row["verification_count"]),
                verified_project_count=int(row["verified_project_count"]),
                last_verified_at=row["last_verified_at"],
                environment_match=environment_match,
                fingerprint=row["fingerprint"],
            )
            ranked.append((
                version_rank,
                environment_rank,
                INTEGRITY_RANK.get(solution.integrity_level, 0),
                transport_rank,
                solution.verified_project_count,
                solution.verification_count,
                _timestamp_value(solution.last_verified_at),
                solution,
            ))

        ranked.sort(key=lambda item: (
            -item[0],
            -item[1],
            -item[2],
            -item[3],
            -item[4],
            -item[5],
            -item[6],
            item[7].fingerprint,
        ))
        return [item[-1] for item in ranked[:limit]]

    def ingest_verified_dockerfile(
        self,
        dockerfile_path,
        project_name,
        project_repository="",
        history_dir=None,
    ):
        with open(dockerfile_path, "r", encoding="utf-8") as dockerfile:
            dockerfile_content = dockerfile.read()
        solutions, diagnostics = parse_verified_dockerfile(
            dockerfile_content,
            include_diagnostics=True,
        )
        for diagnostic in diagnostics:
            _safe_append_audit("dependency_registry_extraction_skipped", diagnostic)
        dockerfile_sha256 = hashlib.sha256(dockerfile_content.encode("utf-8")).hexdigest()
        learned_aliases = self._learn_last_repair_aliases(history_dir, solutions)
        inserted_solutions = 0
        inserted_evidence = 0

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for solution in solutions:
                dependency_id, solution_id, created = self._upsert_solution(
                    connection,
                    solution,
                    alias_source="successful_build",
                )
                inserted_solutions += int(created)
                evidence_created = self._add_evidence(
                    connection,
                    solution_id,
                    project_name=project_name,
                    project_repository=project_repository,
                    dockerfile_sha256=dockerfile_sha256,
                    verification_source="successful_build",
                )
                inserted_evidence += int(evidence_created)
                for alias in learned_aliases.get(_solution_target_key(solution), ()):
                    self._upsert_alias(connection, dependency_id, alias, "repair_error")
            connection.commit()

        result = {
            "extracted_solutions": len(solutions),
            "inserted_solutions": inserted_solutions,
            "inserted_evidence": inserted_evidence,
            "learned_aliases": sum(len(items) for items in learned_aliases.values()),
        }
        _safe_append_audit("dependency_registry_ingest", {
            "project_name": project_name,
            "project_repository": project_repository,
            **result,
        })
        return result

    def _learn_last_repair_aliases(self, history_dir, final_solutions):
        if not history_dir or not os.path.isdir(history_dir):
            return {}
        versions = []
        for filename in os.listdir(history_dir):
            match = re.fullmatch(r"error_message-v(\d+)", filename)
            if match:
                versions.append(int(match.group(1)))
        if not versions:
            return {}
        version = max(versions)
        before_path = os.path.join(history_dir, f"Dockerfile-v{version}")
        error_path = os.path.join(history_dir, f"error_message-v{version}")
        if not os.path.exists(before_path) or not os.path.exists(error_path):
            return {}

        try:
            with open(before_path, "r", encoding="utf-8") as source:
                before_solutions = parse_verified_dockerfile(source.read())
            before_targets = {_solution_target_key(item) for item in before_solutions}
            new_targets = {
                _solution_target_key(item)
                for item in final_solutions
                if _solution_target_key(item) not in before_targets
            }
            if len(new_targets) != 1:
                return {}
            with open(error_path, "r", encoding="utf-8") as source:
                error_message = source.read()
            from cxxcrafter.search_module import extract_search_hints

            hints = extract_search_hints(error_message, use_llm=False)
            aliases = []
            for group in (
                hints.headers,
                hints.libraries,
                hints.packages,
                hints.commands,
                hints.cmake_components,
            ):
                aliases.extend(group)
            aliases = tuple(_deduplicate_aliases(aliases))
            return {next(iter(new_targets)): aliases} if aliases else {}
        except Exception as error:
            _safe_append_audit("dependency_registry_alias_learning_failed", {"error": str(error)})
            return {}

    def import_json(self, path, dry_run=False):
        with open(path, "r", encoding="utf-8") as source:
            payload = json.load(source)
        prepared = _validate_import_payload(payload)
        report = self._plan_import(prepared)
        report["dry_run"] = bool(dry_run)
        if dry_run:
            return report

        inserted_solutions = 0
        inserted_evidence = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for canonical_name, aliases, solutions in prepared:
                for solution, evidence_items in solutions:
                    dependency_id, solution_id, created = self._upsert_solution(
                        connection,
                        solution,
                        alias_source="manual_import",
                        canonical_name=canonical_name,
                        extra_aliases=aliases,
                    )
                    inserted_solutions += int(created)
                    if not evidence_items:
                        evidence_items = [{"verification_source": "manual_import"}]
                    for evidence in evidence_items:
                        inserted_evidence += int(self._add_evidence(
                            connection,
                            solution_id,
                            project_name=evidence.get("project_name", ""),
                            project_repository=evidence.get("project_repository", ""),
                            dockerfile_sha256=evidence.get("dockerfile_sha256", ""),
                            verification_source=evidence.get("verification_source", "manual_import"),
                            verified_at=evidence.get("verified_at"),
                        ))
            connection.commit()
        report.update({
            "inserted_solutions": inserted_solutions,
            "inserted_evidence": inserted_evidence,
        })
        _safe_append_audit("dependency_registry_import", report)
        return report

    def _plan_import(self, prepared):
        with self._connection() as connection:
            existing_dependencies = {
                row[0] for row in connection.execute(
                    "SELECT canonical_name FROM dependencies"
                ).fetchall()
            }
            existing_fingerprints = {
                row[0] for row in connection.execute(
                    "SELECT fingerprint FROM solutions"
                ).fetchall()
            }
            existing_aliases = {
                (row[0], row[1]) for row in connection.execute("""
                    SELECT d.canonical_name, a.alias_normalized
                    FROM dependency_aliases a
                    JOIN dependencies d ON d.id = a.dependency_id
                """).fetchall()
            }

        dependency_names = {item[0] for item in prepared}
        fingerprints = {
            solution.fingerprint()
            for _, _, solutions in prepared
            for solution, _ in solutions
        }
        aliases = set()
        for canonical_name, imported_aliases, solutions in prepared:
            aliases.add((canonical_name, canonical_name))
            for alias in imported_aliases:
                value = alias.get("alias", "") if isinstance(alias, dict) else alias
                normalized = normalize_dependency_name(value)
                if normalized:
                    aliases.add((canonical_name, normalized))
            for solution, _ in solutions:
                for alias in solution.aliases:
                    normalized = normalize_dependency_name(alias)
                    if normalized:
                        aliases.add((canonical_name, normalized))

        return {
            "dependencies": len(dependency_names),
            "solutions": len(fingerprints),
            "aliases": len(aliases),
            "new": {
                "dependencies": len(dependency_names - existing_dependencies),
                "solutions": len(fingerprints - existing_fingerprints),
                "aliases": len(aliases - existing_aliases),
            },
            "merged": {
                "dependencies": len(dependency_names & existing_dependencies),
                "solutions": len(fingerprints & existing_fingerprints),
                "aliases": len(aliases & existing_aliases),
            },
            "skipped": 0,
            "errors": 0,
        }

    def export_json(self, path):
        dependencies = []
        with self._connection() as connection:
            dependency_rows = connection.execute(
                "SELECT * FROM dependencies ORDER BY canonical_name"
            ).fetchall()
            for dependency in dependency_rows:
                aliases = connection.execute("""
                    SELECT alias, source FROM dependency_aliases
                    WHERE dependency_id = ? ORDER BY alias_normalized, source
                """, (dependency["id"],)).fetchall()
                solution_rows = connection.execute("""
                    SELECT * FROM solutions WHERE dependency_id = ? ORDER BY fingerprint
                """, (dependency["id"],)).fetchall()
                solutions = []
                for solution in solution_rows:
                    evidence = connection.execute("""
                        SELECT project_name, project_repository, dockerfile_sha256,
                               verification_source, verified_at
                        FROM solution_evidence
                        WHERE solution_id = ?
                        ORDER BY verified_at, project_name, dockerfile_sha256
                    """, (solution["id"],)).fetchall()
                    solutions.append({
                        "manager": solution["manager"],
                        "dependency_version": solution["dependency_version"],
                        "package_name": solution["package_name"],
                        "package_version": solution["package_version"],
                        "os_family": solution["os_family"],
                        "os_release": solution["os_release"],
                        "base_image": solution["base_image"],
                        "dockerfile_snippet": solution["dockerfile_snippet"],
                        "source_url": solution["source_url"],
                        "checksum": solution["checksum"],
                        "integrity_level": solution["integrity_level"],
                        "transport": solution["transport"],
                        "coinstalled_packages": json.loads(solution["coinstalled_packages"]),
                        "evidence": [dict(item) for item in evidence],
                    })
                dependencies.append({
                    "name": dependency["canonical_name"],
                    "aliases": [dict(item) for item in aliases],
                    "solutions": solutions,
                })

        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": _utc_now(),
            "dependencies": dependencies,
        }
        target = os.path.abspath(path)
        target_dir = os.path.dirname(target)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix="dependency-registry-", suffix=".json", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary_path, target)
        except Exception:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise
        return {"dependencies": len(dependencies), "path": target}

    def list(self, query=None, manager=None):
        clauses = []
        values = []
        if query:
            clauses.append("d.canonical_name LIKE ?")
            values.append(f"%{normalize_dependency_name(query)}%")
        if manager:
            clauses.append("s.manager = ?")
            values.append(manager)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(f"""
                SELECT d.canonical_name, COUNT(DISTINCT s.id) AS solution_count,
                       COUNT(e.id) AS verification_count
                FROM dependencies d
                JOIN solutions s ON s.dependency_id = d.id
                LEFT JOIN solution_evidence e ON e.solution_id = s.id
                {where}
                GROUP BY d.id
                ORDER BY d.canonical_name
            """, values).fetchall()
        return [dict(row) for row in rows]

    def stats(self):
        with self._connection() as connection:
            return {
                "dependencies": connection.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0],
                "aliases": connection.execute("SELECT COUNT(*) FROM dependency_aliases").fetchone()[0],
                "solutions": connection.execute("SELECT COUNT(*) FROM solutions").fetchone()[0],
                "evidence": connection.execute("SELECT COUNT(*) FROM solution_evidence").fetchone()[0],
                "database_path": self.db_path,
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }

    def _upsert_solution(
        self,
        connection,
        solution,
        alias_source,
        canonical_name=None,
        extra_aliases=(),
    ):
        now = _utc_now()
        canonical_name = normalize_dependency_name(canonical_name or solution.canonical_name)
        if not canonical_name:
            raise ValueError("Dependency solution is missing a canonical name.")
        row = connection.execute(
            "SELECT id FROM dependencies WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
        if row:
            dependency_id = row["id"]
            connection.execute(
                "UPDATE dependencies SET updated_at = ? WHERE id = ?",
                (now, dependency_id),
            )
        else:
            cursor = connection.execute("""
                INSERT INTO dependencies(canonical_name, created_at, updated_at)
                VALUES (?, ?, ?)
            """, (canonical_name, now, now))
            dependency_id = cursor.lastrowid

        for alias in (canonical_name,) + tuple(solution.aliases) + tuple(extra_aliases):
            if isinstance(alias, dict):
                alias_value = alias.get("alias", "")
                source = alias.get("source", alias_source)
            else:
                alias_value = alias
                source = alias_source
            self._upsert_alias(connection, dependency_id, alias_value, source)

        existing = connection.execute(
            "SELECT id FROM solutions WHERE fingerprint = ?",
            (solution.fingerprint(),),
        ).fetchone()
        if existing:
            solution_id = existing["id"]
            connection.execute(
                "UPDATE solutions SET updated_at = ? WHERE id = ?",
                (now, solution_id),
            )
            return dependency_id, solution_id, False

        cursor = connection.execute("""
            INSERT INTO solutions(
                dependency_id, manager, dependency_version, package_name,
                package_version, os_family, os_release, base_image,
                dockerfile_snippet, source_url, checksum, integrity_level,
                transport, coinstalled_packages, fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            dependency_id,
            solution.manager,
            solution.dependency_version,
            solution.package_name,
            solution.package_version,
            solution.os_family,
            solution.os_release,
            solution.base_image,
            solution.dockerfile_snippet,
            solution.source_url,
            solution.checksum,
            solution.integrity_level,
            solution.transport,
            json.dumps(list(solution.coinstalled_packages), ensure_ascii=False),
            solution.fingerprint(),
            now,
            now,
        ))
        return dependency_id, cursor.lastrowid, True

    def _upsert_alias(self, connection, dependency_id, alias, source):
        alias = str(alias or "").strip()
        normalized = normalize_dependency_name(alias)
        if not normalized:
            return False
        cursor = connection.execute("""
            INSERT OR IGNORE INTO dependency_aliases(
                dependency_id, alias, alias_normalized, source, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (dependency_id, alias, normalized, source or "unknown", _utc_now()))
        return cursor.rowcount > 0

    def _add_evidence(
        self,
        connection,
        solution_id,
        project_name,
        project_repository,
        dockerfile_sha256,
        verification_source,
        verified_at=None,
    ):
        cursor = connection.execute("""
            INSERT OR IGNORE INTO solution_evidence(
                solution_id, project_name, project_repository,
                dockerfile_sha256, verification_source, verified_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            solution_id,
            str(project_name or ""),
            str(project_repository or ""),
            str(dockerfile_sha256 or ""),
            str(verification_source or "manual_import"),
            str(verified_at or _utc_now()),
        ))
        return cursor.rowcount > 0


def format_dependency_solutions(solutions_by_query):
    sections = []
    seen = set()
    index = 0
    for query_name, solutions in solutions_by_query.items():
        for solution in solutions:
            if solution.fingerprint in seen:
                continue
            seen.add(solution.fingerprint)
            index += 1
            environment = ":".join(filter(None, (solution.os_family, solution.os_release))) or "generic"
            sections.extend([
                f"{index}. Dependency query: {query_name}",
                f"   Canonical dependency: {solution.dependency_name}",
                f"   Manager: {solution.manager}",
                f"   Environment: {environment} ({solution.environment_match})",
                f"   Integrity: {solution.integrity_level}",
                f"   Mutable: {'yes' if solution.is_mutable else 'no'}",
                f"   Verifications: {solution.verification_count} across "
                f"{solution.verified_project_count} project(s)",
            ])
            if solution.source_url:
                sections.append(f"   Source URL: {solution.source_url}")
            if solution.checksum:
                sections.append(f"   Checksum: {solution.checksum}")
            sections.extend([
                "   Dockerfile snippet:",
                "```dockerfile",
                solution.dockerfile_snippet,
                "```",
            ])
    if not sections:
        return ""
    return "Verified Dependency Solutions:\n" + "\n".join(sections)


def query_dependency_solutions(
    registry,
    dependencies,
    os_family="",
    os_release="",
    limit=None,
):
    results = {}
    for name, version in dependencies:
        solutions = registry.get(
            name,
            dependency_version=version,
            os_family=os_family,
            os_release=os_release,
            limit=limit,
        )
        if solutions:
            results[name] = solutions
    return results


def read_dockerfile_environment(dockerfile_path):
    try:
        with open(dockerfile_path, "r", encoding="utf-8") as source:
            return detect_dockerfile_environment(source.read())
    except OSError:
        return "", "", ""


def _validate_import_payload(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Dependency registry JSON must use schema_version {SCHEMA_VERSION}.")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("Dependency registry JSON 'dependencies' must be a list.")
    prepared = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("Each dependency entry must be an object.")
        canonical_name = normalize_dependency_name(dependency.get("name"))
        if not canonical_name:
            raise ValueError("Each dependency entry requires a valid name.")
        aliases = dependency.get("aliases", [])
        if not isinstance(aliases, list):
            raise ValueError(f"Aliases for '{canonical_name}' must be a list.")
        solutions_payload = dependency.get("solutions", [])
        if not isinstance(solutions_payload, list):
            raise ValueError(f"Solutions for '{canonical_name}' must be a list.")
        solutions = []
        for solution_payload in solutions_payload:
            if not isinstance(solution_payload, dict):
                raise ValueError(f"Solution for '{canonical_name}' must be an object.")
            manager = str(solution_payload.get("manager", "")).lower()
            if manager not in ALLOWED_MANAGERS:
                raise ValueError(f"Unsupported dependency manager '{manager}'.")
            snippet = str(solution_payload.get("dockerfile_snippet", ""))
            if not snippet or len(snippet) > 50000:
                raise ValueError(f"Solution for '{canonical_name}' requires a snippet up to 50000 chars.")
            _validate_imported_snippet_urls(snippet, canonical_name)
            integrity = str(solution_payload.get("integrity_level", "mutable_build_verified"))
            if integrity not in ALLOWED_INTEGRITY_LEVELS:
                raise ValueError(f"Unsupported integrity level '{integrity}'.")
            coinstalled = solution_payload.get("coinstalled_packages", [])
            if not isinstance(coinstalled, list) or not all(isinstance(item, str) for item in coinstalled):
                raise ValueError("coinstalled_packages must be a list of strings.")
            source_url = str(solution_payload.get("source_url", ""))
            if source_url:
                sanitized_source_url = sanitize_download_url(source_url)
                if not sanitized_source_url:
                    raise ValueError(
                        f"Solution for '{canonical_name}' contains an unsafe source URL."
                    )
                source_url = sanitized_source_url
            extracted = ExtractedDependencySolution(
                canonical_name=canonical_name,
                aliases=tuple(
                    item.get("alias", "") if isinstance(item, dict) else str(item)
                    for item in aliases
                ),
                manager=manager,
                dependency_version=str(solution_payload.get("dependency_version", "")),
                package_name=str(solution_payload.get("package_name", "")),
                package_version=str(solution_payload.get("package_version", "")),
                os_family=normalize_dependency_name(solution_payload.get("os_family", "")),
                os_release=str(solution_payload.get("os_release", "")),
                base_image=str(solution_payload.get("base_image", "")),
                dockerfile_snippet=snippet,
                source_url=source_url,
                checksum=str(solution_payload.get("checksum", "")),
                integrity_level=integrity,
                transport=str(solution_payload.get("transport", "")),
                coinstalled_packages=tuple(coinstalled),
            )
            evidence = solution_payload.get("evidence", [])
            if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
                raise ValueError("Solution evidence must be a list of objects.")
            solutions.append((extracted, evidence))
        prepared.append((canonical_name, aliases, solutions))
    return prepared


def _solution_target_key(solution):
    target = solution.package_name if solution.manager in {"apt", "apt-get"} else solution.source_url
    return solution.manager, normalize_dependency_name(solution.canonical_name), target


def _validate_imported_snippet_urls(snippet, canonical_name):
    for candidate in re.findall(r"https?://[^\s'\"<>]+", snippet):
        candidate = candidate.rstrip(");,]")
        if not sanitize_download_url(candidate):
            raise ValueError(
                f"Solution for '{canonical_name}' contains an unsafe URL in its snippet."
            )


def _environment_rank(requested_family, requested_release, solution_family, solution_release):
    if requested_family:
        if not solution_family:
            return 1, "generic_fallback"
        if requested_family != solution_family:
            return -1, "incompatible"
        if requested_release and requested_release == solution_release:
            return 4, "exact"
        if not solution_release:
            return 3, "same_distribution_generic"
        if requested_release:
            return 2, "same_distribution_release_fallback"
        return 3, "same_distribution"
    if not solution_family:
        return 2, "generic"
    return 1, "environment_unspecified"


def _normalize_version(value):
    return str(value or "").strip().lower().lstrip("v")


def _timestamp_value(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _safe_append_audit(event, payload):
    try:
        append_audit(event, payload)
    except Exception as error:
        LOGGER.warning("Dependency registry audit write failed: %s", error)


def _deduplicate_aliases(values):
    result = []
    seen = set()
    for value in values:
        normalized = normalize_dependency_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(str(value).strip())
    return result
