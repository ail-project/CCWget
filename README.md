# CCWget

CCWget is a lightweight Python client for searching and retrieving archived web
objects from a CIRCL Common Crawl indexing service.

![CCWget logo](assets/ccwget-logo.png)

## Why CCWget exists

[Common Crawl](https://commoncrawl.org/) publishes large, openly available web
crawls. Each indexed record can contain the URL, crawl timestamp, response
metadata, content digest, and the WARC filename plus byte range needed to
retrieve the archived response.

The dataset is useful for historical and security investigations, but its size
makes downloading complete crawl collections impractical. CCWget queries the
service-side metadata index first, then retrieves only the selected WARC object.
The client does not need ClickHouse credentials and does not download complete
Common Crawl archives.

## Architecture

```text
ccwget.py
    +--> Common Crawl WARC byte-range retrieval
    |
    | authenticated HTTP API and queued jobs
    v
CIRCL Common Crawl service
    |
    +--> ClickHouse metadata index
    +--> object cache
```

The service performs metadata searches and controls object access. Depending on
the token mode and cache state, the service either returns the object or asks
the client to retrieve the WARC byte range directly from
`https://data.commoncrawl.org/`. Repeated retrievals can use the service-side
object cache.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config_client.yaml.sample config_client.yaml
```

The client reads `config_client.yaml` from the repository root. Environment
variables override the file:

```bash
export CCWGET_SERVICE_URL=https://commoncrawl.example.org
export CCWGET_TOKEN="your-token"
```

`CCWGET_CLIENT_CONFIG` selects another configuration file. Keep live tokens
outside version control.

## Quick start

```bash
# Retrieve the latest indexed version of a URL
python ccwget.py https://www.circl.lu/

# Print indexed metadata without downloading the response body
python ccwget.py https://www.circl.lu/ --info

# List all indexed pages for one FQDN
python ccwget.py --list-fqdn www.circl.lu

# Search URL substrings in the hostname field
python ccwget.py -s circl --fqdn --alltime

# Search by content digest
python ccwget.py --sha1 67df35fd332c2956c96771fd68a75680de5df4a4 --alltime

# Extract passive-DNS observations
python ccwget.py -pdns www.circl.lu --alltime -O pdns.csv
```

## Search capabilities

- Exact URL lookup, with optional `--all` retrieval of every occurrence.
- Historical URL enumeration with `--enumerate`.
- FQDN and domain page listings with `--list-fqdn` and `--list-domain`.
- FQDN enumeration below a domain with `--domain-enumeration`.
- Substring search with field selectors `--fqdn`, `--path`, and `--query`.
- SHA-1 lookup using Common Crawl Base32 or hexadecimal digests.
- Passive-DNS-style IP and observation ranges with `-pdns`.
- Date selection with `--after`, `--before`, `--on`, `--at`, `--time-range`,
  `--year`, and `--alltime`.

Without an explicit timeframe, the client searches the previous two years.
Use `--alltime` when a full indexed-history search is intended.

## Output formats

`-O` saves response bodies for download commands. For listing commands it
writes CSV instead:

- URL occurrences: `timestamp,digest,url`.
- Domain listings: `url`.
- FQDN enumeration: `fqdn`.
- PDNS: `ip,first_seen,last_seen,fqdn`.

URLs are always quoted in CSV output so query strings and commas remain valid.
Existing output files are preserved with a numeric suffix. Use `-O -` to write
a downloaded response or CSV listing to standard output.

## Performance notes

Substring search is the most expensive search mode. It can scan many physical
tables and may take a long time, especially for short terms. Terms must contain
at least four characters; results are limited to 500 by default. Prefer
`--fqdn`, `--path`, or `--query`, use a timeframe, and add `--limit` when a
smaller result set is sufficient.

## Troubleshooting and diagnostics

```bash
python ccwget.py --help
python ccwget.py --help-textsearch
python ccwget.py --help-time
python ccwget.py -v https://www.circl.lu/
python ccwget.py -vv https://www.circl.lu/
```

`Ctrl-C` requests cancellation of the active server-side job. Jobs can also
be submitted asynchronously with `-async`, inspected with `-status`, resumed
with `-result`, listed with `-jobs`, or cancelled with `-flush`.

For command-by-command examples, see
[documentation/customer-commands.md](documentation/customer-commands.md).
