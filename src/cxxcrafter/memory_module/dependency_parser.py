import hashlib
import json
import re
import shlex
import urllib.parse
from dataclasses import asdict, dataclass


ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
    ".gz",
    ".bz2",
    ".xz",
)

SENSITIVE_QUERY_PARTS = (
    "access_key",
    "api_key",
    "auth",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
    "x-amz-signature",
)

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}

SHELL_OPERATORS = {"&&", "||", ";", "|", "&"}


@dataclass(frozen=True)
class DockerInstruction:
    keyword: str
    value: str
    raw: str
    stage: int
    base_image: str = ""
    os_family: str = ""
    os_release: str = ""


@dataclass(frozen=True)
class ExtractedDependencySolution:
    canonical_name: str
    aliases: tuple[str, ...]
    manager: str
    dependency_version: str = ""
    package_name: str = ""
    package_version: str = ""
    os_family: str = ""
    os_release: str = ""
    base_image: str = ""
    dockerfile_snippet: str = ""
    source_url: str = ""
    checksum: str = ""
    integrity_level: str = "mutable_build_verified"
    transport: str = ""
    coinstalled_packages: tuple[str, ...] = ()

    def fingerprint(self):
        payload = {
            "canonical_name": normalize_dependency_name(self.canonical_name),
            "manager": self.manager,
            "dependency_version": self.dependency_version,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "os_family": self.os_family,
            "os_release": self.os_release,
            "base_image": self.base_image,
            "dockerfile_snippet": self.dockerfile_snippet,
            "source_url": self.source_url,
            "checksum": self.checksum,
            "integrity_level": self.integrity_level,
            "transport": self.transport,
            "coinstalled_packages": list(self.coinstalled_packages),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self):
        return asdict(self)


def parse_verified_dockerfile(dockerfile_content, include_diagnostics=False):
    instructions = parse_dockerfile_instructions(dockerfile_content)
    diagnostics = []
    solutions = []
    solutions.extend(_extract_apt_solutions(instructions, diagnostics))
    solutions.extend(_extract_download_solutions(instructions, diagnostics))

    unique = {}
    for solution in solutions:
        unique.setdefault(solution.fingerprint(), solution)
    extracted = list(unique.values())
    if include_diagnostics:
        return extracted, diagnostics
    return extracted


def parse_dockerfile_instructions(dockerfile_content):
    logical_lines = []
    buffer = []
    for line in str(dockerfile_content or "").splitlines():
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        buffer.append(line.rstrip())
        if _has_line_continuation(line):
            continue
        logical_lines.append("\n".join(buffer))
        buffer = []
    if buffer:
        logical_lines.append("\n".join(buffer))

    instructions = []
    stage = -1
    base_image = ""
    os_family = ""
    os_release = ""
    for raw in logical_lines:
        logical = re.sub(r"\\\s*\n", " ", raw).strip()
        match = re.match(r"^([A-Za-z]+)\s+(.*)$", logical, flags=re.DOTALL)
        if not match:
            continue
        keyword = match.group(1).upper()
        value = " ".join(match.group(2).split())
        if keyword == "FROM":
            stage += 1
            base_image, os_family, os_release = parse_base_image(value)
        elif stage < 0:
            stage = 0
        instructions.append(DockerInstruction(
            keyword=keyword,
            value=value,
            raw=raw.strip(),
            stage=stage,
            base_image=base_image,
            os_family=os_family,
            os_release=os_release,
        ))
    return instructions


def parse_base_image(from_value):
    tokens = str(from_value or "").split()
    image = next((token for token in tokens if not token.startswith("--")), "")
    if not image or "$" in image:
        return image, "", ""
    image_without_digest = image.split("@", 1)[0]
    last_component = image_without_digest.rsplit("/", 1)[-1]
    if ":" in last_component:
        name, release = last_component.split(":", 1)
    else:
        name, release = last_component, "latest"
    name = name.lower()
    family = name if name in {"ubuntu", "debian"} else ""
    return image, family, release if family else ""


def detect_dockerfile_environment(dockerfile_content):
    for instruction in parse_dockerfile_instructions(dockerfile_content):
        if instruction.keyword == "FROM" and instruction.os_family:
            return instruction.os_family, instruction.os_release, instruction.base_image
    return "", "", ""


def normalize_dependency_name(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9_.+\-]", "", text)
    return text.strip("-_.")


def _extract_apt_solutions(instructions, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else []
    solutions = []
    for index, instruction in enumerate(instructions):
        if instruction.keyword != "RUN":
            continue
        has_apt_install = bool(re.search(r"\bapt(?:-get)?\s+(?:[^;&|]+\s+)?install\b", instruction.value))
        if has_apt_install and instruction.os_family not in {"ubuntu", "debian"}:
            diagnostics.append({
                "reason": "unsupported_or_dynamic_base_image",
                "manager": "apt",
                "base_image": instruction.base_image,
            })
            continue
        if instruction.os_family not in {"ubuntu", "debian"}:
            continue
        package_groups = _parse_apt_install_groups(instruction.value)
        if has_apt_install and not any(packages for _, packages in package_groups):
            diagnostics.append({
                "reason": "apt_install_not_statically_parseable",
                "manager": "apt",
                "base_image": instruction.base_image,
            })
        for manager, packages in package_groups:
            if not packages:
                continue
            package_names = tuple(package for package, _ in packages)
            snippet_parts = []
            if index > 0:
                previous = instructions[index - 1]
                if (
                    previous.stage == instruction.stage
                    and previous.keyword == "RUN"
                    and _is_apt_update(previous.value)
                ):
                    snippet_parts.append(previous.raw)
            snippet_parts.append(instruction.raw)
            snippet = "\n".join(snippet_parts)

            for package_name, package_version in packages:
                aliases = _package_aliases(package_name)
                canonical_name = _canonical_package_name(package_name)
                if not canonical_name:
                    continue
                solutions.append(ExtractedDependencySolution(
                    canonical_name=canonical_name,
                    aliases=aliases,
                    manager=manager,
                    package_name=package_name,
                    package_version=package_version,
                    os_family=instruction.os_family,
                    os_release=instruction.os_release,
                    base_image=instruction.base_image,
                    dockerfile_snippet=snippet,
                    integrity_level="package_manager_verified",
                    transport="repository",
                    coinstalled_packages=package_names,
                ))
    return solutions


def _parse_apt_install_groups(command):
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    groups = []
    index = 0
    while index < len(tokens):
        tool = tokens[index].rsplit("/", 1)[-1].lower()
        if tool not in {"apt", "apt-get"}:
            index += 1
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].startswith("-"):
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor].lower() != "install":
            index += 1
            continue
        cursor += 1
        packages = []
        dynamic_package = False
        skip_next = False
        while cursor < len(tokens):
            token = tokens[cursor]
            clean_token = token.rstrip(";&|")
            ends_command = clean_token != token
            if token in SHELL_OPERATORS or not clean_token:
                break
            if skip_next:
                skip_next = False
                cursor += 1
                continue
            if clean_token in {"-o", "--option", "-t", "--target-release"}:
                skip_next = True
                cursor += 1
                continue
            if clean_token.startswith("-"):
                cursor += 1
                continue
            if "$" in clean_token or clean_token.startswith("/"):
                dynamic_package = dynamic_package or "$" in clean_token
                cursor += 1
                continue
            if "=" in clean_token:
                package_name, package_version = clean_token.split("=", 1)
            else:
                package_name, package_version = clean_token, ""
            package_name = package_name.split(":", 1)[0]
            package_name = normalize_dependency_name(package_name)
            if package_name:
                packages.append((package_name, package_version))
            cursor += 1
            if ends_command:
                break
        groups.append((tool, [] if dynamic_package else packages))
        index = max(index + 1, cursor)
    return groups


def _extract_download_solutions(instructions, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else []
    solutions = []
    for index, instruction in enumerate(instructions):
        if instruction.keyword != "RUN":
            continue
        downloads = _parse_download_commands(instruction.value)
        for download in downloads:
            source_url = sanitize_download_url(download["url"])
            if not source_url:
                diagnostics.append({
                    "reason": "unsafe_or_nonliteral_url",
                    "manager": download["manager"],
                    "url": _redact_url(download["url"]),
                })
                continue
            name_info = _infer_download_name(source_url, download.get("output", ""))
            if not name_info[0]:
                diagnostics.append({
                    "reason": "dependency_name_not_inferred",
                    "manager": download["manager"],
                    "url": source_url,
                })
                continue
            canonical_name, aliases, version, tracked_names = name_info
            block = _collect_download_block(instructions, index, tracked_names)
            snippet = "\n".join(item.raw for item in block)
            checksum = _extract_checksum(snippet)
            integrity = _download_integrity(source_url, version, checksum)
            parsed_url = urllib.parse.urlsplit(source_url)
            solutions.append(ExtractedDependencySolution(
                canonical_name=canonical_name,
                aliases=aliases,
                manager=download["manager"],
                dependency_version=version,
                os_family=instruction.os_family,
                os_release=instruction.os_release,
                base_image=instruction.base_image,
                dockerfile_snippet=snippet,
                source_url=source_url,
                checksum=checksum,
                integrity_level=integrity,
                transport=parsed_url.scheme.lower(),
            ))
    return solutions


def _parse_download_commands(command):
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    downloads = []
    index = 0
    while index < len(tokens):
        manager = tokens[index].rsplit("/", 1)[-1].lower()
        if manager not in {"curl", "wget"}:
            index += 1
            continue
        cursor = index + 1
        output = ""
        urls = []
        while cursor < len(tokens) and tokens[cursor] not in SHELL_OPERATORS:
            token = tokens[cursor]
            clean_token = token.rstrip(";&|")
            ends_command = clean_token != token
            if clean_token in {"-o", "--output", "-O", "--output-document"}:
                if cursor + 1 < len(tokens):
                    output = tokens[cursor + 1]
                    cursor += 2
                    continue
            if clean_token.startswith("--output=") or clean_token.startswith("--output-document="):
                output = clean_token.split("=", 1)[1]
            elif manager == "curl" and clean_token.startswith("-o") and len(clean_token) > 2:
                output = clean_token[2:]
            elif manager == "wget" and clean_token.startswith("-O") and len(clean_token) > 2:
                output = clean_token[2:]
            elif clean_token.startswith(("http://", "https://")):
                urls.append(clean_token)
            cursor += 1
            if ends_command:
                break
        for url in urls:
            downloads.append({"manager": manager, "url": url, "output": output})
        index = max(index + 1, cursor)
    return downloads


def _collect_download_block(instructions, start_index, tracked_names):
    block = [instructions[start_index]]
    tracked = {item.lower() for item in tracked_names if len(item) >= 2}
    stage = instructions[start_index].stage
    for instruction in instructions[start_index + 1:]:
        if instruction.stage != stage or instruction.keyword == "FROM":
            break
        if instruction.keyword in {"COPY", "ADD"}:
            break
        lowered = instruction.value.lower()
        if instruction.keyword == "RUN" and _parse_download_commands(instruction.value):
            break
        references_artifact = any(name in lowered for name in tracked)
        if instruction.keyword == "WORKDIR" and references_artifact:
            block.append(instruction)
            tracked.add(instruction.value.rsplit("/", 1)[-1].lower())
            continue
        if instruction.keyword != "RUN":
            if references_artifact:
                block.append(instruction)
                continue
            break
        if _parse_apt_install_groups(instruction.value):
            break
        lifecycle_command = bool(re.search(
            r"(?:^|\s|&&|;)(?:tar|unzip|cmake|meson|ninja|make|\./configure|"
            r"[^\s]*/configure|[^\s]*/bootstrap(?:\.sh)?|[^\s]*/b2|install|rm)\b",
            lowered,
        ))
        if not references_artifact and not lifecycle_command:
            break
        block.append(instruction)
        for token in re.findall(r"[A-Za-z0-9_.+\-]{3,}", instruction.value):
            if any(name in token.lower() for name in tracked):
                tracked.add(token.lower())
    return block


def sanitize_download_url(url):
    value = str(url or "").strip()
    if not value or "$" in value or "{" in value or "}" in value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    query_items = []
    for key, item_value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_QUERY_PARTS):
            return ""
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, item_value))
    host = parsed.hostname.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((
        parsed.scheme.lower(),
        host,
        parsed.path,
        urllib.parse.urlencode(query_items),
        "",
    ))


def _redact_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "<invalid-url>"
    hostname = parsed.hostname or ""
    return urllib.parse.urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def _infer_download_name(source_url, output):
    parsed = urllib.parse.urlsplit(source_url)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    repository = ""
    if parsed.hostname in {"github.com", "www.github.com"} and len(parts) >= 2:
        repository = parts[1].removesuffix(".git")
    elif parsed.hostname and "gitlab" in parsed.hostname and len(parts) >= 2:
        marker = parts.index("-") if "-" in parts else len(parts)
        if marker >= 2:
            repository = parts[marker - 1].removesuffix(".git")

    url_filename = parts[-1] if parts else ""
    output_filename = output.rsplit("/", 1)[-1] if output else ""
    archive_stem = _strip_archive_suffix(url_filename)
    output_stem = _strip_archive_suffix(output_filename)
    candidate = repository or archive_stem or output_stem or url_filename
    version = _extract_version_or_commit(source_url)
    canonical = _remove_version_suffix(candidate)
    canonical = normalize_dependency_name(canonical)
    if not canonical:
        return "", (), "", ()

    aliases = _unique_strings((
        canonical,
        repository,
        archive_stem,
        output_stem,
        url_filename,
        output_filename,
    ))
    tracked = _unique_strings((
        canonical,
        repository,
        archive_stem,
        output_stem,
        url_filename,
        output_filename,
    ))
    return canonical, aliases, version, tracked


def _extract_version_or_commit(value):
    commits = re.findall(r"(?<![0-9a-f])([0-9a-f]{7,40})(?![0-9a-f])", value, flags=re.IGNORECASE)
    if commits:
        return commits[-1]
    versions = re.findall(r"(?<![A-Za-z0-9])v?(\d+(?:[._-]\d+){1,5})(?![A-Za-z0-9])", value)
    if versions:
        return versions[-1].replace("_", ".")
    return ""


def _extract_checksum(snippet):
    sha512 = re.search(r"\b([0-9a-fA-F]{128})\b", snippet)
    if sha512:
        return f"sha512:{sha512.group(1).lower()}"
    sha256 = re.search(r"\b([0-9a-fA-F]{64})\b", snippet)
    if sha256:
        return f"sha256:{sha256.group(1).lower()}"
    return ""


def _download_integrity(source_url, version, checksum):
    if checksum:
        return "checksum_verified"
    lowered = source_url.lower()
    if version and not any(marker in lowered for marker in ("latest", "master", "main", "/head")):
        return "pinned_build_verified"
    return "mutable_build_verified"


def _canonical_package_name(package_name):
    value = normalize_dependency_name(package_name.split(":", 1)[0])
    if value.startswith("lib") and len(value) > 3:
        value = value[3:]
    if value.endswith("-dev"):
        value = value[:-4]
    return normalize_dependency_name(value)


def _package_aliases(package_name):
    exact = normalize_dependency_name(package_name)
    canonical = _canonical_package_name(package_name)
    return _unique_strings((exact, canonical))


def _strip_archive_suffix(filename):
    value = str(filename or "")
    lowered = value.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return value[:-len(suffix)]
    return value


def _remove_version_suffix(value):
    text = str(value or "").removesuffix(".git")
    return re.sub(
        r"[-_.]v?\d+(?:[-_.]\d+)*(?:[-_.]?(?:alpha|beta|rc)\d*)?.*$",
        "",
        text,
        flags=re.IGNORECASE,
    ) or text


def _is_apt_update(command):
    return bool(re.search(r"\bapt(?:-get)?\s+update\b", command, flags=re.IGNORECASE))


def _has_line_continuation(line):
    return bool(re.search(r"\\\s*$", line))


def _unique_strings(values):
    result = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        normalized = normalize_dependency_name(value)
        if not value or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return tuple(result)
