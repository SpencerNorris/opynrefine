"""
OpenRefine API client and CLI entry point.
Documentation for OpenRefine API: https://openrefine.org/docs/technical-reference/openrefine-api
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, urlparse

import requests


LOGGER = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure a sane default logging setup if the app has not done so."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    LOGGER.setLevel(level)


class CommandProxy:
    """Fluent builder for OpenRefine command paths."""

    def __init__(self, client: "OpenRefineClient", parts: Optional[Iterable[str]] = None):
        self._client = client
        self._parts = list(parts or [])

    def __getattr__(self, attr: str) -> "CommandProxy":
        if attr.startswith("_"):
            raise AttributeError(attr)
        return CommandProxy(self._client, self._parts + [attr.replace("_", "-")])

    def __call__(
        self,
        *,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        path = "/".join(self._parts)
        return self._client._request(
            path,
            method=method,
            params=params,
            data=data,
            json_body=json_body,
            files=files,
            headers=headers,
            timeout=timeout,
        )


class OpenRefineClient:
    """Object-oriented wrapper for the OpenRefine HTTP API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3333",
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 60,
        logger: Optional[logging.Logger] = None,
        auto_register_signals: bool = True,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.logger = logger or LOGGER
        self._stop_event = threading.Event()
        self._signal_handlers: Dict[int, Any] = {}
        self._csrf_token: Optional[str] = None
        if auto_register_signals:
            self.register_signal_handlers(signal.SIGINT, signal.SIGTERM)

    @property
    def command(self) -> CommandProxy:
        return CommandProxy(self)

    def register_signal_handlers(self, *signals_to_handle: signal.Signals) -> None:
        for sig in signals_to_handle:
            try:
                self._signal_handlers[sig] = signal.signal(sig, self._handle_signal)  # type: ignore[assignment]
            except (ValueError, OSError):  # pragma: no cover - not hit on Windows CI typically
                self.logger.debug("Unable to register handler for %s", sig)

    def unregister_signal_handlers(self) -> None:
        for sig, handler in self._signal_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - best effort only
                self.logger.debug("Unable to restore original handler for %s", sig)
        self._signal_handlers.clear()

    def close(self) -> None:
        self.unregister_signal_handlers()
        self.session.close()

    # Public helper methods -------------------------------------------------
    def list_projects(self) -> Dict[str, Any]:
        """Return metadata for all projects."""
        response = self.command.core.get_all_project_metadata()
        response.raise_for_status()
        return response.json()

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        response = self.command.core.delete_project(method="POST", data={"project": project_id})
        response.raise_for_status()
        return response.json()

    def get_models(self, project_id: str) -> Dict[str, Any]:
        response = self.command.core.get_models(params={"project": project_id})
        response.raise_for_status()
        return response.json()

    def get_rows(self, project_id: str, **options: Any) -> Dict[str, Any]:
        params = {"project": project_id, **options}
        response = self.command.core.get_rows(params=params)
        response.raise_for_status()
        return response.json()

    def export_rows(self, project_id: str, export_format: str = "tsv", **options: Any) -> requests.Response:
        """
        Export rows from a project.
        Returns the raw requests.Response object to allow streaming output.

        Raises:
            requests.HTTPError: if the server returns a 4xx/5xx status.
            RuntimeError: if OpenRefine returns an HTML error page with a 2xx status,
                which can happen when the project ID is invalid or the project is corrupt.
        """
        data = {"project": project_id, "format": export_format, **options}
        if "engine" not in data:
            data["engine"] = '{"facets": [], "mode": "row-based"}'
        response = self.command.core.export_rows(method="POST", data=data)
        response.raise_for_status()
        # OpenRefine has a known bug where it can return HTTP 200 with a text/html
        # error page body (e.g. "Failed to find project id … - may be corrupt")
        # instead of the expected data. Detect and raise rather than silently
        # passing the HTML to the caller as if it were valid export data.
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            snippet = response.text[:500].strip()
            raise RuntimeError(
                f"OpenRefine returned an HTML error page instead of export data "
                f"for project {project_id!r}. "
                f"The project may be corrupt or the ID may be invalid. "
                f"Response snippet: {snippet!r}"
            )
        return response

    def apply_operations(self, project_id: str, operations: Any) -> Dict[str, Any]:
        payload = json.dumps(operations)
        response = self.command.core.apply_operations(
            method="POST",
            data={"project": project_id, "operations": payload},
        )
        response.raise_for_status()
        return response.json()

    def compute_facets(self, project_id: str, facets: Any) -> Dict[str, Any]:
        payload = json.dumps(facets)
        response = self.command.core.compute_facets(
            method="POST",
            data={"project": project_id, "engine": payload},
        )
        response.raise_for_status()
        return response.json()

    def create_project_from_url(self, project_name: str, data_url: str, format_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = {
            "projectName": project_name,
            "format": (format_options or {}).get("format"),
            "options": json.dumps(format_options or {}),
            "url": data_url,
        }
        response = self.command.core.create_project_from_url(method="POST", data=data)
        response.raise_for_status()
        return response.json()

    def create_project_from_upload(
        self,
        project_name: str,
        file_path: str,
        *,
        format_hint: Optional[str] = None,
        format_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        options_payload = json.dumps(format_options or {}) if format_options else None
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as handle:
            files = {"project-file": (filename, handle)}
            data: Dict[str, Any] = {"project-name": project_name}
            if format_hint:
                data["format"] = format_hint
            if options_payload:
                data["options"] = options_payload
            response = self.command.core.create_project_from_upload(
                method="POST",
                data=data,
                files=files,
            )
        response.raise_for_status()
        project_id = self._extract_project_id(response.url)
        return {
            "project_id": project_id,
            "project_url": response.url,
            "html": response.text,
        }

    def execute_command(
        self,
        command_path: str,
        *,
        method: str = "GET",
        **kwargs: Any,
    ) -> requests.Response:
        """Execute an arbitrary command path and return the raw Response object."""
        return self._request(command_path, method=method, **kwargs)

    # Internal helpers ------------------------------------------------------
    def _handle_signal(self, signum: int, frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - defensive
            name = str(signum)
        self.logger.warning("Received %s signal; aborting new requests", name)
        self._stop_event.set()

    def _build_url(self, command: str) -> str:
        command = command.lstrip("/")
        if not command.startswith("command/"):
            command = f"command/{command}"
        return f"{self.base_url}/{command}"

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            value = f"http://{value}"
        return value

    def _ensure_not_interrupted(self) -> None:
        if self._stop_event.is_set():
            raise RuntimeError("OpenRefine client interrupted by signal")

    def _request(
        self,
        command: str,
        *,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        self._ensure_not_interrupted()
        url = self._build_url(command)
        self.logger.debug("%s %s params=%s", method.upper(), url, params)
        method_upper = method.upper()
        params = self._maybe_inject_csrf_token_query(params, method_upper)
        data = self._maybe_inject_csrf_token_form(data, method_upper)
        json_body = self._maybe_inject_csrf_token_json(json_body, method_upper)
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            data=data,
            json=json_body,
            files=files,
            headers=headers,
            timeout=timeout or self.timeout,
        )
        self.logger.info("%s %s -> %s", method.upper(), url, response.status_code)
        return response

    @staticmethod
    def _extract_project_id(url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        project_param = parse_qs(parsed.query).get("project")
        if not project_param:
            return None
        return project_param[0]

    def _ensure_csrf_token(self) -> str:
        if not self._csrf_token:
            self._csrf_token = self._fetch_csrf_token()
        return self._csrf_token

    def _fetch_csrf_token(self) -> str:
        url = f"{self.base_url}/command/core/get-csrf-token"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token")
        if not token:
            raise RuntimeError("Failed to obtain CSRF token from OpenRefine")
        return token

    def _maybe_inject_csrf_token_form(self, data: Optional[Any], method: str) -> Optional[Any]:
        if method in {"GET", "HEAD"}:
            return data
        token = self._ensure_csrf_token()
        if data is None:
            return {"csrf_token": token}
        if isinstance(data, dict):
            if "csrf_token" not in data:
                data = dict(data)
                data["csrf_token"] = token
            return data
        if isinstance(data, list):
            if not any(k == "csrf_token" for k, _ in data):
                data = list(data)
                data.append(("csrf_token", token))
            return data
        return data

    def _maybe_inject_csrf_token_json(self, json_body: Optional[Any], method: str) -> Optional[Any]:
        if method in {"GET", "HEAD"} or json_body is None:
            return json_body
        token = self._ensure_csrf_token()
        if isinstance(json_body, dict) and "csrf_token" not in json_body:
            json_body = dict(json_body)
            json_body["csrf_token"] = token
        return json_body

    def _maybe_inject_csrf_token_query(self, params: Optional[Dict[str, Any]], method: str) -> Optional[Dict[str, Any]]:
        if method in {"GET", "HEAD"}:
            return params
        token = self._ensure_csrf_token()
        if not params:
            return {"csrf_token": token}
        if "csrf_token" not in params:
            params = dict(params)
            params["csrf_token"] = token
        return params


def _parse_kv_json(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("Expected a JSON object")
        return parsed
    except json.JSONDecodeError as exc:  # pragma: no cover - CLI-only path
        raise SystemExit(f"Invalid JSON payload provided: {exc}") from exc


def _load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - CLI path
        raise SystemExit(f"Operations file not found: {path}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal CLI for the OpenRefine API client")
    parser.add_argument("--base-url", default="http://127.0.0.1:3333", help="OpenRefine server base URL")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds")
    parser.add_argument("--log-level", default="INFO", help="Python logging level (default: INFO)")

    subparsers = parser.add_subparsers(dest="action", required=True)

    list_parser = subparsers.add_parser("list-projects", help="List available projects")
    list_parser.set_defaults(func=_cli_list_projects)

    delete_parser = subparsers.add_parser("delete-project", help="Delete a project by id")
    delete_parser.add_argument("project_id")
    delete_parser.set_defaults(func=_cli_delete_project)

    call_parser = subparsers.add_parser("call", help="Execute an arbitrary OpenRefine command")
    call_parser.add_argument("command_path", help="Path following the /command/ prefix, e.g. core/get-models")
    call_parser.add_argument("--method", default="GET", help="HTTP method to use")
    call_parser.add_argument("--params", help="JSON object of query params")
    call_parser.add_argument("--data", help="JSON object of form data to send")
    call_parser.set_defaults(func=_cli_call)

    create_parser = subparsers.add_parser("create-project", help="Create a new project from a file upload")
    create_parser.add_argument("project_name", help="Name for the new project")
    create_parser.add_argument("file_path", help="Path to the data file to upload")
    create_parser.add_argument("--format-hint", help="Optional MIME hint like text/line-based/*sv")
    create_parser.add_argument("--options", help="JSON string of format-specific options")
    create_parser.set_defaults(func=_cli_create_project)

    apply_parser = subparsers.add_parser("apply-operations", help="Apply a JSON operations file to a project")
    apply_parser.add_argument("project_id", help="Target project id")
    apply_parser.add_argument("operations_file", help="Path to the operations JSON file")
    apply_parser.set_defaults(func=_cli_apply_operations)

    return parser


def _cli_list_projects(client: OpenRefineClient, _: argparse.Namespace) -> None:
    projects = client.list_projects()
    json.dump(projects, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cli_delete_project(client: OpenRefineClient, args: argparse.Namespace) -> None:
    result = client.delete_project(args.project_id)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cli_call(client: OpenRefineClient, args: argparse.Namespace) -> None:
    params = _parse_kv_json(args.params)
    data = _parse_kv_json(args.data)
    response = client.execute_command(
        args.command_path,
        method=args.method,
        params=params,
        data=data,
    )
    # Behave like curl/httpie: output headers/body depending on flags?
    # For now, just simplistic behavior: try JSON, else text
    try:
        json.dump(response.json(), sys.stdout, indent=2)
    except ValueError:
        sys.stdout.write(response.text)
    sys.stdout.write("\n")


def _cli_create_project(client: OpenRefineClient, args: argparse.Namespace) -> None:
    options = _parse_kv_json(args.options)
    result = client.create_project_from_upload(
        args.project_name,
        args.file_path,
        format_hint=args.format_hint,
        format_options=options,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cli_apply_operations(client: OpenRefineClient, args: argparse.Namespace) -> None:
    operations = _load_json_file(args.operations_file)
    result = client.apply_operations(args.project_id, operations)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(logging, args.log_level.upper(), logging.INFO))
    client = OpenRefineClient(base_url=args.base_url, timeout=args.timeout)
    try:
        args.func(client, args)
    except requests.HTTPError as exc:
        sys.stderr.write(f"HTTP Error: {exc}\n")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
