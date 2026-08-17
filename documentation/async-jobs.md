# Asynchronous jobs

Use asynchronous mode when a search may take longer than an interactive
session. The client submits the operation, prints a job ID, and exits. The
server keeps the job associated with the configured token.

## Submit a job

Combine `-async` with one search command. Example: search every indexed
occurrence of `www.circl.lu` without waiting:

```bash
./ccwget.py -async -e https://www.circl.lu/ --alltime
```

Output is the retained job identifier:

```text
019f7abc-1234-7abc-8def-0123456789ab
```

The identifier is required by `-status` and `-result`. Keep it if the command
is going to run unattended.

## Inspect one job

```bash
./ccwget.py -status 019f7abc-1234-7abc-8def-0123456789ab
```

Example output while queued:

```text
Job: 019f7abc-1234-7abc-8def-0123456789ab
Operation: query
Action: enumerate-url
State: WAITING
Elapsed: 12sec
Queue position: 2
Tables: 0/0
```

`-status` is read-only. It never downloads WARC data and never consumes the
retained result.

## List retained jobs

```bash
./ccwget.py -jobs
```

Example:

```text
Job ID                           Operation     State             Age     Queue  Tables
-------------------------------------------------------------------------------------------
019f7abc-1234-7abc-8def-0123456789ab query         WAITING              12s       2     0/0
019f6def-5678-7def-8abc-9876543210fe list-domain   DONE                 94s       -    24/24
  Dataset rows: 58,403,552,305
```

Only jobs belonging to the configured client token are listed. Results remain
retained until normal cleanup or successful `-result` consumption.

## Resume and consume a result

```bash
./ccwget.py -result 019f7abc-1234-7abc-8def-0123456789ab
```

For an active job, `-result` reports its current state and exits immediately:

```text
Job: 019f7abc-1234-7abc-8def-0123456789ab
Operation: query
Action: enumerate-url
State: RUNNING
Tables: 8/24
Dataset rows: 58,403,552,305
```

For a terminal job, `-result` renders the saved result using the original
operation. Output options can be supplied again:

```bash
./ccwget.py -result 019f6def-5678-7def-8abc-9876543210fe -O circl.csv
```

Successful terminal-result consumption deletes the retained job. Failed
rendering, downloading, or network requests leave it available for retry.

## Cancel jobs

During a synchronous search, press `Ctrl-C`. The client requests cancellation
of that submitted job with `DELETE /jobs/<job_id>`. A
waiting job becomes `CANCELLED`; a running job becomes `CANCEL_REQUESTED` and
stops at its next cancellation checkpoint.

Cancel all active jobs owned by the configured token:

```bash
./ccwget.py -flush
```

Example:

```text
Waiting cancelled: 1
Running cancelled: 0
```

`-flush` cannot cancel another client’s jobs.

## Help and diagnostics

The compact CLI help is available with:

```bash
./ccwget.py --help-async
```

Use `-v` for job states and progress, `-vv` for HTTP diagnostics, and `-vvv`
for decoded JSON response traces. Tokens and authorization headers are never
printed.
