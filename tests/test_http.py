"""Tests for authenticated HTTP client diagnostics."""

# Response doubles intentionally expose only the HTTP methods under test.
# pylint: disable=too-few-public-methods

import logging

import pytest

from lib import http


class Response:
    """Minimal HTTP response double for JSON tracing tests."""

    def __init__(
        self, payload, content_type: str = "application/json", status_code: int = 200
    ):
        """Store response data.

        Args:
            payload: JSON-compatible payload or exception raised by ``json``.
            content_type: Response content type.
            status_code: HTTP status code.
        """
        self.payload = payload
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.reason = "Bad Request"

    def json(self):
        """Return payload or raise configured JSON decoding error."""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        """Treat the default response double as a successful HTTP response."""


def test_http_client_traces_json_only_when_enabled(monkeypatch, caplog) -> None:
    """JSON response tracing is opt-in and includes structured response data."""
    logger = logging.getLogger("test.http.trace")
    client = http.ServiceHttpClient(
        "https://service.test", "secret-token", logger=logger
    )
    response = Response(
        {"items": [1, {"nested": ["alpha", "beta"]}], "state": "RUNNING"}
    )
    monkeypatch.setattr(http.requests, "request", lambda *_args, **_kwargs: response)
    caplog.set_level(logging.DEBUG, logger=logger.name)

    client.request("/jobs/job-1")
    assert "JSON response" not in caplog.text

    client.configure_json_trace(True)
    client.request("/jobs/job-1")

    assert '"state": "RUNNING"' in caplog.text
    assert '"nested": [' in caplog.text
    assert '"beta"' in caplog.text
    assert "secret-token" not in caplog.text
    assert "Authorization" not in caplog.text


def test_http_client_reports_malformed_json_without_failing(
    monkeypatch, caplog
) -> None:
    """Malformed successful JSON responses are logged but left to callers."""
    logger = logging.getLogger("test.http.malformed")
    client = http.ServiceHttpClient(
        "https://service.test", "secret-token", logger=logger
    )
    client.configure_json_trace(True)
    response = Response(ValueError("bad json"))
    monkeypatch.setattr(http.requests, "request", lambda *_args, **_kwargs: response)
    caplog.set_level(logging.DEBUG, logger=logger.name)

    assert client.request("/jobs/job-1") is response
    assert "returned invalid JSON" in caplog.text


def test_http_client_does_not_trace_binary_response(monkeypatch, caplog) -> None:
    """Binary object responses are never decoded or logged as JSON."""
    logger = logging.getLogger("test.http.binary")
    client = http.ServiceHttpClient(
        "https://service.test", "secret-token", logger=logger
    )
    client.configure_json_trace(True)

    class BinaryResponse(Response):
        """Response whose JSON decoder must not be called."""

        def json(self):
            """Fail if binary payload is decoded."""
            raise AssertionError("binary response was decoded")

    monkeypatch.setattr(
        http.requests,
        "request",
        lambda *_args, **_kwargs: BinaryResponse(
            b"payload", "application/octet-stream"
        ),
    )
    caplog.set_level(logging.DEBUG, logger=logger.name)

    client.request("/getobject")

    assert "JSON response" not in caplog.text
    assert "payload" not in caplog.text


def test_http_client_traces_json_error_before_raising(monkeypatch, caplog) -> None:
    """JSON error responses are traced without changing API error handling."""
    logger = logging.getLogger("test.http.error")
    client = http.ServiceHttpClient(
        "https://service.test", "secret-token", logger=logger
    )
    client.configure_json_trace(True)
    response = Response({"error": "quota exceeded"}, status_code=429)
    monkeypatch.setattr(http.requests, "request", lambda *_args, **_kwargs: response)
    caplog.set_level(logging.DEBUG, logger=logger.name)

    with pytest.raises(RuntimeError, match="quota exceeded"):
        client.request("/jobs")

    assert '"error": "quota exceeded"' in caplog.text


def test_compatibility_request_sends_optional_bearer_token(monkeypatch) -> None:
    """Compatibility requests include the configured bearer token when provided."""
    captured = {}

    def fake_get(*_args, **kwargs):
        captured.update(kwargs)
        return Response({"minimum_year": 2013})

    monkeypatch.setattr(http.requests, "get", fake_get)

    result = http.request_json(
        "https://service.test", "/metadata", {}, token="client-token"
    )

    assert result == {"minimum_year": 2013}
    assert captured["headers"] == {"Authorization": "Bearer client-token"}
