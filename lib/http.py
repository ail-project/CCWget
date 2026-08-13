"""HTTP request and bearer-authentication helpers."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Callable

import requests

CLIENT_ACTIONS = {
    "domain-enum",
    "enumerate-url",
    "list-domain",
    "list-domain-detail",
    "list-fqdn",
    "list-fqdn-detail",
    "pdns",
    "query",
    "query-info",
    "sha1",
    "sha1-info",
    "string-search",
}


def parse_positive_interval(value: str, name: str) -> float:
    """Validate one positive interval setting.

    Args:
        value: Candidate interval in seconds.
        name: Configuration name used in error messages.

    Returns:
        Positive interval in seconds.

    Raises:
        ValueError: If value is not a positive number.
    """
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc
    if interval <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return interval


class ServiceHttpClient:
    """Authenticated HTTP client for the Common Crawl backend service."""

    def __init__(
        self,
        service_url: str,
        token: str,
        timeout: int = 300,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create an authenticated service client.

        Args:
            service_url: Backend base URL.
            token: Bearer token sent to the backend.
            timeout: Request timeout in seconds.
            logger: Optional logger for request diagnostics.
        """
        self.service_url = service_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)
        self.trace_json = False

    def configure_json_trace(self, enabled: bool) -> None:
        """Enable or disable decoded JSON response tracing.

        Args:
            enabled: Whether JSON response bodies may be logged at debug level.
        """
        self.trace_json = enabled

    def _trace_json_response(
        self,
        response: requests.Response,
        method: str,
        endpoint: str,
    ) -> None:
        """Log one JSON response when explicit tracing is enabled.

        Args:
            response: HTTP response received from the backend.
            method: HTTP method used for the request.
            endpoint: Backend endpoint, without service URL or auth headers.
        """
        if not self.trace_json:
            return
        content_type = response.headers.get("Content-Type", "").lower()
        if "json" not in content_type:
            return
        try:
            payload = response.json()
        except ValueError:
            self.logger.debug(
                "%s %s -> %s returned invalid JSON",
                method,
                endpoint,
                response.status_code,
            )
            return
        formatted = json.dumps(payload, indent=2, sort_keys=True, default=str)
        self.logger.debug(
            "%s %s -> %s JSON response:\n%s",
            method,
            endpoint,
            response.status_code,
            formatted,
        )

    def request(
        self, endpoint: str, method: str = "GET", **kwargs: Any
    ) -> requests.Response:
        """Call one authenticated service endpoint.

        Args:
            endpoint: Service path beginning with ``/``.
            method: HTTP method.
            kwargs: Extra ``requests.request`` arguments.

        Returns:
            Successful HTTP response.

        Raises:
            RuntimeError: If the request fails or the backend rejects it.
        """
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token}"
        try:
            self.logger.debug("%s %s", method, endpoint)
            response = requests.request(
                method,
                f"{self.service_url}{endpoint}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
            self._trace_json_response(response, method, endpoint)
            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", "")
                except ValueError:
                    detail = ""
                message = detail or response.reason or response.status_code
                raise RuntimeError(f"Backend API error: {message}")
            return response
        except requests.RequestException as exc:
            raise RuntimeError(f"Backend API error: {exc}") from exc

    def get_json(self, endpoint: str, **kwargs: Any) -> Any:
        """Request and decode one authenticated JSON endpoint.

        Args:
            endpoint: Service path beginning with ``/``.
            kwargs: Extra ``requests.request`` arguments.

        Returns:
            Decoded JSON response.

        Raises:
            RuntimeError: If response body is not valid JSON.
        """
        response = self.request(endpoint, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("Backend API error: invalid JSON response") from exc


# pylint: disable=too-many-arguments,too-many-positional-arguments  # Compatibility helper keeps request options explicit.
def request_json(
    service_url: str,
    endpoint: str,
    params: dict[str, Any],
    timeout: int = 300,
    logger: logging.Logger | None = None,
    token: str = "",
) -> Any:
    """Request JSON from a compatibility endpoint.

    Args:
        service_url: Backend base URL.
        endpoint: Backend route beginning with ``/``.
        params: Query parameters sent to the backend.
        timeout: Request timeout in seconds.
        logger: Optional logger for request diagnostics.
        token: Optional bearer token sent to the backend.

    Returns:
        Decoded JSON response.

    Raises:
        RuntimeError: If the request fails or returns invalid JSON.
    """
    active_logger = logger or logging.getLogger(__name__)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        active_logger.debug("GET %s", endpoint)
        response = requests.get(
            f"{service_url.rstrip('/')}{endpoint}",
            params=params,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Backend API error: {exc}") from exc


def authenticate_bearer(
    authorization: str,
    entries: dict[str, dict[str, Any]] | Callable[[], dict[str, dict[str, Any]]],
) -> tuple[str, str] | None:
    """Resolve one bearer header to a safe client identity.

    Args:
        authorization: Complete HTTP Authorization header.
        entries: Normalized token entries or lazy entry loader.

    Returns:
        Safe client ID and mode, or ``None`` when authentication fails.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    active_entries = entries() if callable(entries) else entries
    for mode, entry in active_entries.items():
        if secrets.compare_digest(token, str(entry["token"])):
            return str(entry["client_id"]), mode
    return None


def safe_job_summary(job: Any) -> dict[str, Any]:
    """Build a client-visible queue job summary.

    Args:
        job: Queue job object with public status attributes.

    Returns:
        Safe status mapping without token, arguments, result, or payload data.
    """
    requested_action = str(job.arguments.get("_client_action", ""))
    client_action = (
        requested_action if requested_action in CLIENT_ACTIONS else job.operation
    )
    return {
        "job_id": job.job_id,
        "operation": job.operation,
        "client_action": client_action,
        "state": job.state,
        "position": job.position,
        "completed_tables": job.completed_tables,
        "total_tables": job.total_tables,
        "total_rows": job.total_rows,
        "progress_detail": job.progress_detail,
        "error": job.error,
        "created": job.created,
        "updated": job.updated,
    }
