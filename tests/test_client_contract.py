"""Unit tests for customer CLI contract."""

import base64
from datetime import date
import gzip
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import engine, printout

CLIENT_PATH = Path(__file__).parents[1] / "lib" / "local.py"
SPEC = importlib.util.spec_from_file_location("ccwget_local", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_client_parser_keeps_local_search_options(monkeypatch) -> None:
    """Parser accepts all documented local search flags after split."""
    monkeypatch.setattr(
        "sys.argv",
        [
            str(CLIENT_PATH),
            "--list-fqdn",
            "www.example.test",
            "--alltime",
            "--detail",
            "--year",
            "2024",
        ],
    )
    args = MODULE.parse_args()
    assert args.list_fqdn == "www.example.test"
    assert args.alltime is True
    assert args.detail is True
    assert args.year == 2024
    assert MODULE.common_params(args)["year"] == ""


def test_client_parser_accepts_since_alias(monkeypatch) -> None:
    """The since alias maps to the after date bound."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "--since", "240101", "www.example.test"]
    )
    args = MODULE.parse_args()
    assert args.after == "20240101"
    assert MODULE.common_params(args)["after"] == "20240101"


@pytest.mark.parametrize(
    "value",
    [
        "https://www.example.test",
        "www.example.test/path",
        "www.example.test?query=value",
        "www.example.test#fragment",
        "invalid_domain",
    ],
)
def test_client_parser_rejects_url_or_invalid_list_fqdn(monkeypatch, value) -> None:
    """Parser rejects URLs and malformed values for FQDN listing."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-l", value])

    with pytest.raises(SystemExit):
        MODULE.parse_args()


def test_client_parser_accepts_url_enumeration(monkeypatch) -> None:
    """Parser accepts exact URL occurrence enumeration."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-e", "https://example.test/"])
    args = MODULE.parse_args()
    assert args.enumerate_url == "https://example.test/"


def test_client_parser_normalizes_bare_fqdn_and_warns(monkeypatch, capsys) -> None:
    """Parser converts bare FQDN to HTTPS root URL and emits warning."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "www.example.test"])

    args = MODULE.parse_args()

    assert args.url == "https://www.example.test/"
    assert "searching https://www.example.test/" in capsys.readouterr().err


def test_client_parser_keeps_quiet_bare_fqdn_warning_silent(
    monkeypatch, capsys
) -> None:
    """Quiet parser conversion does not emit normalization warning."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-q", "www.example.test"])

    args = MODULE.parse_args()

    assert args.url == "https://www.example.test/"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.example.test", "https://www.example.test/"),
        ("http://www.example.test?view=1", "http://www.example.test/?view=1"),
        ("https://www.example.test/path", "https://www.example.test/path"),
        (
            "https://www.example.test/path?view=1#top",
            "https://www.example.test/path?view=1#top",
        ),
    ],
)
def test_client_parser_preserves_explicit_and_invalid_urls(
    monkeypatch, value, expected
) -> None:
    """Parser preserves explicit paths, queries, fragments, and invalid domains."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-q", value])

    args = MODULE.parse_args()

    assert args.url == expected


def test_client_parser_rejects_invalid_bare_fqdn(monkeypatch) -> None:
    """Parser rejects a bare value without a dot or valid FQDN shape."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "eeuisr"])

    with pytest.raises(SystemExit):
        MODULE.parse_args()


def test_client_parser_accepts_year_before_legacy_lower_bound(monkeypatch) -> None:
    """Year validation leaves the actual lower bound to indexed-dataset metadata."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "--year", "2013", "example.test"]
    )

    args = MODULE.parse_args()

    assert args.year == 2013


def test_client_applies_server_minimum_year(monkeypatch) -> None:
    """Server metadata supplies the all-time progress start year."""
    monkeypatch.setattr(
        MODULE, "request_json", lambda endpoint, params: {"minimum_year": 2013}
    )
    args = SimpleNamespace()

    MODULE.apply_server_metadata(args)

    assert args.minimum_year == 2013


def test_client_parser_accepts_all_url_download_flag(monkeypatch) -> None:
    """Parser accepts explicit all-occurrence URL download flag."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "--all", "https://example.test/"]
    )

    args = MODULE.parse_args()

    assert args.all is True


def test_client_parser_accepts_passive_dns_fqdn(monkeypatch) -> None:
    """Parser accepts one exact FQDN for passive-DNS extraction."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-pdns", "www.example.test"])

    args = MODULE.parse_args()

    assert args.pdns == "www.example.test"


def test_client_parser_normalizes_string_search_tld(monkeypatch) -> None:
    """String search accepts four characters and normalizes TLD spelling."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-s", "circl", "-tld", "LU"])

    args = MODULE.parse_args()

    assert args.string_search == "circl"
    assert args.tld == ".lu"


def test_client_parser_accepts_multiple_string_search_fields(monkeypatch) -> None:
    """String search accepts combined FQDN, path, and query field selectors."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "-s", "luxtrust", "--fqdn", "--path"]
    )

    args = MODULE.parse_args()

    assert MODULE.selected_string_search_fields(args) == "fqdn,path"
    assert args.limit == 500


def test_string_search_saves_csv_listing(monkeypatch, tmp_path) -> None:
    """Substring listing writes timestamp, digest, and URL columns to CSV."""
    output_file = tmp_path / "results.csv"
    monkeypatch.setattr(
        "sys.argv",
        [str(CLIENT_PATH), "-s", "luxtrust", "--alltime", "-O", str(output_file)],
    )
    args = MODULE.parse_args()
    monkeypatch.setattr(
        MODULE,
        "request_json",
        lambda endpoint, params: [
            {
                "warc_filename": (
                    "crawl-data/CC-MAIN-2026-30/x/"
                    "CC-MAIN-20260713175126-20260713205126-00193.warc.gz"
                ),
                "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
                "url": "https://luxtrust.example/",
            }
        ],
    )

    MODULE.string_search(args)

    assert output_file.read_text(encoding="utf-8") == (
        "timestamp,digest,url\n"
        "2026-07-13T17:51:26,67df35fd332c2956c96771fd68a75680de5df4a4,"
        '"https://luxtrust.example/"\n'
    )


def test_sha1_search_saves_csv_listing(monkeypatch, tmp_path) -> None:
    """SHA-1 listing writes occurrence columns to CSV instead of downloading data."""
    output_file = tmp_path / "sha1.csv"
    monkeypatch.setattr(
        "sys.argv",
        [str(CLIENT_PATH), "-1", "digest-value", "--alltime", "-O", str(output_file)],
    )
    args = MODULE.parse_args()
    monkeypatch.setattr(
        MODULE,
        "request_json",
        lambda endpoint, params: [
            {
                "warc_filename": (
                    "crawl-data/CC-MAIN-2026-30/x/"
                    "CC-MAIN-20260713175126-20260713205126-00193.warc.gz"
                ),
                "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
                "url": "https://example.test/",
            }
        ],
    )

    MODULE.query_sha1(args)

    assert output_file.read_text(encoding="utf-8").startswith(
        "timestamp,digest,url\n2026-07-13T17:51:26,"
    )


def test_client_parser_rejects_short_string_search(monkeypatch) -> None:
    """String search rejects terms shorter than four characters."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-s", "abc"])

    with pytest.raises(SystemExit):
        MODULE.parse_args()


def test_client_parser_accepts_result_resume(monkeypatch) -> None:
    """Parser accepts a retained remote job result identifier."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "-result", "job-123"])

    args = MODULE.parse_args()

    assert args.result_job == "job-123"


def test_format_instance_contains_date_digest_and_page() -> None:
    """Occurrence output uses the requested date, digest, and URL fields."""
    entry = {
        "warc_filename": "crawl-data/CC-MAIN-2026-30/x/CC-MAIN-20260713175126-20260713205126-00193.warc.gz",
        "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
        "url": "https://example.test/",
    }
    assert MODULE.format_instance(entry) == (
        "2026-07-13T17:51:26:67df35fd332c2956c96771fd68a75680de5df4a4:"
        "https://example.test/"
    )


def test_format_pdns_observation_uses_warc_date_ip_and_hostname() -> None:
    """Passive-DNS output uses compact UTC time, validated IP, and FQDN."""
    headers = """WARC/1.0
WARC-Date: 2026-07-13T19:28:03Z
WARC-IP-Address: 185.194.93.14
"""

    assert engine.format_pdns_observation(headers, "WWW.Example.Test.") == (
        "20260713192803:185.194.93.14:www.example.test"
    )


def test_format_pdns_observation_brackets_ipv6() -> None:
    """Passive-DNS output brackets IPv6 to preserve colon delimiters."""
    headers = """WARC-Date: 2026-07-13T19:28:03+02:00
WARC-IP-Address: 2001:db8::1
"""

    assert engine.format_pdns_observation(headers, "v6.example.test") == (
        "20260713172803:[2001:db8::1]:v6.example.test"
    )


def test_format_pdns_observation_rejects_missing_or_invalid_fields() -> None:
    """Passive-DNS formatting rejects absent dates and invalid IP addresses."""
    assert (
        engine.format_pdns_observation("WARC-IP-Address: 192.0.2.1", "host.test")
        is None
    )
    assert (
        engine.format_pdns_observation(
            "WARC-Date: 2026-01-01T00:00:00Z\nWARC-IP-Address: invalid",
            "host.test",
        )
        is None
    )


def test_format_pdns_timestamp_uses_iso_datetime() -> None:
    """PDNS output timestamp uses ISO Python datetime format."""
    assert engine.format_pdns_timestamp("20260722233821") == "2026-07-22T23:38:21"


def test_pdns_parser_defaults_to_one_object_per_day_and_supports_full(
    monkeypatch,
) -> None:
    """PDNS defaults to sampled daily objects and accepts the full override."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "-pdns", "www.example.test", "--full"]
    )
    args = MODULE.parse_args()
    assert args.full is True


def test_client_payload_round_trip() -> None:
    """Client decodes backend Base85-wrapped gzip payload bytes."""
    payload = b"backend payload"
    encoded = base64.b85encode(gzip.compress(payload)).decode("ascii")
    assert MODULE.decode_payload({"warc_payload": encoded}) == payload


def test_client_parser_normalizes_date_range(monkeypatch) -> None:
    """Parser accepts short dates and normalizes the inclusive range."""
    monkeypatch.setattr(
        "sys.argv",
        [str(CLIENT_PATH), "--time-range", "240101,20240331", "www.example.test"],
    )
    args = MODULE.parse_args()
    assert args.time_range == "20240101,20240331"


def test_client_parser_normalizes_exact_crawl_time(monkeypatch) -> None:
    """Parser accepts ISO crawl timestamps for exact date limiting."""
    monkeypatch.setattr(
        "sys.argv",
        [
            str(CLIENT_PATH),
            "http://www.circl.lu/team/",
            "--at",
            "2013-05-16T12:53:28",
        ],
    )
    args = MODULE.parse_args()
    assert args.at == "20130516125328"
    assert MODULE.common_params(args)["at"] == "20130516125328"
    assert MODULE.common_params(args)["after"] == "20130516"
    assert MODULE.common_params(args)["before"] == "20130516"


def test_client_parser_normalizes_on_date_alias(monkeypatch) -> None:
    """Parser accepts ``--on`` with existing compact date syntax."""
    monkeypatch.setattr(
        "sys.argv", [str(CLIENT_PATH), "--on", "130516", "www.example.test"]
    )
    args = MODULE.parse_args()
    assert args.at == "20130516"
    assert MODULE.common_params(args)["after"] == "20130516"
    assert MODULE.common_params(args)["before"] == "20130516"
    assert "at" not in MODULE.common_params(args)


def test_client_defaults_to_last_two_years(monkeypatch, caplog) -> None:
    """Client applies and reports a two-year period when none is supplied."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "www.example.test"])
    args = MODULE.parse_args()
    caplog.set_level(logging.INFO)
    MODULE.apply_default_timeframe(args, date(2026, 7, 30))
    assert args.after == "20240730"
    assert args.before == "20260730"
    assert "No timeframe specified" not in caplog.text


def test_client_default_timeframe_handles_leap_day(monkeypatch) -> None:
    """Two-year default maps leap day to February 28 when needed."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "www.example.test"])
    args = MODULE.parse_args()
    MODULE.apply_default_timeframe(args, date(2024, 2, 29))
    assert args.after == "20220228"
    assert args.before == "20240229"


def test_client_alltime_skips_default_timeframe(monkeypatch) -> None:
    """Explicit all-time search does not receive default date bounds."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "--alltime", "www.example.test"])
    args = MODULE.parse_args()
    MODULE.apply_default_timeframe(args, date(2026, 7, 30))
    assert args.after is None
    assert args.before is None


def test_client_exact_time_skips_default_timeframe(monkeypatch) -> None:
    """Explicit exact-time search does not receive default date bounds."""
    monkeypatch.setattr(
        "sys.argv",
        [str(CLIENT_PATH), "--at", "2024-01-02T03:04:05", "www.example.test"],
    )
    args = MODULE.parse_args()
    MODULE.apply_default_timeframe(args, date(2026, 7, 30))
    assert args.at == "20240102030405"
    assert args.after is None
    assert args.before is None


def test_client_without_arguments_prints_full_help(monkeypatch, capsys) -> None:
    """Calling client without arguments prints concise help and exits."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH)])
    with pytest.raises(SystemExit) as exc_info:
        MODULE.parse_args()
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--alltime" not in output
    assert "--help-async" in output
    assert "--help-time" in output
    assert "--help-textsearch" in output
    assert "--string-search" not in output
    assert "--tld" not in output
    assert "--fqdn" not in output
    assert "--path" not in output
    assert "--query" not in output
    assert "--limit" not in output
    assert "-status JOB_ID" not in output
    assert "CIRCL.LU" in output
    assert "-i, --info" in output
    assert "Only print index records." in output
    assert "-O file, --output file" in output
    assert "Output file ('-' for stdout); listing modes write CSV" in output
    assert "--info-only" not in output


def test_client_async_help_lists_queue_controls(monkeypatch, capsys) -> None:
    """Dedicated asynchronous help lists remote queue-control commands."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "--help-async"])

    with pytest.raises(SystemExit) as exc_info:
        MODULE.parse_args()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "-async" in output
    assert "-status JOB_ID" in output
    assert "-result JOB_ID" in output
    assert "-jobs" in output
    assert "-flush" in output
    assert "CIRCL.LU" in output


def test_client_time_help_groups_all_time_modifiers(monkeypatch, capsys) -> None:
    """Dedicated time help groups modifiers under the CIRCL logo."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "--help-time"])

    with pytest.raises(SystemExit) as exc_info:
        MODULE.parse_args()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "CIRCL.LU" in output
    assert "time modifiers:" in output
    assert "--alltime" in output
    assert "Search all indexed data." in output
    assert "--year YEAR" in output
    assert (
        f"Search one year between {MODULE.MIN_YEAR} and {MODULE.datetime.now().year}"
        in output
    )
    assert "--after YYMMDD" in output
    assert "--since YYMMDD" in output
    assert "--before YYMMDD" in output
    assert "--on DATE_OR_TIME" in output
    assert "--at DATE_OR_TIME" in output
    assert "--time-range YYMMDD,YYMMDD" in output


def test_client_text_search_help_explains_scope_and_cost(monkeypatch, capsys) -> None:
    """Dedicated text-search help documents selectors, limit, and slowness warning."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "--help-textsearch"])

    with pytest.raises(SystemExit) as exc_info:
        MODULE.parse_args()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "extremely slow" in output
    assert "--fqdn" in output
    assert "--path" in output
    assert "--query" in output
    assert "--limit" in output
    assert "default: 500" in output


def test_client_rejects_removed_info_only_option(monkeypatch) -> None:
    """Parser rejects removed ``--info-only`` compatibility option."""
    monkeypatch.setattr("sys.argv", [str(CLIENT_PATH), "--info-only"])

    with pytest.raises(SystemExit) as exc_info:
        MODULE.parse_args()

    assert exc_info.value.code == 2


def test_output_section_titles_have_equal_width(capsys) -> None:
    """Indexed record, headers, and stored-data titles use equal widths."""
    printout.print_index_info({"url_protocol": "https"})
    printout.print_headers({"warc_headers": "WARC", "http_headers": "HTTP"})
    printout.print_stored_data("index.html")

    lines = capsys.readouterr().out.splitlines()
    titles = [line for line in lines if line.startswith("-")]
    assert len(titles) == 4
    assert {len(line) for line in titles} == {printout.SECTION_WIDTH}
    assert "Indexed record" in titles[0]
    assert lines[lines.index(titles[0]) + 1] == ""


def test_save_payload_reports_destination_in_normal_mode(tmp_path, capsys) -> None:
    """Normal payload downloads print their destination while quiet mode stays silent."""
    destination = tmp_path / "index.html"

    printout.save_payload(b"body", str(destination), quiet=False)

    assert capsys.readouterr().out == f"Saving {destination}\n"
    assert destination.read_bytes() == b"body"
