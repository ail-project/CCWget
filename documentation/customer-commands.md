# Customer Commands

Run commands from repository root. Activate environment first:

```bash
source .venv/bin/activate
```

## Direct local search

`ccwget.py` sends all search and WARC requests to the backend service. Set `CCWGET_SERVICE_URL` when the
service is not local. Configure `CCWGET_TOKEN` or the `token` YAML value; direct compatibility search routes require
the bearer token just like queued job routes.

`ccwget.py` has the same command-line contract but requires `CCWGET_TOKEN`. Without an explicit timeframe,
the client searches the previous two years and prints the calculated start date. It submits queued searches and always
calls `/getobject`; the server decides whether the response is `200` (server download) or `204` (client download).

In default mode, remote searches show a 20-block progress bar. While queued, it reports the queue position and elapsed
wait time, for example `[ WAIT - 2 in Queue (69sec) ]`. `-q` hides the bar. `-v` and `-vv` keep textual state and HTTP
diagnostics.

Substring searches are limited to 500 results by default. Override with `--limit N`, for example
`./ccwget.py -s circl --fqdn --limit 1000`.

Press `Ctrl-C` during a synchronous remote search to request cancellation of that submitted job. The client sends
`DELETE /jobs/<job_id>`, prints the cancellation outcome, and exits with status 130. A waiting job becomes
`CANCELLED`; a running job becomes `CANCEL_REQUESTED` and the worker stops it at its next cancellation check. If the
cancel request cannot reach the service, the client reports the failure; the server-side job may remain active. Use
`-jobs`, `-status JOB_ID`, `-result JOB_ID`, or `-flush` after reconnecting to inspect or clean up retained work.

## Client configuration

The client reads the optional root file `config_client.yaml`:

```yaml
service_url: "http://127.0.0.1:4321"
token: "client-token-value"
```

Create it from `config_client.yaml.sample`. Environment variables override YAML values:

```bash
export CCWGET_SERVICE_URL=http://127.0.0.1:4321
export CCWGET_TOKEN="client-token-value"
```

Submit without waiting, inspect one job, list retained jobs, or cancel active jobs owned by the configured token:

```bash
python ccwget.py -async https://example.org/
python ccwget.py -status JOB_ID
python ccwget.py -result JOB_ID
python ccwget.py -jobs
python ccwget.py -flush
```

`-jobs` displays only jobs submitted by the configured token, newest first. It shows safe status metadata without
arguments, results, tokens, salts, or internal client IDs. Completed, failed, and cancelled jobs remain visible until
normal server retention cleanup or `-result JOB_ID` consumes them. `-result` reports an active `WAITING`, `RUNNING`, or
`CANCEL_REQUESTED` job immediately without waiting, fetching a result, or deleting the job. For terminal jobs, it
renders or downloads the retained result, then removes the job. A failed render, download, or network request keeps a
successful job available for retry. Reading a retained `ERROR` or `CANCELLED` state consumes it before reporting the
error. Current output options such as `-q`, `-v`, `-i`, `-O`, and `-S` apply to the resumed command. `-status` never
downloads a WARC object. `-flush` cancels both waiting work and the current running query for this token; it cannot
affect another client ID.


Use `CCWGET_CLIENT_CONFIG` to select another YAML path. Keep the live file and token outside version control.

Use `-v`/`--verbose` to print job states, phase percentages, `/getobject` status, and download-path decisions. Repeat
it as `-vv` to print HTTP calls and connection details. Tokens and payload contents are never printed.
Use `-vvv` to additionally trace decoded JSON API responses. JSON tracing excludes binary object responses and never
prints authorization headers or bearer tokens, but returned search metadata may be captured in terminal logs.

Positional bare FQDNs are interpreted as HTTPS root URLs. For example, `www.circl.lu` is searched as
`https://www.circl.lu/` and emits a warning. Use `-q` to suppress that warning. Explicit HTTP/HTTPS URLs keep their
paths, queries, and fragments unchanged; root URLs gain a trailing slash when needed.

Date filters accept `YYMMDD` or `YYYYMMDD`. Use `--on` or `--at` to limit downloads to one crawl day or to one ISO
timestamp such as `2013-05-16T12:53:28`. Use `--alltime` to disable the default two-year range:

```bash
python ccwget.py --after 240101 --before 241231 https://example.org/page
python ccwget.py http://www.circl.lu/team/ --at 2013-05-16T12:53:28
python ccwget.py --time-range 20240101,20241231 https://example.org/page
python ccwget.py --alltime https://example.org/page
```

Exact URL searches download only the first matching archived occurrence by default. Add `--all` to download every
matching occurrence:

```bash
python ccwget.py --all https://example.org/page --alltime
```

Search substrings across complete indexed URLs with at least four characters. Results print every occurrence as
`YYYY-MM-DDTHH:MM:SS:sha1:url` without downloading payloads. Add `--tld` to restrict the FQDN suffix; `LU`, `lu`,
`.lu`, and `.LU` are equivalent:

```bash
python ccwget.py -s circl --tld .LU --alltime
```

### Show help

```bash
python ccwget.py --help
python ccwget.py --help-async
python ccwget.py --help-time
```

### Download latest archived version of URL

```bash
python ccwget.py https://circl.lu/pub/tr-73/
```

If URL path ends with `/`, output defaults to `index.html`. Existing files are preserved by adding a numeric suffix.

### Save with selected filename

```bash
python ccwget.py https://circl.lu/pub/tr-73/ -O archived-page.html
```

### Write body to stdout

```bash
python ccwget.py https://circl.lu/pub/tr-73/ -O -
```

### Save listing results as CSV

For listing modes, `-O` writes a CSV file instead of printing the listing. Substring and FQDN listings use
`timestamp,digest,url` columns; domain and FQDN enumeration use one `fqdn` or `url` column as appropriate.

```bash
python ccwget.py -s circl --fqdn -O results.csv
python ccwget.py --list-domain lu -O domains.csv
```

### Show metadata without downloading WARC body

```bash
python ccwget.py https://circl.lu/pub/tr-73/ --info
```

Short form:

```bash
python ccwget.py https://circl.lu/pub/tr-73/ -i
```

### Show WARC and HTTP response headers

```bash
python ccwget.py https://circl.lu/pub/tr-73/ -S
```

### List URLs for one FQDN

```bash
python ccwget.py --list-fqdn www.circl.lu
```

Short form:

```bash
python ccwget.py -l www.circl.lu
```

`-l/--list-fqdn` accepts FQDNs only, not URLs. Use `-e/--enumerate` for exact URL searches.

Include content digest and WARC location:

```bash
python ccwget.py -l www.circl.lu --detail
```

### Extract passive-DNS observations

`-pdns FQDN` is available through `ccwget.py`. It first searches indexed pages for the exact FQDN, then fetches
each WARC object through the token-selected `/getobject` path. It extracts `WARC-Date` and `WARC-IP-Address`, groups
observations by IP/FQDN, and prints:

```text
ip,first_seen,last_seen,fqdn
```

`first_seen` and `last_seen` use ISO Python datetime format: `YYYY-MM-DDTHH:MM:SS`.

Downloaded WARC response bodies are stored in the server SQLite object cache. A client-side (`L`) download uploads its
payload after retrieval; later `L`, `S`, or local requests reuse the cached payload.

IPv6 addresses are enclosed in brackets. Search-time modifiers apply normally:

```bash
python ccwget.py -pdns www.circl.lu
python ccwget.py -pdns www.circl.lu --full
python ccwget.py -pdns www.circl.lu --year 2025
python ccwget.py -async -pdns www.circl.lu --alltime
```

By default, PDNS downloads one indexed WARC object per day, using the date encoded in the WARC filename. Use
`--full` to download every matching object.

### List URLs for domain and subdomains

Long-running remote domain enumeration displays the queue state and refreshes the physical-table progress while the
backend job remains RUNNING. Use `-q` to suppress progress output or `-v`/`-vv` for diagnostic logging.

Include leading dot to reduce unwanted suffix matches:

```bash
python ccwget.py --list-domain .circl.lu
```

Detailed output:

```bash
python ccwget.py -ld .circl.lu -d
```

### Enumerate FQDNs under domain

```bash
python ccwget.py --domain-enumeration .circl.lu
```

Search every physical crawl:

```bash
python ccwget.py -de .circl.lu --alltime
```

### Search content SHA-1

Common Crawl Base32 digest:

```bash
python ccwget.py --sha1 M7PTL7JTFQUVNSLHOH6WRJ2WQDPF35FE --info
```

Hexadecimal SHA-1:

```bash
python ccwget.py --sha1 67df35fd332c2956c96771fd68a75680de5df4a4 --info
```

Search all crawls:

```bash
python ccwget.py -1 67df35fd332c2956c96771fd68a75680de5df4a4 --alltime --info
```

SHA-1 search prints every matching occurrence as `YYYY-MM-DDTHH:MM:SS:sha1:url` and does not download payloads by
default. Use `-d` or `-S` when WARC content or headers are required.

Download every record matching hash:

```bash
python ccwget.py -1 67df35fd332c2956c96771fd68a75680de5df4a4 -O matching-content.bin
```

Multiple matches create suffixed files.

### Select search period

Default behavior searches physical `CCMAINYYYYWW` tables covering the previous two years, newest first.
Use `--help-time` to display the CIRCL logo and all search-time modifiers. `--alltime` searches data since 2015.

Search one year using its physical crawl tables:

```bash
python ccwget.py -l www.circl.lu --year 2024
```

Search every physical crawl table:

```bash
python ccwget.py -l www.circl.lu --alltime
```

Do not combine `--year` and `--alltime`; `--alltime` wins.

## Direct development HTTP API

These endpoints are development-only and currently unauthenticated.

### Query URL metadata

The host is matched exactly; an apex domain is never changed to or searched as its `www` variant. A URL with no
explicit path is searched as the root path and matches records stored with either `/` or a NULL path.

```bash
curl --get http://127.0.0.1:4321/query \
  --data-urlencode 'url=https://circl.lu/pub/tr-73/' \
  --data-urlencode 'info_only=true'
```

### List FQDN

```bash
curl --get http://127.0.0.1:4321/list-fqdn \
  --data-urlencode 'fqdn=www.circl.lu' \
  --data-urlencode 'brief=true'
```

### List domain across all crawls

```bash
curl --get http://127.0.0.1:4321/list-domain \
  --data-urlencode 'domain=.circl.lu' \
  --data-urlencode 'alldataset=true' \
  --data-urlencode 'all=true' \
  --data-urlencode 'brief=true'
```

### Enumerate domain hosts

```bash
curl --get http://127.0.0.1:4321/domain-enum \
  --data-urlencode 'domain=.circl.lu' \
  --data-urlencode 'alldataset=true' \
  --data-urlencode 'all=true'
```
