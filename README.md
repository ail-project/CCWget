# CCWget

**Need a tool to retrieve a 32 KB index.html from 7.5 PiB of data spanning more than a decade?**
<div align="center">
  <img src="assets/ccwget-logo-small.png" alt="CCWget logo" width="314">
</div>
CCWget is a lightweight Python client for searching and retrieving archived web
objects from a CIRCL Common Crawl indexing service.

## Why CCWget exists

[Common Crawl](https://commoncrawl.org/) publishes large, openly available web
crawls. Each indexed record can contain the URL, crawl timestamp, response
metadata, content digest, and the [WARC](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1-annotated/)
filename plus byte range needed to
retrieve the archived response.

The dataset is useful for historical and security investigations, but its size
makes downloading complete crawl collections impractical. CCWget queries the
service-side metadata index first, then retrieves only the selected [WARC](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1-annotated/) object.
The client uses the service API and does not download complete Common Crawl
archives.

## Architecture

```text
ccwget.py
    +--> Common Crawl WARC byte-range retrieval
    |
    | authenticated HTTP API and queued jobs
    v
CIRCL Common Crawl service
    |
    +--> metadata index
    +--> object cache
```

The service performs metadata searches and controls object access. Depending on
the token mode and cache state, the service either returns the object or asks
the client to retrieve the [WARC](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1-annotated/) byte range directly from
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

## Access

Access to the service is provided free of charge for eligible use cases. To
request access credentials, please contact [info@circl.lu](mailto:info@circl.lu)
with a short description of your intended use.

Requests are reviewed individually so that access can be allocated responsibly
and in line with available capacity, operational considerations, and the
service's intended purpose. Providing access is at CIRCL's discretion and
cannot be guaranteed; this approach helps maintain a reliable service for the
whole community.

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
- SHA-1 lookup using Base32 or hexadecimal digests.
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
Listing results are printed to standard output by default. Use `-O file` to
save a listing as CSV, or `-O -` to write that CSV to standard output. For
download commands, `-O file` saves the response body and `-O -` writes it to
standard output; without `-O`, the body is saved using a filename derived from
the URL. Existing output files are preserved with a numeric suffix.

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

Jobs can be submitted asynchronously with `-async`, inspected with `-status`, resumed
with `-result`, listed with `-jobs`, or cancelled with `-flush`.

For command-by-command examples, see
[documentation/customer-commands.md](documentation/customer-commands.md).


## EU Funded Project

HOPLITE is an EU-funded project to provide Law Enforcement Agencies (LEAs) and Judicial Authorities (JAs) with an intuitive platform for launching OSINT campaigns, receiving threshold-level incident alerts, and exchanging threat intelligence with trusted partners.
