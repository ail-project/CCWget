#!/usr/bin/env python3
"""Customer CLI for Common Crawl search through the backend HTTP service."""

# pylint: disable=invalid-name,wrong-import-position
# Script-path bootstrap must run before shared root-package imports.

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_client_config
from lib.http import request_json as shared_request_json
from lib.engine import decode_payload
from lib.printout import (
    SearchProgress,
    TimeValidators,
    build_client_parser,
    format_instance,
    occurrence_csv_row,
    output_entry,
    print_async_help,
    print_text_search_help,
    print_time_help,
    save_csv_rows,
)
from lib.printout import time
from lib.url import normalize_client_url

__all__ = (
    "SearchProgress",
    "decode_payload",
    "format_instance",
    "output_entry",
    "time",
)

CLIENT_CONFIG = load_client_config()
SERVICE_URL = CLIENT_CONFIG["service_url"]
MIN_YEAR = 2015
YEAR_FLOOR = 1900
REQUEST_TIMEOUT = 300


def valid_year(value: str) -> int:
    """Validate CLI year argument.

    Args:
        value: Candidate year text.

    Returns:
        Parsed year.

    Raises:
        argparse.ArgumentTypeError: If year is outside supported range.
    """
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--year must be an integer") from exc
    current_year = datetime.now().year
    if year < YEAR_FLOOR or year > current_year:
        raise argparse.ArgumentTypeError(
            f"--year must be between {YEAR_FLOOR} and {current_year}"
        )
    return year


def valid_date(value: str) -> str:
    """Validate and normalize a six- or eight-digit date.

    Args:
        value: Date in ``YYMMDD`` or ``YYYYMMDD`` form.

    Returns:
        Date normalized to ``YYYYMMDD``.

    Raises:
        argparse.ArgumentTypeError: If the date is invalid.
    """
    formats = ("%y%m%d", "%Y%m%d")
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).strftime("%Y%m%d")
        except ValueError:
            continue
    raise argparse.ArgumentTypeError("date must use YYMMDD or YYYYMMDD")


def valid_time_range(value: str) -> str:
    """Validate and normalize a comma-separated date range.

    Args:
        value: Range in ``YYMMDD,YYMMDD`` or eight-digit equivalent form.

    Returns:
        Normalized ``YYYYMMDD,YYYYMMDD`` range.

    Raises:
        argparse.ArgumentTypeError: If the range is malformed or reversed.
    """
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--time-range must be START,END")
    start, end = valid_date(parts[0]), valid_date(parts[1])
    if start > end:
        raise argparse.ArgumentTypeError("--time-range start must not exceed end")
    return f"{start},{end}"


def valid_crawl_instant(value: str) -> str:
    """Validate and normalize one crawl date or timestamp.

    Args:
        value: Date in existing ``YYMMDD`` or ``YYYYMMDD`` form, or ISO datetime.

    Returns:
        ``YYYYMMDD`` for date input or ``YYYYMMDDHHMMSS`` for datetime input.

    Raises:
        argparse.ArgumentTypeError: If the value is not a valid date or timestamp.
    """
    try:
        return valid_date(value)
    except argparse.ArgumentTypeError:
        pass
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date must use YYMMDD, YYYYMMDD, or ISO datetime"
        ) from exc
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    return observed.strftime("%Y%m%d%H%M%S")


def valid_domain(value: str) -> str:
    """Validate a domain suffix with two to five DNS labels.

    Args:
        value: Domain suffix such as ``example.org`` or ``.example.org``.

    Returns:
        Original domain suffix.

    Raises:
        argparse.ArgumentTypeError: If value is a URL or invalid domain.
    """
    candidate = value.strip().strip(".")
    labels = candidate.split(".")
    if "://" in value or "/" in value or not 2 <= len(labels) <= 5:
        raise argparse.ArgumentTypeError("domain must contain 2 to 5 labels, not a URL")
    if any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise argparse.ArgumentTypeError("domain contains an invalid label")
    return value


def normalize_and_validate_url(value: str, quiet: bool) -> str:
    """Normalize a URL or valid bare FQDN and reject other positional values.

    Args:
        value: Positional URL or bare FQDN supplied by the user.
        quiet: Suppress the bare-FQDN normalization warning when true.

    Returns:
        Normalized URL.

    Raises:
        ValueError: If a schemeless value is not a valid FQDN.
    """
    normalized, bare_fqdn = normalize_client_url(value, valid_domain)
    if not bare_fqdn and "://" not in value:
        raise ValueError("URL must be an http(s) URL or a valid FQDN")
    if bare_fqdn and not quiet:
        print(
            f"WARNING: bare FQDN normalized; searching {normalized}",
            file=sys.stderr,
        )
    return normalized


def normalize_tld(value: str) -> str:
    """Normalize one TLD suffix for substring search.

    Args:
        value: User-supplied TLD with optional leading dot.

    Returns:
        Lowercase TLD with one leading dot.

    Raises:
        argparse.ArgumentTypeError: If the TLD contains invalid labels.
    """
    candidate = value.strip().lstrip(".").lower()
    labels = candidate.split(".")
    if not candidate or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise argparse.ArgumentTypeError("TLD must contain valid domain labels")
    return f".{candidate}"


def parse_args() -> argparse.Namespace:
    """Parse local-client-compatible command-line arguments.

    Returns:
        Parsed CLI namespace.
    """
    validators = TimeValidators(
        valid_year, valid_date, valid_crawl_instant, valid_time_range
    )
    parser = build_client_parser(validators, valid_domain, MIN_YEAR)
    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit(0)
    args = parser.parse_args()
    if args.url:
        try:
            args.url = normalize_and_validate_url(args.url, args.quiet)
        except ValueError as exc:
            parser.error(str(exc))
    if args.string_search is not None and len(args.string_search) < 4:
        parser.error("--string-search requires at least 4 characters")
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.tld and not args.string_search:
        parser.error("--tld requires --string-search")
    if (args.fqdn or args.path or args.query) and not args.string_search:
        parser.error("--fqdn, --path, and --query require --string-search")
    if args.tld:
        try:
            args.tld = normalize_tld(args.tld)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    if args.help_async:
        print_async_help(parser.prog)
        parser.exit(0)
    if args.help_time:
        print_time_help(
            parser.prog,
            validators,
            MIN_YEAR,
        )
        parser.exit(0)
    if args.help_textsearch:
        print_text_search_help(parser.prog)
        parser.exit(0)
    return args


def configure_logging(quiet: bool, verbose: int = 0) -> None:
    """Configure client logging.

    Args:
        quiet: Suppress informational logs when true.
        verbose: Verbosity count; two enables HTTP debug logs, three enables JSON response tracing in the remote client.
    """
    logging.basicConfig(
        level=(
            logging.ERROR
            if quiet or verbose == 0
            else (logging.DEBUG if verbose >= 2 else logging.INFO)
        ),
        format="%(levelname)s: %(message)s",
    )


def two_years_before(current_date: date) -> date:
    """Return calendar date two years before a given date.

    Args:
        current_date: Date used as upper search boundary.

    Returns:
        Same month and day two years earlier, or February 28 for a leap day.
    """
    try:
        return current_date.replace(year=current_date.year - 2)
    except ValueError:
        return current_date.replace(year=current_date.year - 2, day=28)


def apply_default_timeframe(
    args: argparse.Namespace, current_date: date | None = None
) -> None:
    """Apply a two-year search period when user supplied none.

    Args:
        args: Parsed CLI arguments modified in place.
        current_date: Optional date used by deterministic tests.
    """
    has_timeframe = bool(
        args.alltime
        or args.year is not None
        or args.after
        or args.before
        or args.at
        or args.time_range
    )
    if has_timeframe:
        return
    period_end = current_date or date.today()
    period_start = two_years_before(period_end)
    args.after = period_start.strftime("%Y%m%d")
    args.before = period_end.strftime("%Y%m%d")


def apply_server_metadata(args: argparse.Namespace) -> None:
    """Load indexed-dataset metadata used by all-time progress reporting.

    Args:
        args: Parsed CLI arguments modified in place.
    """
    try:
        metadata = request_json("/metadata", {})
        minimum_year = int(metadata["minimum_year"])
    except (KeyError, TypeError, ValueError, RuntimeError):
        return
    if YEAR_FLOOR <= minimum_year <= datetime.now().year:
        args.minimum_year = minimum_year


def request_json(endpoint: str, params: dict[str, Any]) -> Any:
    """Request JSON result from backend service.

    Args:
        endpoint: Backend route beginning with `/`.
        params: Query parameters sent to backend.

    Returns:
        Decoded JSON response.

    Raises:
        RuntimeError: If backend request fails or returns invalid JSON.
    """
    return shared_request_json(
        SERVICE_URL,
        endpoint,
        params,
        timeout=REQUEST_TIMEOUT,
        logger=logging.getLogger(__name__),
        token=CLIENT_CONFIG["token"],
    )


def common_params(args: argparse.Namespace) -> dict[str, str]:
    """Build common backend table-selection parameters.

    Args:
        args: Parsed CLI options.

    Returns:
        Query parameter mapping.
    """
    params = {
        "alldataset": "true",
        "year": str(args.year) if args.year is not None and not args.alltime else "",
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


def query_url(args: argparse.Namespace) -> None:
    """Query URL through backend and render records.

    Args:
        args: Parsed CLI options.
    """
    params = common_params(args)
    params.update(
        {
            "url": args.url,
            "info_only": str(args.info_only).lower(),
            "show_headers": str(args.show_headers).lower(),
            "all": str(args.all).lower(),
        }
    )
    results = request_json("/query", params)
    if not results:
        if not args.quiet:
            print("No matching records found.")
        return
    for entry in results:
        output_entry(entry, args)


def selected_string_search_fields(args: argparse.Namespace) -> str:
    """Return selected URL fields for substring search.

    Args:
        args: Parsed CLI args containing field selector flags.

    Returns:
        Comma-separated backend field names, or empty for full-URL compatibility mode.
    """
    fields = []
    if args.fqdn:
        fields.append("fqdn")
    if args.path:
        fields.append("path")
    if args.query:
        fields.append("query")
    return ",".join(fields)


def string_search(args: argparse.Namespace) -> None:
    """List all indexed URLs containing one substring.

    Args:
        args: Parsed CLI options containing substring and optional TLD.
    """
    params = common_params(args)
    params.update(
        {
            "url_contains": args.string_search,
            "url_contains_fields": selected_string_search_fields(args),
            "tld": args.tld or "",
            "all": "true",
            "limit": str(args.limit),
        }
    )
    results = request_json("/query", params)
    if not results:
        if not args.quiet:
            print("No matching records found.")
        return
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in results],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
    elif not args.quiet:
        for entry in results:
            print(format_instance(entry))


def list_records(args: argparse.Namespace, endpoint: str, key: str) -> None:
    """List FQDN or domain records through backend.

    Args:
        args: Parsed CLI options.
        endpoint: Backend listing route.
        key: Request parameter name.
    """
    value = args.list_fqdn if key == "fqdn" else args.list_domain
    params = common_params(args)
    detailed = key in {"fqdn", "domain"} or args.detail
    params.update({key: value, "detail": str(detailed).lower(), "all": "true"})
    results = request_json(endpoint, params)
    if not results:
        if not args.quiet:
            print(f"No records found for {value}")
        return
    if args.output:
        if key in {"fqdn", "domain"} and not args.detail:
            save_csv_rows(
                [occurrence_csv_row(entry) for entry in results],
                args.output,
                ["timestamp", "digest", "url"],
                args.quiet,
            )
        elif args.detail:
            save_csv_rows(results, args.output, list(results[0]), args.quiet)
        else:
            save_csv_rows(
                [{"url": entry.get("url", "")} for entry in results],
                args.output,
                ["url"],
                args.quiet,
            )
        return
    for entry in results:
        if args.quiet:
            continue
        if key in {"fqdn", "domain"} and not args.detail:
            print(format_instance(entry))
        elif args.detail:
            output_entry(entry, args, show_url=True)
        else:
            print(entry.get("url", ""))


def enumerate_domain(args: argparse.Namespace) -> None:
    """Enumerate FQDNs under domain through backend.

    Args:
        args: Parsed CLI options.
    """
    params = common_params(args)
    params.update({"domain": args.domain_enumeration, "all": "true"})
    results = request_json("/domain-enum", params)
    if args.output:
        save_csv_rows(
            [{"fqdn": host} for host in results],
            args.output,
            ["fqdn"],
            args.quiet,
        )
        return
    for host in results:
        if not args.quiet:
            print(host)


def query_sha1(args: argparse.Namespace) -> None:
    """Search content digest through backend.

    Args:
        args: Parsed CLI options.
    """
    params = common_params(args)
    params.update(
        {
            "digest": args.sha1,
            "info_only": str(args.info_only).lower(),
            "show_headers": str(args.show_headers).lower(),
        }
    )
    results = request_json("/sha1", params)
    if not results:
        if not args.quiet:
            print(f"No matching records found for digest {args.sha1}")
        return
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in results],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
        return
    for entry in results:
        if not args.quiet and not args.detail:
            print(format_instance(entry))
        if args.detail or args.show_headers or args.info_only:
            output_entry(entry, args, show_url=args.detail)


def enumerate_url(args: argparse.Namespace) -> None:
    """Print every indexed occurrence of one exact URL.

    Args:
        args: Parsed CLI options.
    """
    params = common_params(args)
    params.update({"url": args.enumerate_url, "info_only": "true", "all": "true"})
    results = request_json("/query", params)
    if not results:
        if not args.quiet:
            print("No matching records found.")
        return
    if args.output:
        save_csv_rows(
            [occurrence_csv_row(entry) for entry in results],
            args.output,
            ["timestamp", "digest", "url"],
            args.quiet,
        )
    elif not args.quiet:
        for entry in results:
            print(format_instance(entry))


# CLI dispatcher keeps mutually exclusive legacy and remote-compatible actions together.
# pylint: disable=too-many-branches
def main() -> None:
    """Run customer CLI using backend service only."""
    args = parse_args()
    remote_only = any(
        (
            args.async_mode,
            args.status_job,
            args.result_job,
            args.list_jobs,
            args.flush_jobs,
            args.pdns,
            args.full,
        )
    )
    if remote_only:
        raise SystemExit(
            "-async, -status, -result, -jobs, -flush, -pdns, and --full require "
            "ccwget.py"
        )
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
    configure_logging(args.quiet, args.verbose)
    apply_server_metadata(args)
    apply_default_timeframe(args)
    try:
        if args.string_search:
            string_search(args)
        elif args.enumerate_url:
            enumerate_url(args)
        elif args.domain_enumeration:
            enumerate_domain(args)
        elif args.list_fqdn:
            list_records(args, "/list-fqdn", "fqdn")
        elif args.list_domain:
            list_records(args, "/list-domain", "domain")
        elif args.sha1:
            query_sha1(args)
        elif args.url:
            query_url(args)
        else:
            raise SystemExit("Error: URL, -s, -l, -ld, -de, or -1 must be specified")
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
