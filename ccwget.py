#!/usr/bin/env python3
"""API-only customer client with token-selected object download."""

# pylint: disable=invalid-name,wrong-import-position
# Script-path bootstrap must run before shared root-package imports.

from __future__ import annotations

import base64
from copy import copy
from functools import partial
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import load_client_config
    from lib.engine import (
        RemoteObjectContext,
        aggregate_pdns_observations,
        direct_payload as engine_direct_payload,
        format_pdns_timestamp,
        parse_pdns_observation,
        populate_remote_payload,
        read_download as engine_read_download,
    )
    from lib.http import ServiceHttpClient, parse_positive_interval
    from lib import local as LOCAL
    from lib.printout import (
        occurrence_csv_row,
        print_flush_result,
        print_job_list,
        print_job_status,
        print_json_result,
        save_csv_rows,
    )
except ModuleNotFoundError as exc:
    missing = exc.name or "a project dependency"
    print(
        f"Missing Python dependency: {missing}\n\n"
        "Install the project virtual environment, then retry:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/python -m pip install -r requirements.txt\n"
        "  .venv/bin/python ccwget.py ...\n\n"
        "Or activate it first with: source .venv/bin/activate",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

CLIENT_CONFIG = load_client_config()
SERVICE_URL = CLIENT_CONFIG["service_url"]
TOKEN = CLIENT_CONFIG["token"]
REQUEST_TIMEOUT = 300
POLL_TIMEOUT = float(os.environ.get("CCWGET_POLL_TIMEOUT", "3600"))
LOGGER = logging.getLogger(__name__)
PDNS_SILENT_LOGGER = logging.getLogger("ccwget.pdns.silent")
PDNS_SILENT_LOGGER.disabled = True
HTTP_CLIENT = ServiceHttpClient(SERVICE_URL, TOKEN, REQUEST_TIMEOUT, LOGGER)


def parse_poll_interval(value: str) -> float:
    """Validate and convert the remote status polling interval.

    Args:
        value: Interval in seconds from configuration or the environment.

    Returns:
        Positive polling interval in seconds.

    Raises:
        ValueError: If the value is not a positive number.
    """
    return parse_positive_interval(value, "CCWGET_POLL_INTERVAL")


POLL_INTERVAL = parse_poll_interval(os.environ.get("CCWGET_POLL_INTERVAL", "1.0"))


def parse_args() -> Any:
    """Parse the exact argument contract shared with the local client.

    Returns:
        Parsed argument namespace.
    """
    return LOCAL.parse_args()


def request_api(endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
    """Call an authenticated service endpoint.

    Args:
        endpoint: Service path beginning with ``/``.
        method: HTTP method.
        kwargs: Requests arguments.

    Returns:
        Successful response.

    Raises:
        RuntimeError: If the service rejects the request.
    """
    return HTTP_CLIENT.request(endpoint, method=method, **kwargs)


def cancel_active_job(job_id: str) -> None:
    """Request server-side cancellation for one active job.

    Args:
        job_id: Queue job identifier submitted by this client.

    Raises:
        RuntimeError: If the server cannot accept the cancellation request.
    """
    request_api(f"/jobs/{job_id}", method="DELETE")


def submit(
    operation: str,
    arguments: dict[str, Any],
    progress: LOCAL.SearchProgress | None = None,
) -> Any:
    """Submit one search operation and return its final JSON result.

    Args:
        operation: Registered service operation.
        arguments: Operation query arguments.
        progress: Optional progress renderer for this search.

    Returns:
        Backend result JSON.
    """
    response = request_api(
        "/jobs", method="POST", json={"operation": operation, "arguments": arguments}
    )
    job = response.json()
    job_id = job["job_id"]
    reporter = progress or LOCAL.SearchProgress(quiet=False, verbose=1)
    if reporter.verbose:
        LOGGER.info("search %s submitted (0%%)", operation)
    try:
        _, result = wait_for_result(job_id, reporter)
    except KeyboardInterrupt:
        try:
            cancel_active_job(job_id)
        except RuntimeError as exc:
            print(f"Interrupted; unable to cancel job {job_id}: {exc}", file=sys.stderr)
        else:
            print(
                f"Interrupted; cancellation requested for job {job_id}", file=sys.stderr
            )
        raise SystemExit(130) from None
    return result


def progress_since(args: Any) -> str | None:
    """Return progress start date for selected search timeframe.

    Args:
        args: Parsed CLI arguments containing timeframe selectors.

    Returns:
        Inclusive start date as ``YYYYMMDD``, or ``None`` when unavailable.
    """
    if args.after:
        return args.after
    if args.time_range:
        return args.time_range.split(",", 1)[0]
    if args.year is not None:
        return f"{args.year:04d}0101"
    if args.alltime:
        minimum_year = getattr(args, "minimum_year", None) or LOCAL.MIN_YEAR
        return f"{minimum_year}0101"
    return None


def wait_for_result(
    job_id: str,
    reporter: Any,
    return_terminal_error: bool = False,
    wait_for_active: bool = True,
) -> tuple[dict[str, Any], Any]:
    """Wait for one existing job and retrieve its terminal result.

    Args:
        job_id: Queue job identifier.
        reporter: Search progress renderer.
        return_terminal_error: Return failed terminal state instead of raising.
        wait_for_active: Keep polling while job is active when true.

    Returns:
        Final status mapping and JSON-compatible result.

    Raises:
        RuntimeError: If polling times out or job fails.
    """
    last_state = None
    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        state = request_api(f"/jobs/{job_id}").json()
        state_changed = state["state"] != last_state
        if state_changed and reporter.verbose:
            state_percent = {"WAITING": 10, "RUNNING": 50, "DONE": 100}.get(
                state["state"], 0
            )
            reporter.log_state(job_id, state["state"], state_percent)
        if state["state"] in {"WAITING", "RUNNING", "CANCEL_REQUESTED"}:
            total_tables = state.get("total_tables", 0)
            total_rows = state.get("total_rows") if total_tables > 0 else None
            progress_detail = state.get("progress_detail", "")
            reporter.update(
                state["state"],
                position=state.get("position"),
                completed=state.get("completed_tables", 0),
                total=total_tables,
                total_rows=total_rows,
                detail=progress_detail,
            )
            if not wait_for_active:
                return state, None
        last_state = state["state"]
        if state["state"] in {"WAITING", "RUNNING", "CANCEL_REQUESTED"}:
            if time.monotonic() >= deadline:
                raise RuntimeError("backend job polling timed out")
            time.sleep(POLL_INTERVAL)
            continue
        if state["state"] in {"ERROR", "CANCELLED"}:
            reporter.finish("ERROR")
            if return_terminal_error:
                return state, None
            raise RuntimeError(
                state.get("error") or f"backend job {state['state'].lower()}"
            )
        result = request_api(f"/jobs/{job_id}/result").json()
        total_tables = state.get("total_tables", 0)
        total_rows = state.get("total_rows") if total_tables > 0 else None
        reporter.finish(
            "DONE",
            total=total_tables,
            total_rows=total_rows,
        )
        return state, result["result"]


def submit_async(operation: str, arguments: dict[str, Any]) -> str:
    """Submit one backend job without waiting for completion.

    Args:
        operation: Registered service operation.
        arguments: Operation query arguments.

    Returns:
        New job identifier.
    """
    response = request_api(
        "/jobs", method="POST", json={"operation": operation, "arguments": arguments}
    )
    return str(response.json()["job_id"])


# Async operation mapping preserves the shared CLI action contract.
# pylint: disable=too-many-return-statements
def build_job_request(args: Any) -> tuple[str, dict[str, Any]]:
    """Build an asynchronous job operation from parsed client arguments.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Operation name and backend arguments.

    Raises:
        ValueError: If no search action was supplied.
    """
    shared = common_args(args)
    if args.pdns:
        return "list-fqdn", {
            **shared,
            "_client_action": "pdns",
            "fqdn": args.pdns,
            "detail": "true",
            "info_only": "true",
            "all": "true",
        }
    if args.string_search:
        return "query", {
            **shared,
            "_client_action": "string-search",
            "url_contains": args.string_search,
            "url_contains_fields": LOCAL.selected_string_search_fields(args),
            "tld": args.tld or "",
            "all": "true",
            "info_only": "true",
            "limit": str(args.limit),
        }
    if args.enumerate_url:
        return "query", {
            **shared,
            "_client_action": "enumerate-url",
            "url": args.enumerate_url,
            "info_only": "true",
            "all": "true",
        }
    if args.domain_enumeration:
        return "domain-enum", {
            **shared,
            "_client_action": "domain-enum",
            "domain": args.domain_enumeration,
            "all": "true",
        }
    if args.list_fqdn or args.list_domain:
        key = "fqdn" if args.list_fqdn else "domain"
        action = f"list-{key}{'-detail' if args.detail else ''}"
        return ("list-fqdn" if key == "fqdn" else "list-domain"), {
            **shared,
            "_client_action": action,
            key: args.list_fqdn or args.list_domain,
            "detail": str(key == "fqdn" or args.detail).lower(),
            "all": "true",
        }
    if args.sha1:
        action = "sha1-info" if args.info_only else "sha1"
        return "sha1", {
            **shared,
            "_client_action": action,
            "digest": args.sha1,
        }
    if args.url:
        action = "query-info" if args.info_only else "query"
        return "query", {
            **shared,
            "_client_action": action,
            "url": args.url,
            "all": str(args.all).lower(),
        }
    raise ValueError("a search action is required with -async")


def show_job_status(job_id: str) -> None:
    """Print one authenticated backend job status.

    Args:
        job_id: Queue job identifier.
    """
    print_job_status(request_api(f"/jobs/{job_id}").json())


def show_jobs() -> None:
    """Print retained jobs owned by the configured client token."""
    print_job_list(request_api("/jobs").json()["jobs"])


def _render_domain_result(result: list[Any], args: Any) -> None:
    """Print resumed domain-enumeration values.

    Args:
        result: Domain-enumeration result values.
        args: Current CLI arguments.
    """
    if args.output:
        save_csv_rows(
            [{"fqdn": value} for value in result],
            args.output,
            ["fqdn"],
            args.quiet,
        )
    elif not args.quiet:
        for value in result:
            print(value)


def _render_occurrence_result(result: list[Any], args: Any) -> None:
    """Print resumed URL occurrences.

    Args:
        result: URL occurrence records.
        args: Current CLI arguments.
    """
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in result],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
    elif not args.quiet:
        for entry in result:
            print(LOCAL.format_instance(entry))


def _render_domain_listing(result: list[Any], args: Any) -> None:
    """Print resumed domain page occurrences.

    Args:
        result: Domain listing records.
        args: Current CLI arguments.
    """
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in result],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
    elif not args.quiet:
        for entry in result:
            print(LOCAL.format_instance(entry))


def _render_detail_listing(result: list[Any], args: Any) -> None:
    """Render resumed detailed listing records.

    Args:
        result: Detailed listing records.
        args: Current CLI arguments.
    """
    resumed_args = copy(args)
    resumed_args.detail = True
    if args.output:
        save_csv_rows(result, args.output, list(result[0]), args.quiet)
        return
    render(result, resumed_args, show_url=True)


def render_pdns_result(result: list[Any], args: Any) -> None:
    """Fetch WARC records and print aggregated passive-DNS observations.

    Args:
        result: Detailed FQDN listing records.
        args: Current CLI arguments controlling output.
    """
    observations = []
    selected_result = select_pdns_entries(result, not getattr(args, "full", False))
    total = len(selected_result)
    if total == 0:
        if not args.quiet:
            print("No passive DNS observations found.")
        return
    if args.verbose:
        LOGGER.info("passive DNS: fetching %d of %d indexed pages", total, len(result))
    progress = LOCAL.SearchProgress(
        args.quiet,
        args.verbose,
        message="Analysing",
        object_count=total,
    )
    progress.update("RUNNING", completed=0, total=total)
    active_logger = LOGGER if args.verbose else PDNS_SILENT_LOGGER
    for index, entry in enumerate(selected_result, start=1):
        if args.verbose:
            LOGGER.info("passive DNS: fetching WARC %d/%d", index, total)
        try:
            get_payload(entry, show_headers=True, logger=active_logger)
        except RuntimeError as exc:
            LOGGER.warning("passive DNS: skipping WARC %d/%d: %s", index, total, exc)
            progress.update("RUNNING", completed=index, total=total)
            continue
        observation = parse_pdns_observation(
            str(entry.get("warc_headers", "")),
            str(entry.get("url_host_name", "")),
        )
        entry.pop("warc_payload", None)
        entry.pop("warc_headers", None)
        entry.pop("http_headers", None)
        if observation is not None:
            observations.append(observation)
        progress.update("RUNNING", completed=index, total=total)
    progress.finish("DONE", total=total)
    ranges = aggregate_pdns_observations(observations)
    if not ranges and not args.quiet:
        print("No passive DNS observations found.")
        return
    if args.output:
        save_csv_rows(
            [
                {
                    "ip": item.address,
                    "first_seen": format_pdns_timestamp(item.first_seen),
                    "last_seen": format_pdns_timestamp(item.last_seen),
                    "fqdn": item.hostname,
                }
                for item in ranges
            ],
            args.output,
            ["ip", "first_seen", "last_seen", "fqdn"],
            args.quiet,
        )
    elif not args.quiet:
        print("ip,first_seen,last_seen,fqdn")
        for item in ranges:
            print(
                f"{item.address},{format_pdns_timestamp(item.first_seen)},"
                f"{format_pdns_timestamp(item.last_seen)},{item.hostname}"
            )


def select_pdns_entries(result: list[Any], one_per_day: bool) -> list[Any]:
    """Select passive-DNS records, optionally limiting downloads to one per WARC day.

    Args:
        result: Detailed FQDN listing records containing WARC filenames.
        one_per_day: Select only the first record for each date encoded in its WARC filename.

    Returns:
        Original records when ``one_per_day`` is false, otherwise one record per WARC day.
    """
    if not one_per_day:
        return result
    selected = []
    seen_days = set()
    for entry in result:
        filename = str(entry.get("warc_filename", ""))
        match = re.search(r"CC-MAIN-(\d{8})", filename)
        day = match.group(1) if match else filename
        if day in seen_days:
            continue
        seen_days.add(day)
        selected.append(entry)
    return selected


def _render_sha1_result(result: list[Any], args: Any, info_only: bool = False) -> None:
    """Render resumed SHA-1 records.

    Args:
        result: SHA-1 result records.
        args: Current CLI arguments.
        info_only: Force metadata-only output.
    """
    resumed_args = copy(args)
    resumed_args.info_only = info_only or args.info_only
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in result],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
        return
    if not args.quiet and not args.detail:
        for entry in result:
            print(LOCAL.format_instance(entry))
    if args.detail or args.show_headers or args.info_only:
        render(result, resumed_args, show_url=args.detail)


def _render_query_result(result: list[Any], args: Any, info_only: bool = False) -> None:
    """Render resumed exact-URL records.

    Args:
        result: Exact-URL result records.
        args: Current CLI arguments.
        info_only: Force metadata-only output.
    """
    resumed_args = copy(args)
    resumed_args.info_only = info_only or args.info_only
    render(result, resumed_args)


def _render_json_list(result: list[Any], args: Any) -> None:
    """Print an unknown resumed list as JSON.

    Args:
        result: Unknown JSON list.
        args: Current CLI arguments.
    """
    if not args.quiet:
        print_json_result(result)


def render_resumed_result(action: str, result: Any, args: Any) -> None:
    """Render a resumed result using its safe client action.

    Args:
        action: Safe client action stored with the job.
        result: JSON-compatible terminal result.
        args: Current CLI arguments controlling output and download.
    """
    handlers = {
        "domain-enum": _render_domain_result,
        "enumerate-url": _render_occurrence_result,
        "string-search": _render_occurrence_result,
        "list-fqdn": _render_occurrence_result,
        "list-domain": _render_domain_listing,
        "list-fqdn-detail": _render_detail_listing,
        "list-domain-detail": _render_detail_listing,
        "pdns": render_pdns_result,
        "sha1": _render_sha1_result,
        "sha1-info": lambda values, options: _render_sha1_result(
            values, options, info_only=True
        ),
        "query": _render_query_result,
        "query-info": lambda values, options: _render_query_result(
            values, options, info_only=True
        ),
    }
    if not isinstance(result, list):
        if not args.quiet:
            print_json_result(result)
    elif not result:
        if not args.quiet:
            print("Job completed with no results.")
    else:
        handlers.get(action, _render_json_list)(result, args)


def resume_result(job_id: str, args: Any) -> None:
    """Resume one job and render or download its terminal result.

    Args:
        job_id: Queue job identifier.
        args: Current CLI arguments controlling progress and output.
    """
    reporter = LOCAL.SearchProgress(True, args.verbose)
    status, result = wait_for_result(
        job_id,
        reporter,
        return_terminal_error=True,
        wait_for_active=False,
    )
    if status["state"] in {"WAITING", "RUNNING", "CANCEL_REQUESTED"}:
        if not args.quiet:
            print_job_status(status)
        return
    if status["state"] in {"ERROR", "CANCELLED"}:
        request_api(f"/jobs/{job_id}", method="DELETE")
        raise RuntimeError(
            status.get("error") or f"backend job {status['state'].lower()}"
        )
    render_resumed_result(
        str(status.get("client_action", status.get("operation", ""))),
        result,
        args,
    )
    request_api(f"/jobs/{job_id}", method="DELETE")


def flush_jobs() -> None:
    """Cancel all active jobs owned by the configured client token."""
    print_flush_result(request_api("/jobs", method="DELETE").json())


def read_download(response: Any, total: int, label: str) -> bytes:
    """Read a streamed response while reporting progress.

    Args:
        response: Streamed HTTP response.
        total: Expected byte count.
        label: Progress label.

    Returns:
        Complete response body.
    """
    return engine_read_download(response, total, label, LOGGER)


def direct_payload(entry: dict[str, Any]) -> tuple[bytes, str, str]:
    """Download and decode a WARC range after a ``204`` object response.

    Args:
        entry: Search record containing WARC location fields.

    Returns:
        Payload bytes, WARC headers, and HTTP headers.
    """
    return engine_direct_payload(entry, REQUEST_TIMEOUT, LOGGER)


def cache_payload(
    entry: dict[str, Any], payload: bytes, warc_headers: str, http_headers: str
) -> None:
    """Store a directly downloaded object in the server SQLite cache.

    Args:
        entry: Search record containing WARC location fields.
        payload: Uncompressed WARC response body.
        warc_headers: Parsed WARC record headers.
        http_headers: Parsed HTTP response headers.

    Raises:
        RuntimeError: If the server rejects the cache update.
    """
    params = {
        "file": entry["warc_filename"],
        "offset": entry["warc_record_offset"],
        "length": entry["warc_record_length"],
        "capability": entry.get("object_capability", ""),
    }
    headers = {
        "Content-Type": "application/octet-stream",
        "X-WARC-Headers": base64.b64encode(warc_headers.encode("utf-8")).decode(
            "ascii"
        ),
        "X-HTTP-Headers": base64.b64encode(http_headers.encode("utf-8")).decode(
            "ascii"
        ),
    }
    request_api(
        "/getobject/cache",
        method="POST",
        params=params,
        data=payload,
        headers=headers,
    )


def get_payload(
    entry: dict[str, Any],
    show_headers: bool = False,
    logger: logging.Logger | None = None,
) -> None:
    """Populate a record using the unified object endpoint.

    Args:
        entry: Search record to populate in place.
        show_headers: Request headers separately when true.
        logger: Optional logger replacing normal object diagnostics.

    Raises:
        RuntimeError: If direct or server-side download fails.
    """
    active_logger = logger or LOGGER
    direct_fetch = direct_payload
    stream_reader = read_download
    if logger is not None:
        direct_fetch = partial(
            engine_direct_payload,
            timeout=REQUEST_TIMEOUT,
            logger=active_logger,
        )
        stream_reader = partial(engine_read_download, logger=active_logger)
    context = RemoteObjectContext(
        request_api,
        direct_fetch,
        stream_reader,
        active_logger,
        cache_payload,
    )
    populate_remote_payload(entry, show_headers, context)


def render(entries: list[dict[str, Any]], args: Any, show_url: bool = False) -> None:
    """Render records using the local client's output implementation.

    Args:
        entries: Search records.
        args: Parsed CLI arguments.
        show_url: Print URL before detailed metadata.
    """
    for entry in entries:
        if not args.info_only:
            get_payload(entry, args.show_headers)
        LOCAL.output_entry(entry, args, show_url=show_url)


def common_args(args: Any) -> dict[str, str]:
    """Build query arguments shared by all remote operations.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Service query argument mapping.
    """
    params = {
        "alldataset": "true",
        "year": str(args.year) if args.year is not None and not args.alltime else "",
        "info_only": "true",
        "show_headers": str(args.show_headers).lower(),
    }
    if args.time_range:
        params["after"], params["before"] = args.time_range.split(",")
    elif args.at:
        params["after"] = args.at[:8]
        params["before"] = args.at[:8]
        if len(args.at) == 14:
            params["at"] = args.at
    else:
        params["after"] = args.after or ""
        params["before"] = args.before or ""
    return params


# pylint: disable=too-many-branches,too-many-return-statements,too-many-statements
# One dispatcher preserves exact CLI operation precedence and terminal actions.
def run(args: Any) -> None:
    """Execute the selected remote CLI operation.

    Args:
        args: Parsed CLI arguments.
    """
    progress = LOCAL.SearchProgress(
        args.quiet,
        args.verbose,
        since=progress_since(args),
    )
    if args.async_mode:
        operation, arguments = build_job_request(args)
        print(submit_async(operation, arguments))
        return
    if args.pdns:
        entries = submit(
            "list-fqdn",
            {
                **common_args(args),
                "fqdn": args.pdns,
                "detail": "true",
                "info_only": "true",
                "limit": str(args.limit),
                "all": "true",
            },
            progress,
        )
        render_pdns_result(entries, args)
        return
    if args.string_search:
        entries = submit(
            "query",
            {
                **common_args(args),
                "url_contains": args.string_search,
                "url_contains_fields": LOCAL.selected_string_search_fields(args),
                "tld": args.tld or "",
                "info_only": "true",
                "all": "true",
                "limit": str(args.limit),
            },
            progress,
        )
        if not entries and not args.quiet:
            print("No matching records found.")
        if args.output:
            save_csv_rows(
                [occurrence_csv_row(entry) for entry in entries],
                args.output,
                ["timestamp", "digest", "url"],
                args.quiet,
            )
        elif not args.quiet:
            for entry in entries:
                print(LOCAL.format_instance(entry))
        return
    if args.enumerate_url:
        entries = submit(
            "query",
            {
                **common_args(args),
                "url": args.enumerate_url,
                "info_only": "true",
                "all": "true",
            },
            progress,
        )
        if not entries and not args.quiet:
            print("No matching records found.")
        if args.output:
            save_csv_rows(
                [occurrence_csv_row(entry) for entry in entries],
                args.output,
                ["timestamp", "digest", "url"],
                args.quiet,
            )
        elif not args.quiet:
            for entry in entries:
                print(LOCAL.format_instance(entry))
        return
    if args.domain_enumeration:
        values = submit(
            "domain-enum",
            {**common_args(args), "domain": args.domain_enumeration, "all": "true"},
            progress,
        )
        if not values and not args.quiet:
            print(f"No matching records found for domain {args.domain_enumeration}")
        if args.output:
            save_csv_rows(
                [{"fqdn": value} for value in values],
                args.output,
                ["fqdn"],
                args.quiet,
            )
            return
        for value in values:
            if not args.quiet:
                print(value)
        return
    if args.list_fqdn or args.list_domain:
        key = "fqdn" if args.list_fqdn else "domain"
        endpoint = "list-fqdn" if key == "fqdn" else "list-domain"
        value = args.list_fqdn or args.list_domain
        entries = submit(
            endpoint,
            {
                **common_args(args),
                key: value,
                "detail": str(key in {"fqdn", "domain"} or args.detail).lower(),
                "all": "true",
            },
            progress,
        )
        if not entries and not args.quiet:
            print(f"No records found for {value}")
        if args.output:
            if args.detail:
                save_csv_rows(
                    entries,
                    args.output,
                    list(entries[0]) if entries else [],
                    args.quiet,
                )
            elif key in {"fqdn", "domain"}:
                save_csv_rows(
                    [occurrence_csv_row(entry) for entry in entries],
                    args.output,
                    ["timestamp", "digest", "url"],
                    args.quiet,
                )
            else:
                save_csv_rows(
                    [{"url": entry.get("url", "")} for entry in entries],
                    args.output,
                    ["url"],
                    args.quiet,
                )
        elif args.detail:
            render(entries, args, show_url=True)
        elif key in {"fqdn", "domain"} and not args.quiet:
            for entry in entries:
                print(LOCAL.format_instance(entry))
        elif not args.quiet:
            for entry in entries:
                print(entry.get("url", ""))
        return
    if args.sha1:
        entries = submit("sha1", {**common_args(args), "digest": args.sha1}, progress)
        if not entries and not args.quiet:
            print(f"No matching records found for digest {args.sha1}")
        if args.output:
            render(entries[:1], args)
            return
        if not args.quiet and not args.detail:
            for entry in entries:
                print(LOCAL.format_instance(entry))
        if args.detail or args.show_headers or args.info_only:
            render(entries, args, show_url=args.detail)
        return
    if args.url:
        entries = submit(
            "query",
            {**common_args(args), "url": args.url, "all": str(args.all).lower()},
            progress,
        )
        if not entries and not args.quiet:
            print("No matching records found.")
        render(entries, args)
        return
    raise SystemExit("Error: URL, -s, -pdns, -l, -ld, -de, or -1 must be specified")


def main() -> None:
    """Run the API-only remote client."""
    args = parse_args()
    if args.time_range and (args.after or args.before):
        raise SystemExit("Use --time-range or --after/--before, not both")
    has_other_time_modifier = any(
        (args.after, args.before, args.time_range, args.year is not None, args.alltime)
    )
    if args.at and has_other_time_modifier:
        raise SystemExit("Use --at/--on without other time modifiers")
    if args.after and args.before and args.after > args.before:
        raise SystemExit("--after must not exceed --before")
    if args.list_domain and ("://" in args.list_domain or "/" in args.list_domain):
        raise SystemExit("--list-domain expects a domain suffix, not a URL")
    if args.pdns and ("://" in args.pdns or "/" in args.pdns):
        raise SystemExit("-pdns expects an FQDN, not a URL")
    if args.full and not args.pdns:
        raise SystemExit("--full requires -pdns")
    LOCAL.configure_logging(args.quiet, args.verbose)
    HTTP_CLIENT.configure_json_trace(args.verbose >= 3)
    if not TOKEN:
        raise SystemExit("CCWGET_TOKEN is required")
    control_actions = sum(
        (
            bool(args.status_job),
            bool(args.result_job),
            bool(args.list_jobs),
            bool(args.flush_jobs),
        )
    )
    search_actions = any(
        (
            args.url,
            args.string_search,
            args.pdns,
            args.enumerate_url,
            args.domain_enumeration,
            args.list_fqdn,
            args.list_domain,
            args.sha1,
        )
    )
    if control_actions > 1 or (control_actions and (search_actions or args.async_mode)):
        raise SystemExit(
            "-status, -result, -jobs, and -flush cannot be combined with searches or -async"
        )
    try:
        if args.status_job:
            show_job_status(args.status_job)
            return
        if args.result_job:
            resume_result(args.result_job, args)
            return
        if args.list_jobs:
            show_jobs()
            return
        if args.flush_jobs:
            flush_jobs()
            return
        LOCAL.apply_server_metadata(args)
        LOCAL.apply_default_timeframe(args)
        run(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
