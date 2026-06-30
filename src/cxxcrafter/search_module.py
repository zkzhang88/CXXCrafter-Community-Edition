import json
import logging
import re
import urllib.parse
import urllib.request

from cxxcrafter.config import (
    SEARCH_API_KEY,
    SEARCH_API_URL,
    SEARCH_ENABLED,
    SEARCH_MAX_RESULTS,
    SEARCH_PROVIDER,
    SEARCH_RETRY_TIMES,
    SEARCH_TIMEOUT_SECONDS,
)


MAX_QUERY_LENGTH = 500
MAX_ERROR_CONTEXT_LENGTH = 1200
MAX_RESULT_CONTENT_LENGTH = 700


class SearchClient:
    def __init__(
        self,
        enabled=SEARCH_ENABLED,
        provider=SEARCH_PROVIDER,
        api_url=SEARCH_API_URL,
        api_key=SEARCH_API_KEY,
        max_results=SEARCH_MAX_RESULTS,
        timeout_seconds=SEARCH_TIMEOUT_SECONDS,
        retry_times=SEARCH_RETRY_TIMES,
        logger=None,
    ):
        self.enabled = bool(enabled)
        self.provider = (provider or "generic").strip().lower()
        self.api_url = api_url
        self.api_key = api_key
        self.max_results = max(0, int(max_results))
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.retry_times = max(0, int(retry_times))
        self.logger = logger or logging.getLogger(__name__)

    def search(self, query):
        if not self.enabled:
            self.logger.info("Web search disabled.")
            return []
        if not self.api_url:
            self.logger.warning("Web search enabled but search_api_url is not configured.")
            return []

        query = _compact_text(query, MAX_QUERY_LENGTH)
        if not query:
            self.logger.warning("Web search skipped because query is empty.")
            return []

        self.logger.info(f"Web search query: {query}")
        last_error = None
        for attempt in range(self.retry_times + 1):
            try:
                results = self._request(query)
                self._log_results(results)
                return results
            except Exception as e:
                last_error = e
                if attempt < self.retry_times:
                    self.logger.warning(f"Web search attempt {attempt + 1} failed: {e}")

        self.logger.warning(f"Web search failed; continuing without search results: {last_error}")
        return []

    def _request(self, query):
        if self.provider == "searxng":
            return self._request_searxng(query)
        return self._request_generic(query)

    def _request_generic(self, query):
        payload = json.dumps({
            "query": query,
            "max_results": self.max_results,
        }).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.api_url,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")

        return parse_search_response(body, self.max_results)

    def _request_searxng(self, query):
        separator = "&" if "?" in self.api_url else "?"
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
        })
        request = urllib.request.Request(
            f"{self.api_url}{separator}{params}",
            headers={
                "Accept": "application/json",
                "X-Real-IP": "127.0.0.1",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")

        return parse_search_response(body, self.max_results)

    def _log_results(self, results):
        self.logger.info(f"Web search returned {len(results)} result(s).")
        for result in results:
            title = result.get("title") or "Untitled"
            url = result.get("url") or ""
            self.logger.info(f"Web search result: {title} {url}".strip())


def parse_search_response(body, max_results):
    payload = json.loads(body)
    if isinstance(payload, dict):
        raw_results = payload.get("results", [])
    elif isinstance(payload, list):
        raw_results = payload
    else:
        raw_results = []

    if not isinstance(raw_results, list):
        return []

    results = []
    for item in raw_results[:max(0, int(max_results))]:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or item.get("snippet") or item.get("summary") or ""
        results.append({
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "content": _compact_text(content, MAX_RESULT_CONTENT_LENGTH),
        })
    return results


def format_search_results(results):
    if not results:
        return ""

    lines = ["Web Search Results:"]
    for index, result in enumerate(results, start=1):
        title = result.get("title") or "Untitled"
        url = result.get("url") or ""
        content = result.get("content") or ""
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   Summary: {content}")
    return "\n".join(lines)


def build_generation_search_query(project_name, build_system_name):
    build_system = build_system_name or "C++"
    return _compact_text(
        f"{project_name} {build_system} build from source Ubuntu dependencies",
        MAX_QUERY_LENGTH,
    )


def build_repair_search_query(project_name, build_system_name, error_message):
    build_system = build_system_name or "C++"
    cleaned_error = clean_error_message(error_message)
    hints = extract_error_hints(cleaned_error)
    error_context = " ".join(hints) if hints else cleaned_error[-MAX_ERROR_CONTEXT_LENGTH:]
    return _compact_text(
        f"{project_name} {build_system} Docker build error {error_context}",
        MAX_QUERY_LENGTH,
    )


def clean_error_message(error_message):
    text = _stringify_message(error_message)
    text = _remove_ansi_escape_sequences(text)
    return _compact_text(text, MAX_ERROR_CONTEXT_LENGTH)


def extract_error_hints(error_message):
    patterns = [
        r"fatal error:\s*([^:\n]+):\s*No such file or directory",
        r"Could NOT find\s+([A-Za-z0-9_.+\-/]+)",
        r"Package ['\"]([^'\"]+)['\"] not found",
        r"No package ['\"]([^'\"]+)['\"] found",
        r"cannot find -l([A-Za-z0-9_.+\-]+)",
        r"([A-Za-z0-9_.+\-/]+): command not found",
    ]

    hints = []
    for pattern in patterns:
        for match in re.findall(pattern, error_message, flags=re.IGNORECASE):
            value = match[0] if isinstance(match, tuple) else match
            value = str(value).strip()
            if value and value not in hints:
                hints.append(value)
    return hints[:5]


def _stringify_message(message):
    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, dict):
                parts.append(str(item.get("stream") or item.get("message") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if message is None else str(message)


def _remove_ansi_escape_sequences(text):
    try:
        ansi_escape_1 = re.compile(r"\^\[\[([0-9]+)(;[0-9]+)*[mG]")
        ansi_escape_2 = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
        message = re.sub(r"\x1b\[[0-9;]*[mK]|\x1b\(B", "", ansi_escape_1.sub("", text))
        return ansi_escape_2.sub("", message)
    except Exception:
        return text


def _compact_text(value, max_length):
    text = " ".join(str(value).split())
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip()
