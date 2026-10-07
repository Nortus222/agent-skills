"""Read-only CodeMagic API client for diagnosing builds.

The skill observes builds through GitHub check runs, which report that a build
failed but never why. This adds the missing half: the failing step's name and
its log, and the list of builds for a branch so a workflow that never started
can be told apart from one that started and failed.

Nothing here mutates CodeMagic. The token can start and cancel builds, so the
client deliberately exposes no way to do either.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

API_ROOT = "https://api.codemagic.io"
KEYCHAIN_SERVICE = "CODEMAGIC_API_TOKEN"
TOKEN_VARIABLE = "CODEMAGIC_API_TOKEN"

# CodeMagic check runs link to https://codemagic.io/app/<appId>/build/<buildId>,
# which exposes both ids. Both are 24-hex Mongo ids.
_BUILD_URL = re.compile(
    r"https://codemagic\.io/app/(?P<app>[0-9a-f]{24})/build/(?P<build>[0-9a-f]{24})"
)


class CodemagicError(RuntimeError):
    """A CodeMagic request failed or returned something unusable."""


@dataclass(frozen=True)
class BuildReference:
    """The pair of ids needed to address one build."""

    app_id: str
    build_id: str


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]: ...


class UrllibTransport:
    """Default transport. Separated so tests never reach the network."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except OSError as error:
            raise CodemagicError(f"CodeMagic request failed: {error}") from error


def build_reference(details_url: str | None) -> BuildReference | None:
    """The app and build ids inside a check run's details URL, if it names one."""
    if not details_url:
        return None
    match = _BUILD_URL.search(details_url)
    if match is None:
        return None
    return BuildReference(match.group("app"), match.group("build"))


def _github_repository(value: Any) -> str | None:
    """Normalize a GitHub URL or owner/repo without accepting another host."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.lower().startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    elif "://" in value:
        url = urllib.parse.urlsplit(value)
        if url.hostname != "github.com":
            return None
        path = url.path
    else:
        path = value
    path = path.strip("/").lower().removesuffix(".git")
    if not re.fullmatch(r"[^/\s:]+/[^/\s:]+", path):
        return None
    return path


def failed_steps(build: dict[str, Any]) -> list[dict[str, Any]]:
    """Every build action that failed, in the order CodeMagic ran them."""
    actions = build.get("buildActions")
    if not isinstance(actions, list):
        return []
    return [
        action
        for action in actions
        if isinstance(action, dict) and action.get("status") == "failed"
    ]


def _keychain_token() -> str | None:
    """Read the token from the macOS keychain, where it is stored interactively."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def resolve_token(
    environ: dict[str, str] | None = None,
    keychain: Callable[[], str | None] = _keychain_token,
) -> str | None:
    """The API token from the environment, else the keychain, else nothing.

    Absent a token the skill keeps working with GitHub check runs alone, so this
    returns None rather than raising.
    """
    environ = os.environ if environ is None else environ
    from_environment = (environ.get(TOKEN_VARIABLE) or "").strip()
    if from_environment:
        return from_environment
    return keychain()


class CodemagicClient:
    """Read-only access to builds and their logs."""

    def __init__(self, token: str, transport: Transport | None = None) -> None:
        self.token = token
        self.transport = transport or UrllibTransport()

    def build(self, build_id: str) -> dict[str, Any]:
        """One build, including its `buildActions` and their statuses."""
        payload = self._get_json(f"{API_ROOT}/builds/{build_id}")
        build = payload.get("build") if isinstance(payload, dict) else None
        if not isinstance(build, dict):
            raise CodemagicError(f"build {build_id}: unexpected response shape")
        return build

    def application_for_repository(self, repository: str) -> str | None:
        """Find the exact GitHub repository, including apps without check runs."""
        expected = _github_repository(repository)
        if expected is None:
            raise CodemagicError(f"not a GitHub repository: {repository}")
        payload = self._get_json(f"{API_ROOT}/apps")
        applications = (
            payload.get("applications") if isinstance(payload, dict) else None
        )
        if not isinstance(applications, list):
            raise CodemagicError("unexpected response shape listing applications")
        matches: set[str] = set()
        for application in applications:
            if not isinstance(application, dict):
                continue
            source = application.get("repository")
            source = source if isinstance(source, dict) else {}
            urls = [source.get("htmlUrl"), application.get("repositoryUrl")]
            if any(_github_repository(url) == expected for url in urls):
                app_id = application.get("_id")
                if not isinstance(app_id, str) or not app_id:
                    raise CodemagicError(f"application for {repository} has no id")
                matches.add(app_id)
        if len(matches) > 1:
            raise CodemagicError(
                f"multiple CodeMagic applications matched {repository}"
            )
        return next(iter(matches), None)

    def builds_for_branch(self, app_id: str, branch: str) -> list[dict[str, Any]]:
        """Recent builds of one application on one branch, newest first."""
        query = urllib.parse.urlencode({"appId": app_id, "branch": branch})
        payload = self._get_json(f"{API_ROOT}/builds?{query}")
        builds = payload.get("builds") if isinstance(payload, dict) else None
        if not isinstance(builds, list):
            raise CodemagicError("unexpected response shape listing builds")
        return [
            build
            for build in builds
            if isinstance(build, dict)
            and build.get("appId") == app_id
            and build.get("branch") == branch
        ]

    def step_log(self, build_id: str, step_id: str) -> str:
        """The raw log for one build step."""
        status, body = self.transport.get(
            f"{API_ROOT}/builds/{build_id}/step/{step_id}", self._headers()
        )
        if status != 200:
            raise CodemagicError(
                f"build {build_id} step {step_id}: CodeMagic returned {status}"
            )
        return body

    def _headers(self) -> dict[str, str]:
        return {"x-auth-token": self.token, "Content-Type": "application/json"}

    def _get_json(self, url: str) -> Any:
        status, body = self.transport.get(url, self._headers())
        if status != 200:
            raise CodemagicError(f"CodeMagic returned {status} for {url}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise CodemagicError(
                f"CodeMagic returned invalid JSON for {url}"
            ) from error
