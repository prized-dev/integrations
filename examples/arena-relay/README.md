# Arena relay

Many viewers can fork one live contestant at the same moment and each get a working copy. With 25 viewers, the copies are ready in 3.7 s at p50 and 5.5 s at p95.

A contestant is an agent running in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=arena-relay). A viewer who wants to poke it gets a fork: a sandbox of their own with the contestant's memory and running processes. When each viewer makes their own fork call, a burst is slow. Every call takes its own checkpoint of the contestant, and the host takes them one at a time. `relay.py` sits in front. It collects viewers for a second, then makes one `count: N` fork call, which takes one checkpoint for all N copies. Each viewer gets a copy as soon as that copy answers on its URL.

This is a prototype. It's private for now.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install prized==0.1.2
export PRIZED_API_KEY=dcp_...
.venv/bin/python contestant_up.py up --label owner=me     # prints {"id": "sbx_...", "pid": ...}
.venv/bin/python relay.py --contestant sbx_... --label owner=me
# in another shell: 25 viewers within 2 s
.venv/bin/python loadtest.py relay --relay http://127.0.0.1:PORT --contestant sbx_... --viewers 25 --out run.jsonl
.venv/bin/python contestant_up.py down sbx_...
```

The key stays on the machine that runs the relay. The contestant sandbox gets no key and no environment variables. You need Python 3.10 or later and `prized` 0.1.2. The calls the SDK doesn't have yet go out through httpx, which the SDK already installs.

The relay's HTTP API:

| Call | What it does |
| --- | --- |
| `POST /v1/contestants/{id}/copies` | Held until the viewer's copy is ready. Answers `{copyId, url, browserUrl, expiresAt, batch, batchSize, waitMs}`. `url` opens the contestant's `--port` (8080) on the copy, with a port token, until the copy's TTL. |
| `DELETE /v1/copies/{id}` | Hands a copy back early. |
| `GET /v1/stats` | Counters. |

Error answers:

- `503 {"error": "full", "reason", "retryAfterS"}`: no room for the viewer in time.
- `502 {"error": "fork_failed", "reason"}`: the API refused the fork for good (the contestant is gone, for example), or 3 forks failed in a row.

## What it does

1. **Collects viewers for one window** (`--window`, 1 s). The first request for a contestant opens it. It closes early if the queue already fills the room.
2. **Forks once, at the room it has.** `count` is the queue, or `--cap` (36 live copies per contestant) minus the copies live or in flight, whichever is smaller. A fork is all or nothing. If the API answers `409 fork_capacity` with `fits`, or `429 sandbox_quota_exceeded` with `admitted`, the relay forks again at that number. The next window opens while a fork is still in flight, so forks can overlap.
3. **Hands out each copy when it answers.** This is `--ready probe`, the default. The relay forks without a hold and mints each copy's port token right away. It then polls each URL on `--probe-path` until the copy itself answers 2xx or 3xx, and hands that copy to the longest-waiting viewer. Probes go out 12 at a time, read only the status line and headers, and give up after 3 s. On a 429 they wait for the proxy's Retry-After (1-5 s). `--ready hold` uses `?wait=running` (SDK 0.1.2 doesn't send it) and hands the whole batch out when the hold answers.
4. **Queues the overflow or says `full`.** Viewers the room can't cover go back to the front of the queue for the next window. If no room frees up within `--queue-max` (10 s), they get `full`. The answer is immediate when the copies that expire before the deadline still can't cover the queue.
5. **Deletes every copy after `--ttl`** (300 s), or sooner when its viewer hands it back or hangs up.

### No copy is left behind

- **Labels and a backstop.** Every copy carries `relay=<name>`, `relay_run=<run>` and `relay_batch=<batch>`, plus your `--label`s. Its `maxRuntimeMin` is the TTL plus 2 minutes, so the API deletes it even if the relay dies.
- **A fork with no answer** (a timeout, a dropped connection, a cancel at shutdown, or a 5xx) has its batch deleted by label right away. Then the relay replays the fork with the same Idempotency-Key. The API takes the workspace lock before it checks the key, so the replay waits for the lost call to commit and answers with its copies, and the relay deletes them. If the call never arrived, the replay makes the copies and the relay deletes those. If the replay gets no answer either, the relay sweeps by label for another `fork_timeout` (30 s).
- **An error after the fork answered** deletes every copy not yet handed out. A failed token mint or probe deletes only that copy, and its viewer goes back in the queue.
- **A viewer who hangs up** gets its copy deleted, whether the copy is still being made, being written back, or already handed out.
- **Failed deletes** are retried after 0.5 s, 1 s, 2 s and 4 s, then every 5 s.
- **Reconcile.** Every 15 s, a pass deletes anything under this run that the relay doesn't hold.
- **SIGTERM** answers every waiting viewer `full` (`shutting_down`) and deletes the run. It gives in-flight forks 2 s, then cancels them, and each cancelled fork is replayed and swept as above. Then it waits for every cleanup to finish and repeats sweep and list until nothing of the run is left. That usually takes a few seconds. If the API stops answering, it can take up to 70 s.
- **Startup** deletes anything an earlier run under the same `--name` left, for example after a SIGKILL.

## Numbers

These are real runs from Nov 5, 2026, in our own workspace:

- **Contestant:** one 2 vCPU / 4 GB `prized-agent-v1` sandbox running `contestant.py`, a stand-in with no model. It touches 1.5 GB of memory, runs a CPU child and 3 sleepers, and serves a 50 ms tick counter.
- **Viewers:** they arrive uniformly within 2 s.
- **Ready:** measured on the viewer's own clock, from asking to the first 200 from its copy's URL.
- **Checks:** every served copy had the contestant's pid and all 4 of its children, and its counter kept moving.

| Viewers within 2 s | How | Runs | Ready p50 / p95 / max (s) | Served |
| --- | --- | --- | --- | --- |
| 25 | no relay: each viewer makes its own fork call with `?wait=running` | 2 | 10.2 / 17.6 / 18.4 | 50 of 50 |
| 25 | relay, `--ready hold` | 2 | 6.8 / 7.6 / 9.3 | 50 of 50 |
| 25 | **relay, default** | 2 | **3.7 / 5.5 / 6.3** | 50 of 50 |
| 50 | relay, `--ready hold`, cap 36 | 2 | 7.9 / 11.3 / 12.6 | 72 of 100. The other 28 got `full` within 4 ms. |
| 50 | **relay, default**, cap 36 | 3 | **5.7 / 8.9 / 10.6** | 108 of 150. The other 42 got `full` within 10 ms. |

Every run ended with 0 copies left. The relay's own count matched the CLI's listing, and the product database showed all 0 still running.

After the second review's fixes, one more run with 25 viewers came in at p50 4.1 s and p95 5.3 s. One viewer waited 10.8 s: the host had room for 16 copies, not 17, so that viewer was served by the next fork.

For comparison, the CLI load test on Nov 4 made 25 separate `prized sandbox fork` calls. Its fork p95 was 22.9 s and its ready p95 was 24.1 s.

### Where the time goes

- **The window** holds a viewer 0.3-0.8 s at p50.
- **Without the hold, the fork call answers in 0.3-1.5 s.** Port tokens take up to 2.4 s to mint for 25 copies, and up to 5.6 s for 36. The API's log shows every mint taking 1.5-3.9 s when 36 go at once.
- **Copies answer on their URLs from 2.8 s after the fork, but the last of a batch takes 5-10.5 s.** Before that, the proxy answers 503. In the 5 batches we checked, the product database had every copy `running` 1.3-2.3 s after its fork row was written. So up to about 8 s of a viewer's wait comes after the VM runs.
- **The hold says `running` before the URL works.** In hold mode, 17 of 72 copies at 36 viewers had URLs that weren't answering yet when the hold returned. They took up to 6 s more to answer. With 15 copies, the hold also answered about 3 s after the product database had them running. That's why probe mode is the default.
- **The preview proxy rate-limits** (429, with ReadTimeouts) when 36 copies are polled together. Earlier code polled every 0.15 s with no limit and got up to 99 429s per run. Capping probes at 12 in flight brought that down to 0-10 per run.

### Errors, on the real API, with the final code

- **Injected faults:** one batch failed after its fork answered, and 10% of token mints failed. 3 viewers hung up and 30% handed their copy back early. 28 copies were made and 0 left. All 22 viewers who stayed were served.
- **SIGTERM 2.5 s into a burst of 25:** the relay exited 0 and left no copy. Viewers still waiting got `shutting_down` within 6.2 s.
- **SIGKILL during a fork** left 25 copies. Starting the relay again under the same `--name` deleted all 25.

### Cost

Every run in this README was billed to our workspace at list price ($0.131 an hour for each 2 vCPU / 4 GB copy). The total was $1.45 for 929 copies, about 48 s each, plus two contestants. A viewer who keeps a copy for the default 5 minutes costs about $0.011.

## Limits

- **One host's room.** Copies start only on the contestant's host. That room is shared with every other sandbox there, and a copy being deleted still counts for a moment. In 2 runs the API allowed fewer than we asked for (19 of 22, then 18 of 27). Reserving room per contestant is the product's job.
- **One process.** The queue and the copies live in memory. Run one relay per `--name`, because its startup sweep deletes that name's copies.
- **Billing.** The copies bill to the relay's workspace, and port tokens need a paid workspace.
- **No auth on the relay's own HTTP.** It listens on 127.0.0.1, and anyone who can reach it can delete any copy by id. Put it behind your app's auth.
- **Half-closed connections.** A client that half-closes its connection (`shutdown(SHUT_WR)`) after sending the request is treated as gone and gets no answer. HTTP clients don't do this.
- **One slow contestant can delay the others' probes**, but only up to `fork_timeout`. The 12 probe slots are shared by every contestant.

## Tests

```bash
python test_offline.py
```

22 tests run the relay against a fake API, with no key and no network beyond loopback:

- one fork per burst, the cap and fast `full` answers, and the API's `fits`
- an error after a fork, a lost fork answer swept by label, and token failures
- viewers who hang up, including at the moment their copy comes
- the reconcile pass and the startup sweep
- probe mode and per-copy handout
- a bug inside a flush, a closed stderr, and a permanent refusal answered with 502
- shutdown with viewers requeued, and shutdown with a slow fork
- the HTTP routes, including stray bytes and chunked bodies
- a copy that never finishes its headers while 12 probes are in flight
- a fork that commits after shutdown cancelled it, with and without an answer to the replay
