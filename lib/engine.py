"""Common Crawl query-result and WARC download processing."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from io import BytesIO
import gzip
import ipaddress
import logging
from typing import Any, Callable, Iterable, NamedTuple

import requests
from warcio.archiveiterator import ArchiveIterator

RequestApi = Callable[..., requests.Response]
DirectFetch = Callable[[dict[str, Any]], tuple[bytes, str, str]]
StreamReader = Callable[[requests.Response, int, str], bytes]
CacheStore = Callable[[dict[str, Any], bytes, str, str], None]


class RemoteObjectContext(NamedTuple):
    """Callbacks and logger required for remote object retrieval."""

    request_api: RequestApi
    direct_fetch: DirectFetch
    stream_reader: StreamReader
    logger: logging.Logger
    cache_store: CacheStore | None = None


class PdnsObservation(NamedTuple):
    """One validated passive-DNS observation."""

    timestamp: str
    address: str
    hostname: str


class PdnsRange(NamedTuple):
    """First and last observation times for one IP and hostname."""

    address: str
    first_seen: str
    last_seen: str
    hostname: str


def parse_pdns_observation(warc_headers: str, hostname: str) -> PdnsObservation | None:
    """Parse one passive-DNS observation from WARC headers.

    Args:
        warc_headers: Text containing WARC header fields.
        hostname: Indexed hostname associated with the WARC record.

    Returns:
        Validated observation or ``None`` when required fields are invalid.
    """
    values: dict[str, str] = {}
    for line in warc_headers.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            values[name.strip().lower()] = value.strip()
    date_value = values.get("warc-date")
    address_value = values.get("warc-ip-address")
    normalized_hostname = hostname.strip().rstrip(".").lower()
    if not date_value or not address_value or not normalized_hostname:
        return None
    try:
        observed = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        timestamp = observed.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
        address = ipaddress.ip_address(address_value)
    except ValueError:
        return None
    return PdnsObservation(timestamp, str(address), normalized_hostname)


def format_pdns_observation(warc_headers: str, hostname: str) -> str | None:
    """Format one passive-DNS observation from WARC headers.

    Args:
        warc_headers: Text containing WARC header fields.
        hostname: Indexed hostname associated with the WARC record.

    Returns:
        ``YYYYMMDDHHMMSS:IP:hostname`` or ``None`` when required fields are
        missing or invalid. IPv6 addresses are enclosed in brackets.
    """
    observation = parse_pdns_observation(warc_headers, hostname)
    if observation is None:
        return None
    address = ipaddress.ip_address(observation.address)
    formatted_address = f"[{address}]" if address.version == 6 else str(address)
    return f"{observation.timestamp}:{formatted_address}:{observation.hostname}"


def format_pdns_timestamp(timestamp: str) -> str:
    """Format compact PDNS timestamp as an ISO Python datetime.

    Args:
        timestamp: Timestamp in ``YYYYMMDDHHMMSS`` form.

    Returns:
        Timestamp in ``YYYY-MM-DDTHH:MM:SS`` form.

    Raises:
        ValueError: If timestamp does not use the expected format.
    """
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").isoformat()


def aggregate_pdns_observations(
    observations: Iterable[PdnsObservation],
) -> list[PdnsRange]:
    """Aggregate observations by IP and hostname, sorted by numeric IP.

    Args:
        observations: Validated WARC passive-DNS observations.

    Returns:
        IP-sorted first/last-seen ranges.
    """
    ranges: dict[tuple[str, str], tuple[str, str]] = {}
    for observation in observations:
        key = (observation.address, observation.hostname)
        current = ranges.get(key)
        if current is None:
            ranges[key] = (observation.timestamp, observation.timestamp)
        else:
            ranges[key] = (
                min(current[0], observation.timestamp),
                max(current[1], observation.timestamp),
            )
    sorted_keys = sorted(
        ranges,
        key=lambda value: (
            ipaddress.ip_address(value[0]).version,
            int(ipaddress.ip_address(value[0])),
            value[1],
        ),
    )
    return [
        PdnsRange(
            address,
            ranges[(address, hostname)][0],
            ranges[(address, hostname)][1],
            hostname,
        )
        for address, hostname in sorted_keys
    ]


def fetch_warc_segment(
    warc_filename: str, offset: int, length: int, timeout: int = 300
) -> bytes:
    """Fetch one WARC byte range from Common Crawl.

    Args:
        warc_filename: Common Crawl WARC object path.
        offset: Start byte offset.
        length: Number of bytes to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Raw WARC range bytes.

    Raises:
        requests.RequestException: If range request fails.
    """
    url = f"https://data.commoncrawl.org/{warc_filename}"
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.content


def parse_warc_payload(record: dict[str, Any]) -> None:
    """Attach compressed encoded WARC payload and headers to a result.

    Args:
        record: Mutable metadata record containing WARC location fields.
    """
    raw_bytes = fetch_warc_segment(
        record["warc_filename"],
        record["warc_record_offset"],
        record["warc_record_length"],
    )
    warc_record = next(ArchiveIterator(BytesIO(raw_bytes)))
    payload = warc_record.content_stream().read()
    record["warc_payload"] = base64.b85encode(gzip.compress(payload)).decode("ascii")
    record["warc_headers"] = str(warc_record.rec_headers)
    record["http_headers"] = str(warc_record.http_headers or "")


def row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a ten-column ClickHouse row to a JSON-compatible record.

    Args:
        row: Ten-column URL metadata row.

    Returns:
        JSON-compatible metadata mapping.
    """
    keys = (
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
    )
    record = dict(zip(keys, row))
    scheme = record["url_protocol"] or "https"
    host = record["url_host_name"] or ""
    path = record["url_path"] or "/"
    record["url"] = f"{scheme}://{host}{path}"
    if record["url_query"]:
        record["url"] += f"?{record['url_query']}"
    return record


def add_payload(record: dict[str, Any], info_only: bool) -> None:
    """Fetch payload unless metadata-only mode was requested.

    Args:
        record: Mutable result record.
        info_only: Skip WARC retrieval when true.
    """
    if info_only:
        return
    try:
        parse_warc_payload(record)
    except (OSError, StopIteration, ValueError, requests.RequestException) as exc:
        logging.getLogger(__name__).warning("WARC retrieval failed: %s", exc)
        record["warc_payload"] = None
        record["warc_headers"] = f"Error: {exc}"
        record["http_headers"] = f"Error: {exc}"


def decode_payload(entry: dict[str, Any]) -> bytes:
    """Decode backend Base85-wrapped gzip payload.

    Args:
        entry: Backend record containing ``warc_payload``.

    Returns:
        Archived response body bytes.

    Raises:
        ValueError: If payload is missing or malformed.
    """
    encoded = entry.get("warc_payload")
    if not encoded:
        raise ValueError("Backend returned no WARC payload")
    try:
        return gzip.decompress(base64.b85decode(encoded))
    except (ValueError, OSError, binascii.Error) as exc:
        raise ValueError(f"Invalid backend WARC payload: {exc}") from exc


def read_download(
    response: requests.Response,
    total: int,
    label: str,
    logger: logging.Logger | None = None,
) -> bytes:
    """Read a streamed response while reporting progress.

    Args:
        response: Streamed HTTP response.
        total: Expected byte count.
        label: Progress label.
        logger: Optional progress logger.

    Returns:
        Complete response body.
    """
    active_logger = logger or logging.getLogger(__name__)
    chunks: list[bytes] = []
    received = 0
    last_percent = -1
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        received += len(chunk)
        percent = min(100, int(received * 100 / total)) if total else 0
        if percent >= last_percent + 10 or received == total:
            active_logger.info("%s (%d%%)", label, percent)
            last_percent = percent
    if total == 0:
        active_logger.info("%s (100%%)", label)
    return b"".join(chunks)


def direct_payload(
    entry: dict[str, Any],
    timeout: int = 300,
    logger: logging.Logger | None = None,
) -> tuple[bytes, str, str]:
    """Download and decode a WARC range directly from Common Crawl.

    Args:
        entry: Search record containing WARC location fields.
        timeout: Request timeout in seconds.
        logger: Optional download progress logger.

    Returns:
        Payload bytes, WARC headers, and HTTP headers.
    """
    filename = entry["warc_filename"]
    offset = int(entry["warc_record_offset"])
    length = int(entry["warc_record_length"])
    response = requests.get(
        f"https://data.commoncrawl.org/{filename}",
        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    raw_bytes = read_download(response, length, "direct object download", logger)
    record = next(ArchiveIterator(BytesIO(raw_bytes)))
    return (
        record.content_stream().read(),
        str(record.rec_headers),
        str(record.http_headers or ""),
    )


def populate_remote_payload(
    entry: dict[str, Any],
    show_headers: bool,
    context: RemoteObjectContext,
) -> None:
    """Populate one record through server or direct object download.

    Args:
        entry: Search record modified in place.
        show_headers: Fetch WARC and HTTP headers when true.
        context: HTTP, direct-download, stream, and logging callbacks.
    """
    params = {
        "file": entry["warc_filename"],
        "offset": entry["warc_record_offset"],
        "length": entry["warc_record_length"],
        "capability": entry.get("object_capability", ""),
    }
    response = context.request_api("/getobject", params=params, stream=True)
    context.logger.info("object request (0%)")
    context.logger.info(
        "getobject status=%s file=%s offset=%s length=%s",
        response.status_code,
        params["file"],
        params["offset"],
        params["length"],
    )
    if response.status_code == 204:
        context.logger.info("downloading object directly from Common Crawl")
        payload, warc_headers, http_headers = context.direct_fetch(entry)
        if context.cache_store is not None:
            try:
                context.cache_store(entry, payload, warc_headers, http_headers)
            except RuntimeError as exc:
                context.logger.warning("object cache update failed: %s", exc)
    else:
        context.logger.info("using server-returned object payload")
        payload = context.stream_reader(
            response,
            int(response.headers.get("Content-Length", "0")),
            "server object download",
        )
        warc_headers = http_headers = ""
        if show_headers:
            headers = context.request_api("/getobject/headers", params=params).json()
            warc_headers = headers.get("warc_headers", "")
            http_headers = headers.get("http_headers", "")
    entry["warc_payload"] = base64.b85encode(gzip.compress(payload)).decode("ascii")
    entry["warc_headers"] = warc_headers
    entry["http_headers"] = http_headers
