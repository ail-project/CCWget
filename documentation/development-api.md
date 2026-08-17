# Development HTTP API

These endpoints are development-only and currently unauthenticated.

## Query URL metadata

The host is matched exactly; an apex domain is never changed to or searched as its `www` variant. A URL with no
explicit path is searched as the root path and matches records stored with either `/` or a NULL path.

```bash
curl --get http://127.0.0.1:4321/query \
  --data-urlencode 'url=https://circl.lu/pub/tr-73/' \
  --data-urlencode 'info_only=true'
```

## List FQDN

```bash
curl --get http://127.0.0.1:4321/list-fqdn \
  --data-urlencode 'fqdn=www.circl.lu' \
  --data-urlencode 'brief=true'
```

## List domain across all crawls

```bash
curl --get http://127.0.0.1:4321/list-domain \
  --data-urlencode 'domain=.circl.lu' \
  --data-urlencode 'alldataset=true' \
  --data-urlencode 'all=true' \
  --data-urlencode 'brief=true'
```

## Enumerate domain hosts

```bash
curl --get http://127.0.0.1:4321/domain-enum \
  --data-urlencode 'domain=.circl.lu' \
  --data-urlencode 'alldataset=true' \
  --data-urlencode 'all=true'
```

## End-to-end examples

Examples below use captures present in the July 2026 Common Crawl index. The
index can change; use the same commands to reproduce current results.

### Retrieve an older CIRCL page

`-e` lists occurrences without downloading the page body. Select the capture
from 10 July 2025 with an exact timestamp:

```bash
./ccwget.py -e https://www.circl.lu/team/ \
  --at 2025-07-10T23:41:03
```

Output:

```text
2025-07-10T23:41:03:d956eaebd50f40a21dde657ada80e5a8fd1b2ab8:https://www.circl.lu/team/
```

The client converts the Common Crawl Base32 digest
`3FLOV26VB5AKEHO6MV5NVAHFVD6RWKVY` to the hexadecimal SHA-1 shown above.

### List the CIRCL domain

`-ld` prints the same `timestamp:digest:url` format as `-e`:

```bash
./ccwget.py -ld circl.lu --year 2026
```

Example first rows:

```text
2026-07-15T20:23:25:d2abd1053ad96c621ffb846d49f651e61637405a:https://circl.lu/
2026-07-21T01:10:06:d2abd1053ad96c621ffb846d49f651e61637405a:https://circl.lu/
2026-07-10T07:32:13:d7579e47fb24998e1fe447b1079d49ff071b40e2:https://www.circl.lu/advisory/CVE-2015-4099/
```

Save the complete listing as CSV:

```bash
./ccwget.py -ld circl.lu --year 2026 -O circl.csv
head -n 3 circl.csv
```

```csv
timestamp,digest,url
2026-07-15T20:23:25,d2abd1053ad96c621ffb846d49f651e61637405a,https://circl.lu/
2026-07-21T01:10:06,d2abd1053ad96c621ffb846d49f651e61637405a,https://circl.lu/
```

Use `-d` when full index metadata is needed instead of occurrence lines.

### Find `www.perdu.com` by SHA-1

First list an occurrence to obtain its digest:

```bash
./ccwget.py -e https://www.perdu.com/ --year 2026
```

Example:

```text
2026-07-19T00:21:24:fcaaccd9c2334ec09f615d70dee7be3f591d1893:https://www.perdu.com/
```

Search every indexed occurrence carrying that content SHA-1:

```bash
./ccwget.py -1 fcaaccd9c2334ec09f615d70dee7be3f591d1893 --alltime
```

Without `-O`, the SHA-1 query prints every matching occurrence and does not
download payloads. With `-O`, only the first result returned by the service is
downloaded:

```bash
./ccwget.py -1 fcaaccd9c2334ec09f615d70dee7be3f591d1893 \
  --alltime -O perdu-content.bin
```
