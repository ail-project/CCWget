"""Remote client contract tests."""

# Minimal response doubles intentionally expose one method.
# pylint: disable=too-few-public-methods

import importlib.util
import io
import logging
from pathlib import Path

import pytest


def load_remote():
    """Load the hyphenated remote client module for contract testing."""
    path = Path(__file__).parents[1] / "ccwget.py"
    spec = importlib.util.spec_from_file_location("ccwget_remote", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_parser_matches_local_actions(monkeypatch) -> None:
    """Remote parser delegates to the local parser contract exactly."""
    remote = load_remote()
    monkeypatch.setattr("sys.argv", ["ccwget.py", "--help"])
    try:
        remote.parse_args()
    except SystemExit as exc:
        assert exc.code == 0


def test_remote_parser_rejects_url_for_fqdn_listing(monkeypatch) -> None:
    """Remote parser rejects URL input for the FQDN listing option."""
    remote = load_remote()
    monkeypatch.setattr("sys.argv", ["ccwget.py", "-l", "https://example.test"])

    with pytest.raises(SystemExit):
        remote.parse_args()


def test_remote_fqdn_listing_writes_csv_in_quiet_mode(monkeypatch, tmp_path) -> None:
    """Quiet remote FQDN listing still writes the requested CSV file."""
    remote = load_remote()
    output_file = tmp_path / "toto.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ccwget.py",
            "-l",
            "infrachain.com",
            "--alltime",
            "-O",
            str(output_file),
            "-q",
        ],
    )
    args = remote.parse_args()
    monkeypatch.setattr(
        remote,
        "submit",
        lambda operation, arguments, progress: [
            {
                "warc_filename": (
                    "crawl-data/CC-MAIN-2026-30/x/"
                    "CC-MAIN-20260713175126-20260713205126-00193.warc.gz"
                ),
                "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
                "url": "https://infrachain.com/",
            }
        ],
    )

    remote.run(args)

    assert output_file.read_text(encoding="utf-8").startswith(
        "timestamp,digest,url\n2026-07-13T17:51:26,"
    )


def test_poll_interval_defaults_to_one_second() -> None:
    """Remote polling uses one second when no override is supplied."""
    remote = load_remote()
    assert remote.parse_poll_interval("1.0") == 1.0


def test_poll_interval_accepts_positive_override() -> None:
    """Remote polling accepts a positive environment override."""
    remote = load_remote()
    assert remote.parse_poll_interval("2.5") == 2.5


def test_poll_interval_rejects_non_positive_values() -> None:
    """Remote polling rejects zero, negative, and malformed intervals."""
    remote = load_remote()
    for value in ("0", "-1", "invalid"):
        try:
            remote.parse_poll_interval(value)
        except ValueError as exc:
            assert "CCWGET_POLL_INTERVAL" in str(exc)
        else:
            raise AssertionError(f"Expected ValueError for {value}")


def test_progress_since_uses_selected_timeframe_start() -> None:
    """Progress start date follows custom, year, and all-time selectors."""
    remote = load_remote()

    class Arguments:
        """Minimal timeframe argument object."""

        after = None
        time_range = None
        year = None
        alltime = False
        minimum_year = None

    args = Arguments()
    args.time_range = "20240101,20241231"
    assert remote.progress_since(args) == "20240101"
    args.time_range = None
    args.year = 2025
    assert remote.progress_since(args) == "20250101"
    args.year = None
    args.alltime = True
    assert remote.progress_since(args) == "20150101"
    args.minimum_year = 2013
    assert remote.progress_since(args) == "20130101"


def test_remote_uses_status_to_select_download(monkeypatch, caplog) -> None:
    """A 204 response invokes direct download and logs a clean percentage."""
    remote = load_remote()
    calls = []

    class Response:
        """Minimal object response."""

        status_code = 204

        def raise_for_status(self):
            """Represent a successful response."""

    def fake_request_api(endpoint, **kwargs):
        """Capture object and cache requests.

        Args:
            endpoint: Requested backend endpoint.
            kwargs: Request arguments.

        Returns:
            Successful response double.
        """
        calls.append((endpoint, kwargs))
        return Response()

    monkeypatch.setattr(remote, "request_api", fake_request_api)
    monkeypatch.setattr(remote, "direct_payload", lambda _entry: (b"x", "", ""))
    caplog.set_level(logging.INFO)
    entry = {"warc_filename": "f", "warc_record_offset": 0, "warc_record_length": 1}
    remote.get_payload(entry)
    assert entry["warc_payload"]
    assert [call[0] for call in calls] == ["/getobject", "/getobject/cache"]
    assert calls[1][1]["data"] == b"x"
    assert "object request (0%)" in caplog.text
    assert "object request (0%%)" not in caplog.text


def test_default_progress_bar_has_twenty_blocks() -> None:
    """Default renderer shows queue state and a 20-block table bar."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream)
    progress.started = 0
    progress.update("WAITING", position=3)
    progress.update("RUNNING", completed=3, total=12)
    progress.finish("DONE", total=12)
    output = stream.getvalue()
    assert "[ WAIT - 3 in Queue (0sec) ]" not in output
    assert output.split("\r")[-1].count("█") == 20
    assert "100%" in output
    assert "\r" in output


def test_search_progress_prints_object_count_and_start_datetime() -> None:
    """Standard progress includes object count and ISO start datetime."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream, since="20240730")

    progress.update("RUNNING", completed=1, total=20, total_rows=12345)

    assert (
        "5% Digging into 12,345 Objects since 2024-07-30T00:00:00" in stream.getvalue()
    )


def test_search_progress_prints_valid_zero_dataset_count() -> None:
    """Progress renders zero when selected tables contain zero indexed rows."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream, since="20240730")

    progress.update("RUNNING", completed=1, total=1, total_rows=0)

    assert "100% Digging into 0 Objects since 2024-07-30T00:00:00" in stream.getvalue()


def test_search_progress_does_not_fake_zero_when_count_unavailable() -> None:
    """Progress omits the numeric count when dataset metadata is unavailable."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream, since="20240730")

    progress.update("RUNNING", completed=0, total=0)

    output = stream.getvalue()
    assert "Digging into Objects since 2024-07-30T00:00:00" in output
    assert "Digging into 0 Objects" not in output


def test_pdns_progress_prints_analysis_message() -> None:
    """PDNS progress uses analysis wording and indexed object count."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(
        False,
        0,
        stream,
        message="Analysing",
        object_count=12,
    )

    progress.update("RUNNING", completed=1, total=20)

    assert "5% Analysing 12 Objects..." in stream.getvalue()


def test_pdns_selects_one_warc_object_per_filename_day() -> None:
    """Default PDNS sampling keeps the first indexed object for each WARC day."""
    remote = load_remote()
    entries = [
        {"warc_filename": "CC-MAIN-20260713-00001.warc.gz", "id": 1},
        {"warc_filename": "CC-MAIN-20260713-00002.warc.gz", "id": 2},
        {"warc_filename": "CC-MAIN-20260714-00001.warc.gz", "id": 3},
    ]

    selected = remote.select_pdns_entries(entries, True)

    assert [entry["id"] for entry in selected] == [1, 3]
    assert remote.select_pdns_entries(entries, False) == entries


def test_final_progress_uses_backend_object_count() -> None:
    """Final status object count remains visible when no running poll occurred."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream, since="20240731")

    progress.finish("DONE", total=1, total_rows=1234)

    assert (
        "100% Digging into 1,234 Objects since 2024-07-31T00:00:00" in stream.getvalue()
    )


def test_interactive_progress_waits_with_queue_time(monkeypatch) -> None:
    """Interactive renderer redraws WAIT position and elapsed seconds safely."""
    remote = load_remote()

    class Terminal(io.StringIO):
        """TTY-like output stream for progress rendering."""

        def isatty(self) -> bool:
            """Report an interactive terminal."""
            return True

    stream = Terminal()
    progress = remote.LOCAL.SearchProgress(False, 0, stream)
    progress.started = 0
    monkeypatch.setattr(remote.LOCAL.time, "monotonic", lambda: 69)
    progress.update("WAITING", position=2)
    assert "[ WAIT - 3 in Queue (69sec) ]" in stream.getvalue()
    assert "\r" in stream.getvalue()


def test_progress_clears_longer_wait_text() -> None:
    """A long queue message cannot leave trailing characters after the bar."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream)
    progress.started = remote.LOCAL.time.monotonic() - 123456
    progress.update("WAITING", position=1)
    progress.update("RUNNING", completed=1, total=1)
    output = stream.getvalue().split("\r")[-1]
    assert output.rstrip() == "[████████████████████] 100% Digging into Objects"


def test_progress_finish_clears_wait_text_without_running_update() -> None:
    """DONE transition clears a longer WAIT message even without RUNNING poll."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(False, 0, stream)
    progress.update("WAITING", position=1)

    progress.finish("DONE", total=1)

    output = stream.getvalue().split("\r")[-1]
    assert output.rstrip() == "[████████████████████] 100% Digging into Objects"


def test_quiet_progress_renderer_is_silent() -> None:
    """Quiet mode suppresses progress output completely."""
    remote = load_remote()
    stream = io.StringIO()
    progress = remote.LOCAL.SearchProgress(True, 0, stream)
    progress.update("WAITING", position=1)
    progress.finish("DONE", total=1)
    assert stream.getvalue() == ""


def test_submit_updates_progress_for_each_running_poll(monkeypatch) -> None:
    """Remote jobs report table progress even when state stays RUNNING."""
    remote = load_remote()
    statuses = iter(
        [
            {"state": "RUNNING", "completed_tables": 1, "total_tables": 2},
            {"state": "RUNNING", "completed_tables": 2, "total_tables": 2},
            {"state": "DONE", "completed_tables": 2, "total_tables": 2},
        ]
    )

    class Response:
        """Minimal JSON response used by the submit test."""

        def __init__(self, payload):
            """Store a JSON payload.

            Args:
                payload: Response mapping returned by ``json``.
            """
            self.payload = payload

        def json(self):
            """Return the configured response mapping."""
            return self.payload

    def fake_request(endpoint, method="GET", **_kwargs):
        """Return queued job, repeated running states, and final result.

        Args:
            endpoint: Requested service endpoint.
            method: HTTP method.
            _kwargs: Unused request arguments.

        Returns:
            Minimal response double.
        """
        if endpoint == "/jobs" and method == "POST":
            return Response({"job_id": "job-1"})
        if endpoint == "/jobs/job-1":
            return Response(next(statuses))
        return Response({"state": "DONE", "result": []})

    class Reporter:
        """Capture progress callbacks from ``submit``."""

        verbose = 0

        def __init__(self):
            """Initialize callback storage."""
            self.updates = []

        def update(self, state, **kwargs):
            """Capture one progress update.

            Args:
                state: Job state.
                kwargs: Progress fields.
            """
            self.updates.append((state, kwargs))

        def finish(self, state, **_kwargs):
            """Accept the terminal progress update.

            Args:
                state: Final job state.
                _kwargs: Unused final progress fields.
            """

    reporter = Reporter()
    monkeypatch.setattr(remote, "request_api", fake_request)
    monkeypatch.setattr(remote.time, "sleep", lambda _seconds: None)
    assert remote.submit("domain-enum", {}, reporter) == []
    assert [item[1]["completed"] for item in reporter.updates] == [1, 2]


def test_submit_ctrl_c_requests_server_job_cancellation(monkeypatch, capsys) -> None:
    """Ctrl-C during polling cancels only the submitted server job."""
    remote = load_remote()
    calls = []

    class Response:
        """Minimal response used by the cancellation test."""

        def json(self):
            """Return the submitted job identifier."""
            return {"job_id": "job-interrupted"}

    def fake_request(endpoint, method="GET", **_kwargs):
        """Capture submission and cancellation requests.

        Args:
            endpoint: Requested backend endpoint.
            method: HTTP method used for the request.
            _kwargs: Unused request arguments.

        Returns:
            Minimal submission response.
        """
        calls.append((method, endpoint))
        return Response()

    monkeypatch.setattr(remote, "request_api", fake_request)
    monkeypatch.setattr(
        remote,
        "wait_for_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(SystemExit) as error:
        remote.submit("query", {})

    assert error.value.code == 130
    assert calls == [("POST", "/jobs"), ("DELETE", "/jobs/job-interrupted")]
    assert "cancellation requested" in capsys.readouterr().err


def test_submit_ctrl_c_reports_cancellation_failure(monkeypatch, capsys) -> None:
    """Ctrl-C reports cancellation failures without restoring a traceback."""
    remote = load_remote()

    class Response:
        """Minimal response used by the cancellation failure test."""

        def json(self):
            """Return the submitted job identifier."""
            return {"job_id": "job-unreachable"}

    def fake_request(endpoint, method="GET", **_kwargs):
        """Fail the cancellation request after accepting submission.

        Args:
            endpoint: Requested backend endpoint.
            method: HTTP method used for the request.
            _kwargs: Unused request arguments.

        Returns:
            Submission response before cancellation fails.

        Raises:
            RuntimeError: When the cancellation endpoint is requested.
        """
        del endpoint
        if method == "DELETE":
            raise RuntimeError("service unavailable")
        return Response()

    monkeypatch.setattr(remote, "request_api", fake_request)
    monkeypatch.setattr(
        remote,
        "wait_for_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(SystemExit) as error:
        remote.submit("query", {})

    assert error.value.code == 130
    assert "unable to cancel job job-unreachable" in capsys.readouterr().err


def test_async_submission_returns_job_id(monkeypatch) -> None:
    """Asynchronous submission returns immediately with the backend job ID."""
    remote = load_remote()

    class Response:
        """Minimal asynchronous submission response."""

        def json(self):
            """Return one queued job identifier."""
            return {"job_id": "job-async"}

    monkeypatch.setattr(remote, "request_api", lambda *_args, **_kwargs: Response())
    assert remote.submit_async("query", {"url": "https://example.test/"}) == "job-async"


def test_job_listing_renders_safe_status_fields(monkeypatch, capsys) -> None:
    """Remote job listing prints safe state fields without hidden arguments."""
    remote = load_remote()

    class Response:
        """Minimal job-list response."""

        def json(self):
            """Return one retained job summary."""
            return {
                "jobs": [
                    {
                        "job_id": "job-safe",
                        "operation": "query",
                        "state": "WAITING",
                        "position": 1,
                        "completed_tables": 2,
                        "total_tables": 4,
                        "total_rows": 1234,
                        "error": None,
                        "created": 1,
                        "updated": 2,
                    }
                ]
            }

    monkeypatch.setattr(remote, "request_api", lambda endpoint: Response())
    monkeypatch.setattr(remote.time, "time", lambda: 10)

    remote.show_jobs()

    output = capsys.readouterr().out
    assert "job-safe" in output
    assert "query" in output
    assert "WAITING" in output
    assert "1,234" in output
    assert "arguments" not in output


def test_job_listing_reports_empty_collection(monkeypatch, capsys) -> None:
    """Remote job listing reports a successful empty retained collection."""
    remote = load_remote()

    class Response:
        """Minimal empty job-list response."""

        def json(self):
            """Return an empty retained job collection."""
            return {"jobs": []}

    monkeypatch.setattr(remote, "request_api", lambda endpoint: Response())

    remote.show_jobs()

    assert capsys.readouterr().out.strip() == "No retained jobs found."


def test_job_listing_rejects_search_combination(monkeypatch) -> None:
    """Remote client rejects combining job listing with a search action."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-jobs", "https://example.test/"],
    )
    args = remote.parse_args()
    monkeypatch.setattr(remote, "parse_args", lambda: args)
    monkeypatch.setattr(remote, "TOKEN", "test-token")

    with pytest.raises(SystemExit, match="-jobs"):
        remote.main()


def test_result_resume_rejects_search_combination(monkeypatch) -> None:
    """Remote client rejects combining result resume with a new search."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-result", "job-123", "https://example.test/"],
    )
    args = remote.parse_args()
    monkeypatch.setattr(remote, "parse_args", lambda: args)
    monkeypatch.setattr(remote, "TOKEN", "test-token")

    with pytest.raises(SystemExit, match="-result"):
        remote.main()


@pytest.mark.parametrize(
    ("verbosity", "expects_json"), [("-vv", False), ("-vvv", True)]
)
def test_remote_status_traces_json_only_at_triple_verbose(
    monkeypatch, caplog, capsys, verbosity, expects_json
) -> None:
    """Remote status enables decoded JSON tracing only for ``-vvv``.

    Args:
        monkeypatch: Pytest fixture used to isolate process arguments and HTTP.
        caplog: Pytest fixture used to capture debug logs.
        capsys: Pytest fixture used to capture rendered status output.
        verbosity: Verbosity flag passed to the remote client.
        expects_json: Whether decoded response JSON should be logged.
    """
    remote = load_remote()
    payload = {
        "job_id": "job-json",
        "operation": "query",
        "state": "RUNNING",
        "completed_tables": 1,
        "total_tables": 2,
        "total_rows": 10,
        "details": {"nested": ["one", "two"]},
    }
    captured = {}

    class Response:
        """Minimal JSON status response for remote tracing."""

        headers = {"Content-Type": "application/json"}
        status_code = 200
        reason = "OK"

        def json(self):
            """Return one job status payload."""
            return payload

    def fake_request(*_args, **kwargs):
        """Capture HTTP kwargs and return a status payload.

        Args:
            _args: Positional ``requests.request`` arguments.
            kwargs: Keyword ``requests.request`` arguments.

        Returns:
            Successful JSON response double.
        """
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(
        "sys.argv", ["ccwget.py", "-status", "job-json", verbosity]
    )
    monkeypatch.setattr("lib.http.requests.request", fake_request)
    monkeypatch.setattr(remote, "TOKEN", "client-token")
    monkeypatch.setattr(remote.HTTP_CLIENT, "token", "client-token")
    caplog.set_level(logging.DEBUG, logger=remote.LOGGER.name)

    remote.main()

    output = capsys.readouterr().out
    assert "State: RUNNING" in output
    assert captured["headers"] == {
        "Authorization": "Bearer client-token",
        "User-Agent": "CCWget Client",
    }
    assert ("JSON response" in caplog.text) is expects_json
    assert ('"nested": [' in caplog.text) is expects_json
    assert "client-token" not in caplog.text
    assert "Authorization" not in caplog.text


def test_build_async_request_preserves_search_action(monkeypatch) -> None:
    """Async request construction preserves exact URL search arguments."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv", ["ccwget.py", "-async", "https://example.test/"]
    )
    args = remote.parse_args()
    operation, arguments = remote.build_job_request(args)
    assert operation == "query"
    assert arguments["url"] == "https://example.test/"
    assert arguments["_client_action"] == "query"
    assert arguments["all"] == "false"


def test_build_async_request_all_downloads_all_url_occurrences(monkeypatch) -> None:
    """Async exact URL request enables all occurrence downloads only with flag."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv", ["ccwget.py", "-async", "--all", "https://example.test/"]
    )
    args = remote.parse_args()

    _, arguments = remote.build_job_request(args)

    assert arguments["all"] == "true"


def test_build_async_request_preserves_passive_dns_action(monkeypatch) -> None:
    """Async passive-DNS request stores safe rendering action and exact FQDN."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-async", "-pdns", "www.example.test"],
    )
    args = remote.parse_args()

    operation, arguments = remote.build_job_request(args)

    assert operation == "list-fqdn"
    assert arguments["fqdn"] == "www.example.test"
    assert arguments["_client_action"] == "pdns"
    assert arguments["info_only"] == "true"
    assert arguments["detail"] == "true"


def test_build_async_request_supports_string_search_fields_and_tld(monkeypatch) -> None:
    """Async string search carries selected fields and normalized TLD to backend."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        [
            "ccwget.py",
            "-async",
            "-s",
            "circl",
            "--tld",
            "LU",
            "--fqdn",
            "--query",
        ],
    )
    args = remote.parse_args()

    operation, arguments = remote.build_job_request(args)

    assert operation == "query"
    assert arguments["_client_action"] == "string-search"
    assert arguments["url_contains"] == "circl"
    assert arguments["url_contains_fields"] == "fqdn,query"
    assert arguments["limit"] == "500"
    assert arguments["tld"] == ".lu"


def test_build_async_request_carries_exact_crawl_time(monkeypatch) -> None:
    """Async exact URL search carries exact crawl timestamp limits to backend."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        [
            "ccwget.py",
            "-async",
            "http://www.circl.lu/team/",
            "--at",
            "2013-05-16T12:53:28",
        ],
    )
    args = remote.parse_args()

    operation, arguments = remote.build_job_request(args)

    assert operation == "query"
    assert arguments["url"] == "http://www.circl.lu/team/"
    assert arguments["after"] == "20130516"
    assert arguments["before"] == "20130516"
    assert arguments["at"] == "20130516125328"


def test_sha1_output_prints_all_occurrence_details(monkeypatch, capsys) -> None:
    """SHA-1 output prints ISO timestamp, digest, and URL for every record."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-1", "67df35fd332c2956c96771fd68a75680de5df4a4"],
    )
    args = remote.parse_args()
    fetched = []

    def record_download(*download_args, **download_kwargs):
        """Record unexpected SHA-1 payload download."""
        fetched.append((download_args, download_kwargs))

    monkeypatch.setattr(remote, "get_payload", record_download)

    remote.render_resumed_result(
        "sha1",
        [
            {
                "warc_filename": "CC-MAIN-20260713175126-20260713205126-00193.warc.gz",
                "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
                "url": "https://example.test/one",
            },
            {
                "warc_filename": "CC-MAIN-20260714120000-20260714150000-00193.warc.gz",
                "content_digest": "M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE",
                "url": "https://example.test/two",
            },
        ],
        args,
    )

    output = capsys.readouterr().out
    assert (
        "2026-07-13T17:51:26:67df35fd332c2956c96771fd68a75680de5df4a4:https://example.test/one"
        in output
    )
    assert (
        "2026-07-14T12:00:00:67df35fd332c2956c96771fd68a75680de5df4a4:https://example.test/two"
        in output
    )
    assert not fetched


def test_passive_dns_fetches_warc_and_aggregates_output(monkeypatch, capsys) -> None:
    """Passive-DNS rendering fetches each WARC and prints time ranges."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-pdns", "www.example.test", "--full"],
    )
    args = remote.parse_args()
    entries = [
        {"url_host_name": "www.example.test", "sequence": 1},
        {"url_host_name": "www.example.test", "sequence": 1},
        {"url_host_name": "www.example.test", "sequence": 2},
    ]
    fetched = []

    def fake_get_payload(entry, show_headers=False, logger=None):
        """Attach deterministic WARC headers to one fake record.

        Args:
            entry: Fake record modified in place.
            show_headers: Whether header retrieval was requested.
            logger: Optional logger accepted by object retrieval.
        """
        del logger
        fetched.append(show_headers)
        entry["warc_headers"] = (
            f"WARC-Date: 2026-07-13T19:28:0{entry['sequence']}Z\n"
            "WARC-IP-Address: 185.194.93.14"
        )

    monkeypatch.setattr(remote, "get_payload", fake_get_payload)

    remote.render_pdns_result(entries, args)

    assert fetched == [True, True, True]
    assert capsys.readouterr().out.splitlines() == [
        "ip,first_seen,last_seen,fqdn",
        "185.194.93.14,2026-07-13T19:28:01,2026-07-13T19:28:02,www.example.test",
    ]


def test_passive_dns_skips_failed_warc_fetch(monkeypatch, caplog, capsys) -> None:
    """Passive-DNS rendering continues when one WARC object cannot be retrieved."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv", ["ccwget.py", "-pdns", "www.example.test", "--full"]
    )
    args = remote.parse_args()
    entries = [
        {"url_host_name": "www.example.test", "sequence": 1},
        {"url_host_name": "www.example.test", "sequence": 2},
    ]

    def fake_get_payload(entry, show_headers=False, logger=None):
        """Attach one observation or raise a backend object error.

        Args:
            entry: Fake record modified in place.
            show_headers: Whether header retrieval was requested.
            logger: Optional logger accepted by object retrieval.

        Raises:
            RuntimeError: For the second fake WARC object.
        """
        del show_headers, logger
        if entry["sequence"] == 2:
            raise RuntimeError("Backend API error: object retrieval failed")
        entry["warc_headers"] = "\n".join(
            (
                "WARC-Date: 2026-07-13T19:28:01Z",
                "WARC-IP-Address: 185.194.93.14",
            )
        )

    monkeypatch.setattr(remote, "get_payload", fake_get_payload)
    caplog.set_level(logging.WARNING, logger=remote.LOGGER.name)

    remote.render_pdns_result(entries, args)

    assert "skipping WARC 2/2" in caplog.text
    assert capsys.readouterr().out.splitlines() == [
        "ip,first_seen,last_seen,fqdn",
        "185.194.93.14,2026-07-13T19:28:01,2026-07-13T19:28:01,www.example.test",
    ]


def test_passive_dns_run_searches_metadata_before_rendering(monkeypatch) -> None:
    """Normal passive-DNS mode searches detailed metadata before WARC rendering."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-pdns", "www.example.test", "--year", "2026"],
    )
    args = remote.parse_args()
    calls = []
    records = [{"url_host_name": "www.example.test"}]

    def fake_submit(operation, arguments, _progress):
        """Capture passive-DNS metadata search.

        Args:
            operation: Backend operation name.
            arguments: Backend operation arguments.
            _progress: Unused progress renderer.

        Returns:
            One fake indexed record.
        """
        calls.append((operation, arguments))
        return records

    rendered = []
    monkeypatch.setattr(remote, "submit", fake_submit)
    monkeypatch.setattr(
        remote,
        "render_pdns_result",
        lambda entries, _args: rendered.extend(entries),
    )

    remote.run(args)

    assert calls[0][0] == "list-fqdn"
    assert calls[0][1]["fqdn"] == "www.example.test"
    assert calls[0][1]["detail"] == "true"
    assert calls[0][1]["info_only"] == "true"
    assert calls[0][1]["year"] == "2026"
    assert rendered == records


def test_remote_rejects_url_as_passive_dns_fqdn(monkeypatch) -> None:
    """Remote passive-DNS mode rejects a URL instead of an exact FQDN."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-pdns", "https://www.example.test/"],
    )
    args = remote.parse_args()
    monkeypatch.setattr(remote, "parse_args", lambda: args)
    monkeypatch.setattr(remote, "TOKEN", "test-token")

    with pytest.raises(SystemExit, match="expects an FQDN"):
        remote.main()


def test_resumed_passive_dns_job_uses_warc_renderer(monkeypatch) -> None:
    """Resumed passive-DNS action dispatches to the WARC observation renderer."""
    remote = load_remote()
    monkeypatch.setattr("sys.argv", ["ccwget.py", "-result", "job-pdns"])
    args = remote.parse_args()
    records = [{"url_host_name": "www.example.test"}]
    rendered = []
    monkeypatch.setattr(
        remote,
        "render_pdns_result",
        lambda entries, _args: rendered.extend(entries),
    )

    remote.render_resumed_result("pdns", records, args)

    assert rendered == records


def test_resume_result_waits_renders_and_consumes_without_bar(
    monkeypatch, capsys
) -> None:
    """Result resume renders without progress and consumes the terminal job."""
    remote = load_remote()
    statuses = iter(
        [
            {
                "state": "DONE",
                "operation": "query",
                "client_action": "query-info",
                "completed_tables": 2,
                "total_tables": 2,
            },
        ]
    )

    class Response:
        """Minimal response for result-resume polling."""

        def __init__(self, payload):
            """Store response JSON.

            Args:
                payload: JSON-compatible response.
            """
            self.payload = payload

        def json(self):
            """Return stored response JSON."""
            return self.payload

    calls = []

    def fake_request(endpoint, method="GET", **_kwargs):
        """Return status polls and one terminal result.

        Args:
            endpoint: Requested backend endpoint.
            method: HTTP method.
            _kwargs: Unused request options.

        Returns:
            Minimal response.
        """
        calls.append((method, endpoint))
        if method == "DELETE":
            return Response({"deleted": True})
        if endpoint.endswith("/result"):
            return Response({"state": "DONE", "result": [{"url": "https://test/"}]})
        return Response(next(statuses))

    monkeypatch.setattr(remote, "request_api", fake_request)
    monkeypatch.setattr(remote.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-result", "job-123", "-i"],
    )
    args = remote.parse_args()
    rendered = []
    monkeypatch.setattr(
        remote,
        "render_resumed_result",
        lambda action, result, _args: rendered.append((action, result)),
    )

    remote.resume_result("job-123", args)

    assert rendered == [("query-info", [{"url": "https://test/"}])]
    assert calls[-1] == ("DELETE", "/jobs/job-123")
    assert capsys.readouterr().err == ""


def test_resume_result_reports_active_job_without_waiting(monkeypatch, capsys) -> None:
    """Result resume reports active status without fetching or deleting the job."""
    remote = load_remote()
    calls = []

    class Response:
        """Minimal active-job response."""

        def json(self):
            """Return one active job state."""
            return {
                "job_id": "job-running",
                "state": "RUNNING",
                "operation": "domain-enum",
                "completed_tables": 1,
                "total_tables": 25,
                "total_rows": 60950182700,
            }

    def fake_request(endpoint, method="GET", **_kwargs):
        """Capture active-job status requests.

        Args:
            endpoint: Requested backend endpoint.
            method: HTTP method.
            _kwargs: Unused request options.

        Returns:
            Active-job response.
        """
        calls.append((method, endpoint))
        return Response()

    monkeypatch.setattr(remote, "request_api", fake_request)
    monkeypatch.setattr("sys.argv", ["ccwget.py", "-result", "job-running"])
    args = remote.parse_args()

    remote.resume_result("job-running", args)

    assert calls == [("GET", "/jobs/job-running")]
    assert "State: RUNNING" in capsys.readouterr().out


def test_resume_result_reports_terminal_error(monkeypatch) -> None:
    """Result resume reports a stored terminal job error without result fetch."""
    remote = load_remote()

    class Response:
        """Minimal terminal-error status response."""

        def json(self):
            """Return one failed job state."""
            return {"state": "ERROR", "error": "query failed"}

    monkeypatch.setattr(remote, "request_api", lambda _endpoint: Response())
    reporter = remote.LOCAL.SearchProgress(True, 0)

    with pytest.raises(RuntimeError, match="query failed"):
        remote.wait_for_result("job-error", reporter)


def test_resume_result_consumes_terminal_error(monkeypatch) -> None:
    """Result resume consumes a retained failed job after reading its error."""
    remote = load_remote()
    deleted = []
    monkeypatch.setattr(
        remote,
        "wait_for_result",
        lambda *_args, **_kwargs: (
            {"state": "ERROR", "error": "query failed"},
            None,
        ),
    )
    monkeypatch.setattr(
        remote,
        "request_api",
        lambda endpoint, method="GET", **_kwargs: deleted.append((method, endpoint)),
    )
    monkeypatch.setattr("sys.argv", ["ccwget.py", "-result", "job-error"])
    args = remote.parse_args()

    with pytest.raises(RuntimeError, match="query failed"):
        remote.resume_result("job-error", args)

    assert deleted == [("DELETE", "/jobs/job-error")]


def test_resume_result_keeps_job_when_rendering_fails(monkeypatch) -> None:
    """Result resume keeps a terminal job when output or download fails."""
    remote = load_remote()
    deleted = []
    monkeypatch.setattr(
        remote,
        "wait_for_result",
        lambda *_args, **_kwargs: (
            {"state": "DONE", "operation": "query"},
            [{"url": "https://test/"}],
        ),
    )
    monkeypatch.setattr(
        remote,
        "render_resumed_result",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("download failed")),
    )
    monkeypatch.setattr(
        remote,
        "request_api",
        lambda endpoint, method="GET", **_kwargs: deleted.append((method, endpoint)),
    )
    monkeypatch.setattr("sys.argv", ["ccwget.py", "-result", "job-done"])
    args = remote.parse_args()

    with pytest.raises(RuntimeError, match="download failed"):
        remote.resume_result("job-done", args)

    assert not deleted


def test_resumed_query_info_uses_metadata_rendering(monkeypatch) -> None:
    """Resumed query-info action renders metadata without object download."""
    remote = load_remote()
    monkeypatch.setattr(
        "sys.argv",
        ["ccwget.py", "-result", "job-123"],
    )
    args = remote.parse_args()
    rendered = []
    monkeypatch.setattr(
        remote,
        "render",
        lambda entries, options, show_url=False: rendered.append(
            (entries, options.info_only, show_url)
        ),
    )
    entries = [{"url": "https://example.test/"}]

    remote.render_resumed_result("query-info", entries, args)

    assert rendered == [(entries, True, False)]
