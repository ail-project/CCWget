"""Command-line help, progress, and output helpers."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
from contextlib import nullcontext
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, NamedTuple, TextIO
from urllib.parse import unquote

from lib.engine import decode_payload

PROGRESS_WIDTH = 27
SECTION_WIDTH = 60
WARC_DATE_PATTERN = re.compile(r"CC-MAIN-(\d{8})")
WARC_TIMESTAMP_PATTERN = re.compile(r"CC-MAIN-(\d{14})")
CLI_ASCII_ART = r"""
###########   XXXX    ##########      ___
####### XXXXXXXXXXXXXXXX #######    / ___|___  _ __ ___  _ __ ___   ___  _ __
#### XXXXXXXXXXXXXXXXXXXXXX ####   | |   / _ \| '_ ` _ \| '_ ` _ \ / _ \| '_ \
#####  XXXXXXXXXXXXXXXXXXXX ####   | |__| (_) | | | | | | | | | | | (_) | | | |
#####     XXXXXXXXXXXXXXXX #####    \____\___/|_| |_| |_|_| |_| |_|\___/|_| |_|
####          XXXXXXXXXXX   ####          / ___|_ __ __ ___      _| |
XXXXXXXXXXX     XXXXXXX      X #         | |   | '__/ _` \ \ /\ / / |
XXXXXXXXXXX         XX    XXXXX#         | |___| | | (_| |\ V  V /| |
XXXXXXXXXXXX             XXXXXXX          \____|_|  \__,_| \_/\_/ |_|  _
XXXXXXXXXXX        XXXXXXXXXXXX#                  __      __/ ___| ___| |_
XXXXXXXXXXX    XXXXXXXXXXXXXXX #                  \ \ /\ / / |  _ / _ \ __|
# XXXXXXXXXX  XXXXXXXXXXXXXXXXX#                   \ V  V /| |_| |  __/ |_
## XXXXXXXXX   XXXXXXXXXXXXXXX##                    \_/\_/  \____|\___|__|
### XXXXXXXX     XXXXXXXXXXX ###
##### XXXXXX      XXXXXXXX #####    CIRCL.LU
######## XXX ##### XXXX  #######
"""
RED = "\033[31m"
RESET = "\033[0m"


def colored_ascii_art() -> str:
    """Return the banner with X characters red in interactive terminals.

    Returns:
        Banner text, colorized only when standard output is a terminal.
    """
    if not sys.stdout.isatty():
        return CLI_ASCII_ART
    return re.sub(r"X+", lambda match: f"{RED}{match.group(0)}{RESET}", CLI_ASCII_ART)


class ClientHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep option metavariables stable across supported Python versions."""

    def _format_action_invocation(self, action: argparse.Action) -> str:
        """Format every value-taking option with its metavar.

        Args:
            action: Argument parser action to format.

        Returns:
            Stable option invocation text.
        """
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        default = self._get_default_metavar_for_optional(action)
        arguments = self._format_args(action, default)
        return ", ".join(f"{option} {arguments}" for option in action.option_strings)


class TimeValidators(NamedTuple):
    """Validation callbacks used by search-time arguments."""

    year: Callable[[str], int]
    date: Callable[[str], str]
    instant: Callable[[str], str]
    date_range: Callable[[str], str]


# pylint: disable=too-many-instance-attributes  # Renderer stores output, timeframe, and progress state.
class SearchProgress:
    """Render default search progress without changing result output."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    # Progress options are kept together to preserve existing positional stream API.
    def __init__(
        self,
        quiet: bool,
        verbose: int,
        stream: TextIO | None = None,
        message: str = "Digging into",
        object_count: int | None = None,
        since: str | None = None,
    ) -> None:
        """Create a progress renderer.

        Args:
            quiet: Disable all progress output when true.
            verbose: Verbosity count; verbose modes use log output instead.
            stream: Output stream used for the default progress bar.
            message: Progress message prefix.
            object_count: Object count shown in the progress message.
            since: Inclusive start date shown in the progress message.
        """
        self.enabled = not quiet and verbose == 0
        self.quiet = quiet
        self.verbose = verbose
        self.stream = stream or sys.stderr
        self.message = message
        self.object_count = object_count
        self.since = self._format_since(since)
        self.started = time.monotonic()
        self.last_text = ""
        self.dataset_rows: int | None = None

    @staticmethod
    def _format_since(value: str | None) -> str:
        """Format compact date text as an ISO calendar date.

        Args:
            value: Date as ``YYYYMMDD`` or already formatted text.

        Returns:
            ISO date text, or an empty string when absent.
        """
        if not value:
            return ""
        compact = value.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        return value

    @staticmethod
    def _bar(completed: int, total: int) -> str:
        """Build a 20-block table progress bar.

        Args:
            completed: Number of completed tables.
            total: Number of selected tables.

        Returns:
            Formatted progress bar and percentage.
        """
        percent = (
            100
            if total and completed >= total
            else int(completed * 100 / total) if total else 0
        )
        filled = (
            20 if percent == 100 else min(20, completed * 20 // total) if total else 0
        )
        return f"[{('█' * filled) + (' ' * (20 - filled))}] {percent}%"

    def update(
        self,
        state: str,
        position: int | None = None,
        completed: int = 0,
        total: int = 0,
        total_rows: int | None = None,
        detail: str = "",
    ) -> None:
        """Render one backend job status update.

        Args:
            state: Backend state such as WAITING, RUNNING, or DONE.
            position: Waiting-job ordinal supplied by the backend.
            completed: Completed physical-table count.
            total: Selected physical-table count.
            total_rows: Cached rows in selected physical tables.
            detail: Optional backend page-progress detail retained for API compatibility.
        """
        if total_rows is not None and total_rows != self.dataset_rows:
            self.dataset_rows = total_rows
        if not self.enabled:
            return
        if state == "WAITING":
            elapsed = int(time.monotonic() - self.started)
            queue_position = position + 1 if position is not None else "?"
            text = f"[ WAIT - {queue_position} in Queue ({elapsed}sec) ]"
        elif state == "ERROR":
            text = "[ ERROR ]"
        else:
            count = self.object_count
            if count is None:
                count = (
                    self.dataset_rows if self.dataset_rows is not None else total_rows
                )
            if self.message == "Digging into":
                suffix = (
                    "Digging into Objects"
                    if count is None
                    else f"Digging into {count:,} Objects"
                )
                if self.since:
                    suffix += f" since {self.since}"
            else:
                suffix = f"{self.message} {count:,} Objects..."
            text = f"{self._bar(completed, total)} {suffix}"
        if text == self.last_text:
            return
        display_width = max(PROGRESS_WIDTH, len(self.last_text), len(text))
        self.last_text = text
        print(f"\r{text.ljust(display_width)}", end="", file=self.stream, flush=True)

    def log_state(self, job_id: str, state: str, progress: int) -> None:
        """Log one verbose state update.

        Args:
            job_id: Backend job identifier.
            state: Backend job state.
            progress: Compatibility percentage for verbose logs.
        """
        logging.info("search %s state=%s (%d%%)", job_id, state, progress)

    def finish(self, state: str, total: int = 0, total_rows: int | None = None) -> None:
        """Finish progress output with a newline.

        Args:
            state: Final backend state.
            total: Selected physical-table count.
            total_rows: Cached object count for selected tables.
        """
        if not self.enabled:
            return
        if state == "DONE":
            final_total = total or 1
            self.update(
                "DONE",
                completed=final_total,
                total=final_total,
                total_rows=total_rows,
            )
        elif state == "ERROR":
            self.update("ERROR")
        print(file=self.stream, flush=True)


def add_remote_control_arguments(
    parser: argparse.ArgumentParser, show_help: bool = False
) -> None:
    """Add remote queue-control options and help text.

    Args:
        parser: Client argument parser to extend.
        show_help: Show option descriptions instead of hiding them.
    """
    parser.add_argument(
        "-async",
        "--async",
        dest="async_mode",
        action="store_true",
        help=(
            "Submit a remote job and print its ID without waiting"
            if show_help
            else argparse.SUPPRESS
        ),
    )
    parser.add_argument(
        "-status",
        "--status",
        dest="status_job",
        metavar="JOB_ID",
        help="Show one remote job status" if show_help else argparse.SUPPRESS,
    )
    parser.add_argument(
        "-result",
        "--result",
        dest="result_job",
        metavar="JOB_ID",
        help=(
            "Resume and consume one remote job result"
            if show_help
            else argparse.SUPPRESS
        ),
    )
    parser.add_argument(
        "-jobs",
        "--jobs",
        dest="list_jobs",
        action="store_true",
        help=(
            "List jobs submitted by the configured remote token"
            if show_help
            else argparse.SUPPRESS
        ),
    )
    parser.add_argument(
        "-flush",
        "--flush",
        dest="flush_jobs",
        action="store_true",
        help=(
            "Cancel all active jobs owned by the remote token"
            if show_help
            else argparse.SUPPRESS
        ),
    )


def print_async_help(program: str) -> None:
    """Print remote asynchronous job-control help.

    Args:
        program: Client executable name shown in usage.
    """
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            f"{colored_ascii_art()}\n"
            "Asynchronous remote jobs. Combine -async with one search from "
            f"`{program} --help`."
        ),
        formatter_class=ClientHelpFormatter,
    )
    add_remote_control_arguments(parser, show_help=True)
    parser.print_help()


def add_time_arguments(
    container: Any,
    validators: TimeValidators,
    minimum_year: int,
    show_help: bool = False,
) -> None:
    """Add search-time modifiers to one argument container.

    Args:
        container: Argument parser or argument group receiving options.
        validators: Year, date, and time-range validation callbacks.
        minimum_year: Oldest year accepted by client.
        show_help: Show option descriptions instead of hiding them.
    """
    hidden = not show_help
    container.add_argument(
        "--alltime",
        action="store_true",
        help=argparse.SUPPRESS if hidden else "Search all indexed data.",
    )
    container.add_argument(
        "--year",
        type=validators.year,
        metavar="YEAR",
        help=(
            argparse.SUPPRESS
            if hidden
            else f"Search one year between {minimum_year} and {datetime.now().year}"
        ),
    )
    container.add_argument(
        "--after",
        "--since",
        dest="after",
        type=validators.date,
        metavar="YYMMDD",
        help=argparse.SUPPRESS if hidden else "Include crawls on/after date",
    )
    container.add_argument(
        "--before",
        type=validators.date,
        metavar="YYMMDD",
        help=argparse.SUPPRESS if hidden else "Include crawls on/before date",
    )
    container.add_argument(
        "--on",
        "--at",
        dest="at",
        type=validators.instant,
        metavar="DATE_OR_TIME",
        help=(
            argparse.SUPPRESS if hidden else "Limit crawls to one date or ISO timestamp"
        ),
    )
    container.add_argument(
        "--time-range",
        type=validators.date_range,
        metavar="YYMMDD,YYMMDD",
        help=argparse.SUPPRESS if hidden else "Limit crawls to an inclusive date range",
    )


def print_time_help(
    program: str,
    validators: TimeValidators,
    minimum_year: int,
) -> None:
    """Print dedicated search-time modifier help.

    Args:
        program: Client executable name shown in usage.
        validators: Search-time validation callbacks.
        minimum_year: Oldest year accepted by client.
    """
    parser = argparse.ArgumentParser(
        prog=program,
        description=f"{colored_ascii_art()}\nCommon-Crawl search-time modifiers.",
        formatter_class=ClientHelpFormatter,
    )
    group = parser.add_argument_group("time modifiers")
    add_time_arguments(
        group,
        validators,
        minimum_year,
        show_help=True,
    )
    parser.print_help()


def print_text_search_help(program: str) -> None:
    """Print dedicated substring-search help and performance warnings.

    Args:
        program: Client executable name shown in examples.
    """
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            f"{colored_ascii_art()}\n"
            "Substring URL search with -s/--string-search.\n\n"
            "WARNING: substring search is extremely slow on large Common Crawl datasets. "
            "It may scan many physical tables and can run for a long time."
        ),
        epilog=(
            "Examples:\n"
            f"  {program} --after 20260701 -s circl --fqdn\n"
            f"  {program} --after 20260701 -s circl --fqdn --tld .lu --limit 100\n\n"
            "Use --fqdn for host names, --path for URL paths, and --query for URL query strings. "
            "Multiple selectors are combined with OR. --tld restricts the host suffix. "
            "--limit defaults to 500 and prevents unbounded result collection."
        ),
        formatter_class=ClientHelpFormatter,
    )
    group = parser.add_argument_group("text search options")
    group.add_argument(
        "-s", "--string-search", metavar="TEXT", help="Search TEXT as a substring"
    )
    group.add_argument("--fqdn", action="store_true", help="Search TEXT in FQDN/host")
    group.add_argument("--path", action="store_true", help="Search TEXT in URL path")
    group.add_argument("--query", action="store_true", help="Search TEXT in URL query")
    group.add_argument(
        "--tld", metavar="TLD", help="Restrict host suffix, for example .lu"
    )
    group.add_argument(
        "--limit", metavar="N", default="500", help="Maximum results (default: 500)"
    )
    parser.print_help()


def build_client_parser(
    validators: TimeValidators,
    domain_validator: Callable[[str], str],
    minimum_year: int,
) -> argparse.ArgumentParser:
    """Build complete local and remote client help.

    Args:
        validators: Search-time validation callbacks.
        domain_validator: Domain validation callback.
        minimum_year: Oldest year accepted by client.

    Returns:
        Configured shared client parser.
    """
    parser = argparse.ArgumentParser(
        description=(
            f"{colored_ascii_art()}\n"
            "Common-Crawl wget: query backend service and fetch archived content"
        ),
        formatter_class=ClientHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="URL to query")
    parser.add_argument(
        "-s",
        "--string-search",
        dest="string_search",
        metavar="TEXT",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-tld",
        "--tld",
        dest="tld",
        metavar="TLD",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fqdn",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--path",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--query",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Print progress; -vv for HTTP details; -vvv for JSON responses",
    )
    parser.add_argument(
        "-i",
        "--info",
        dest="info_only",
        action="store_true",
        help="Only print index records.",
    )
    parser.add_argument(
        "-O",
        "--output",
        metavar="file",
        default=None,
        help="Output file ('-' for stdout); listing modes write CSV",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download all matching archived files for an exact URL",
    )
    parser.add_argument(
        "-S", "--show-headers", action="store_true", help="Display WARC/HTTP headers"
    )
    parser.add_argument(
        "-l",
        "--list-fqdn",
        metavar="FQDN",
        type=domain_validator,
        help="List all pages for given FQDN",
    )
    parser.add_argument(
        "-pdns",
        metavar="FQDN",
        help="Extract timestamp:IP:hostname observations from WARC records",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Download every archived WARC object for passive DNS",
    )
    parser.add_argument(
        "-e",
        "--enumerate",
        dest="enumerate_url",
        metavar="URL",
        help="List all indexed occurrences of an exact URL (FQDN + path)",
    )
    parser.add_argument(
        "-ld",
        "--list-domain",
        metavar="DOMAIN",
        type=domain_validator,
        help="List all pages for given domain suffix (2 to 5 labels)",
    )
    parser.add_argument(
        "-d", "--detail", action="store_true", help="Detailed output for listing modes"
    )
    parser.add_argument(
        "-de",
        "--domain-enumeration",
        metavar="DOMAIN",
        type=domain_validator,
        help="Enumerate FQDNs under domain",
    )
    parser.add_argument(
        "-1", "--sha1", metavar="DIGEST", help="Search SHA-1 content digest"
    )
    add_remote_control_arguments(parser)
    parser.add_argument(
        "--help-async",
        action="store_true",
        help="Show asynchronous job-control help and exit",
    )
    parser.add_argument(
        "--help-time",
        action="store_true",
        help="Show search-time modifier help and exit",
    )
    parser.add_argument(
        "--help-textsearch",
        action="store_true",
        help="Show substring-search help and performance warning",
    )
    add_time_arguments(
        parser,
        validators,
        minimum_year,
    )
    return parser


def print_job_status(status: dict[str, Any], now: float | None = None) -> None:
    """Print one backend job status.

    Args:
        status: Safe job status mapping returned by the backend.
        now: Optional current Unix timestamp for deterministic output.
    """
    print(f"Job: {status['job_id']}")
    print(f"Operation: {status.get('operation', '-')}")
    if status.get("client_action") not in {None, status.get("operation")}:
        print(f"Action: {status['client_action']}")
    print(f"State: {status['state']}")
    if status.get("created"):
        current = now if now is not None else time.time()
        print(f"Elapsed: {max(0, int(current - status['created']))}sec")
    if status.get("position") is not None:
        print(f"Queue position: {status['position'] + 1}")
    print(
        f"Tables: {status.get('completed_tables', 0)}/"
        f"{status.get('total_tables', 0)}"
    )
    if status.get("total_tables", 0) > 0:
        print(f"Dataset rows: {status['total_rows']:,}")
    if status.get("error"):
        print(f"Error: {status['error']}")


def print_job_list(jobs: list[dict[str, Any]], now: float | None = None) -> None:
    """Print retained jobs in a compact table.

    Args:
        jobs: Safe job summaries returned by the backend.
        now: Optional current Unix timestamp for deterministic output.
    """
    if not jobs:
        print("No retained jobs found.")
        return
    current = now if now is not None else time.time()
    print(
        "Job ID                           Operation     State             Age     Queue  Tables"
    )
    print("-" * 91)
    for job in jobs:
        age = max(0, int(current - float(job.get("created", current))))
        queue = (
            str(int(job["position"]) + 1) if job.get("position") is not None else "-"
        )
        tables = f"{job.get('completed_tables', 0)}/{job.get('total_tables', 0)}"
        print(
            f"{job['job_id']:<32} {job.get('client_action', job.get('operation', '-')):<13} "
            f"{job['state']:<17} {age:>5}s {queue:>7} {tables:>7}"
        )
        if job.get("total_tables", 0) > 0:
            print(f"  Dataset rows: {job['total_rows']:,}")
        if job.get("error"):
            print(f"  Error: {job['error']}")


def print_flush_result(result: dict[str, Any]) -> None:
    """Print queue cancellation counts.

    Args:
        result: Backend flush response.
    """
    print(f"Waiting cancelled: {result['waiting_cancelled']}")
    print(f"Running cancelled: {result['running_cancelled']}")


def print_json_result(result: Any) -> None:
    """Print one fallback job result as readable JSON.

    Args:
        result: JSON-compatible backend job result.
    """
    print(json.dumps(result, indent=2, sort_keys=True))


def print_index_info(entry: dict[str, Any]) -> None:
    """Print metadata fields for one search result.

    Args:
        entry: Backend result record.
    """
    print_section("Indexed record")
    print()
    for key in (
        "url_protocol",
        "url_host_name",
        "url_path",
        "url_query",
        "content_digest",
        "content_languages",
        "content_mime_detected",
        "warc_filename",
        "warc_record_offset",
        "warc_record_length",
    ):
        if key in entry:
            print(f"{key}: {entry[key]}")


def print_headers(entry: dict[str, Any]) -> None:
    """Print WARC and HTTP headers from one result.

    Args:
        entry: Backend result record.
    """
    if entry.get("warc_headers"):
        print_section("WARC Headers")
        print(entry["warc_headers"])
    if entry.get("http_headers"):
        print_section("HTTP Headers")
        print(entry["http_headers"])


def print_section(title: str) -> None:
    """Print one fixed-width output section title.

    Args:
        title: Section name centered between dashes.
    """
    print("\n" + f" {title} ".center(SECTION_WIDTH, "-"))


def print_stored_data(destination: str) -> None:
    """Print destination section for a saved payload.

    Args:
        destination: Path where payload was stored.
    """
    print_section("Stored Data")
    print(f"Output file: {destination}")


def get_unique_filename(filename: str) -> str:
    """Return unused filename by adding numeric suffixes.

    Args:
        filename: Requested output path.

    Returns:
        Unused output path.
    """
    if not os.path.exists(filename):
        return filename
    base, extension = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = f"{base}.{counter}{extension}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def save_payload(payload: bytes, output_file: str, quiet: bool) -> None:
    """Write response body to stdout or a unique local file.

    Args:
        payload: Response body bytes.
        output_file: Destination path or ``-`` for stdout.
        quiet: Suppress save message when true.
    """
    if output_file == "-":
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return
    destination = get_unique_filename(output_file)
    if not quiet:
        print(f"Saving {destination}")
    Path(destination).write_bytes(payload)


def format_instance(entry: dict[str, Any]) -> str:
    """Format one indexed page occurrence.

    Args:
        entry: Backend record containing digest, URL, and WARC filename.

    Returns:
        ``YYYY-MM-DDTHH:MM:SS:SHA1:page`` formatted occurrence.

    Raises:
        ValueError: If WARC filename has no crawl date.
    """
    match = WARC_TIMESTAMP_PATTERN.search(str(entry.get("warc_filename", "")))
    if match is None:
        raise ValueError("WARC filename has no crawl timestamp")
    crawl_timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").isoformat()
    digest = str(entry.get("content_digest", ""))
    try:
        digest = base64.b32decode(digest).hex()
    except (binascii.Error, ValueError):
        digest = digest.lower()
    return f"{crawl_timestamp}:{digest}:{entry.get('url', '')}"


def occurrence_csv_row(entry: dict[str, Any]) -> dict[str, str]:
    """Convert one indexed occurrence into CSV columns.

    Args:
        entry: Backend record containing crawl, digest, and URL fields.

    Returns:
        CSV-compatible occurrence columns.
    """
    formatted = format_instance(entry)
    timestamp = formatted[:19]
    digest, url = formatted[20:].split(":", 1)
    return {"timestamp": timestamp, "digest": digest, "url": url}


def save_csv_rows(
    rows: list[dict[str, Any]],
    output_file: str,
    fieldnames: list[str],
    quiet: bool,
) -> None:
    """Save listing rows as a CSV document.

    Args:
        rows: Listing records to write.
        output_file: Destination path or ``-`` for standard output.
        fieldnames: CSV header and column order.
        quiet: Suppress the saved-file log message when true.
    """
    destination = (
        output_file if output_file == "-" else get_unique_filename(output_file)
    )
    with (
        nullcontext(sys.stdout)
        if destination == "-"
        else open(destination, "w", encoding="utf-8", newline="")
    ) as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(fieldnames)
        for row in rows:
            cells = []
            for fieldname in fieldnames:
                value = str(row.get(fieldname, ""))
                if fieldname == "url":
                    cells.append(f'"{value.replace(chr(34), chr(34) * 2)}"')
                    continue
                cell = io.StringIO()
                csv.writer(cell, lineterminator="").writerow([value])
                cells.append(cell.getvalue())
            stream.write(",".join(cells) + "\n")
    if not quiet:
        logging.info("Saved CSV listing to %s", destination)


def output_entry(entry: dict[str, Any], args: Any, show_url: bool = False) -> None:
    """Render one backend result and optionally retrieve its payload.

    Args:
        entry: Backend result record.
        args: Parsed CLI options.
        show_url: Print assembled URL before metadata when true.
    """
    if show_url:
        print(f"URL: {entry.get('url', '')}")
    if args.info_only or args.show_headers or args.detail:
        print_index_info(entry)
    if args.show_headers:
        print_headers(entry)
    if args.info_only:
        return
    try:
        payload = decode_payload(entry)
    except ValueError as exc:
        if not args.quiet:
            logging.error("%s", exc)
        return
    output_file = args.output
    if output_file is None:
        output_file = (
            os.path.basename(unquote(entry.get("url_path") or "index.html"))
            or "index.html"
        )
    if args.show_headers or args.detail:
        print_stored_data(output_file)
    save_payload(payload, output_file, args.quiet)
