# Sedis

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![written in Skarn](https://img.shields.io/badge/written%20in-Skarn-0b7d74.svg)](https://github.com/stefan-zobel/Skarn)

A key-value server written in [Skarn](https://github.com/stefan-zobel/Skarn) that speaks RESP, the
protocol of Redis. Redis' own tools -- `redis-cli`, `redis-benchmark`, the client libraries -- can talk
to it unchanged.

It is not meant to compete with Redis. It is meant to show that a real server with real clients can be
written in Skarn: many connections at once, pipelining, keys spread over several cores, keys that
expire, data that survives the process, and parts that crash and come back without the rest noticing.

> **Status: a demonstration, not a production store.** It is tested (see [Tests](#tests)) and it is
> measured ([BENCHMARK.md](BENCHMARK.md)), but it has no replication, no clustering, no authentication
> and no TLS, and it has run on one operating system. Treat it as a worked example of the language.

* **AI-Generated Codebase:** This entire project was designed and implemented by Opus 5.5.

![How Sedis is built](docs/images/architecture.svg)

## Getting it

Sedis is a Skarn program, so what it needs is the Skarn driver `skarnvm` -- one executable, no runtime
to install.

1. Get `skarnvm` from [the Skarn releases](https://github.com/stefan-zobel/Skarn/releases), or build it
   from source: clone Skarn and build `vMachine.sln` with Visual Studio 2022 (x64, Release) on Windows,
   or `cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build` on macOS. The Windows binaries
   need a CPU with AVX2.
2. Clone this repository.
3. Run it:

```
skarnvm sedis.skn --port=6379 --dir=./data
```

There is nothing to compile here: `skarnvm` type-checks and runs the `.skn` sources directly.

## Running it

From this directory, with the Skarn driver `skarnvm` on the path or named in full:

```
skarnvm sedis.skn [--port=P] [--port-file=F] [--shards=N] [--dir=D] [--appendfsync=P]
                  [--rewrite-min=B] [--logfile=F] [--enable-debug]
skarnvm sedis.skn --selftest [--dir=D]
```

| option | meaning |
|---|---|
| `--port=P` | the port to listen on; 6379 by default, 0 lets the system pick a free one |
| `--port-file=F` | write the port to file `F` once listening |
| `--shards=N` | how many shard actors hold the keys; 4 by default |
| `--dir=D` | keep the data in directory `D` (see "Persistence"); without it, in memory only |
| `--appendfsync=P` | when the log reaches the disk: `always`, `everysec` (the default) or `no` |
| `--rewrite-min=B` | rewrite a shard's log on its own once it has doubled and holds `B` bytes or more; 64 MiB by default |
| `--logfile=F` | also write the server's messages to file `F`, with a time stamp and a level on each line |
| `--enable-debug` | allow `DEBUG CRASH key`, which makes the shard holding `key` crash |
| `--selftest` | start a server on a free port (on the data in `D` with `--dir`), run a scripted conversation against it in the same process, and exit 0 if every answer was the expected one |

Then, for example:

```
redis-cli -p 6379 set greeting "hello world"
redis-cli -p 6379 get greeting
```

The server runs until the process is ended (Ctrl+C).

## Commands

**Strings**

| command | answer |
|---|---|
| `GET key` | the value, or nil |
| `SET key value [EX s \| PX ms \| EXAT s \| PXAT ms \| KEEPTTL] [NX \| XX] [GET]` | `OK`; nil if `NX`/`XX` prevented it; the old value with `GET` |
| `INCR key` / `DECR key` | the new value; a missing key counts as 0 |
| `INCRBY key n` / `DECRBY key n` | the new value |
| `APPEND key value` | the new length |
| `STRLEN key` | the length, 0 for a missing key |
| `MGET key...` | the values, nil for a missing key or one that holds no string |
| `MSET key value...` | `OK` |

**Keys and expiry**

| command | answer |
|---|---|
| `DEL key...` | how many keys existed |
| `EXISTS key...` | how many of the keys exist, a key named twice counted twice |
| `TYPE key` | `string`, `hash`, `list` or `none` |
| `KEYS pattern` | the keys that match: `*`, `?`, `[abc]`, `[^abc]`, `[a-z]`, `\x` |
| `EXPIRE key s [NX \| XX \| GT \| LT]` / `PEXPIRE key ms [...]` | 1 if the time was set, else 0; a time in the past deletes the key |
| `EXPIREAT key s [...]` / `PEXPIREAT key ms [...]` | the same, with a moment in Unix time |
| `TTL key` / `PTTL key` | seconds / milliseconds left; -1 without a time to live, -2 for no key |
| `PERSIST key` | 1 if the time to live was removed |
| `DBSIZE` | the number of keys |
| `FLUSHDB` / `FLUSHALL` | `OK`; all keys are gone |
| `BGREWRITEAOF` | `OK` once every shard has rewritten its log (see "Persistence") |

**Hashes**

| command | answer |
|---|---|
| `HSET key field value...` | how many fields were new |
| `HGET key field` | the value, or nil |
| `HDEL key field...` | how many fields were removed; the key goes with the last one |
| `HGETALL key` / `HKEYS key` / `HVALS key` | fields and values / fields / values |
| `HEXISTS key field` / `HLEN key` | 1 or 0 / the number of fields |
| `HINCRBY key field n` | the new value |

**Lists**

| command | answer |
|---|---|
| `LPUSH key value...` / `RPUSH key value...` | the new length |
| `LPOP key [count]` / `RPOP key [count]` | the value, or the values with a count; the key goes with the last one |
| `LRANGE key start stop` | the values from `start` to `stop`, both included; negative counts from the end |
| `LINDEX key i` / `LLEN key` | one value / the length |

**The connection**

| command | answer |
|---|---|
| `PING [message]` / `ECHO message` | `PONG`, or the message |
| `QUIT` | `OK`, and the server closes the connection |
| `SELECT 0` | `OK`; there is only database 0 |
| `DEBUG CRASH key` | the shard holding `key` crashes (only with `--enable-debug`; see "Let it crash") |
| `SEDIS.GC [RESET]` | per shard: garbage collections, the longest pause, the time in all of them, the mean live data |
| `COMMAND ...`, `CONFIG GET name`, `CLIENT SETNAME/SETINFO`, `INFO` | what `redis-cli`, `redis-benchmark` and the client libraries ask for when they start |

Command names are case-insensitive. The error answers use Redis' wording, so a client that matches on
them keeps working; a command on a key of the wrong kind answers `WRONGTYPE`. An inline command — a
line of words, as typed into telnet — works too.

A key with a time to live is removed when a command next touches it after its end, and every shard
also looks for such keys ten times a second, visiting 20 of them at a time and going on while a quarter
or more of those had ended — the way Redis does it.

## Persistence

With `--dir=D`, every shard writes each change it makes to its own append-only file,
`D/shard-<i>.aof`, in RESP — the same format Redis uses — and replays that file when it starts.

- **What is written is the effect**, in a form that means the same later: `EXPIRE k 10` is written as
  `PEXPIREAT` with the moment it ends, and `SET` with `EX` as the value and that moment. A command that
  failed or changed nothing is not written.
- **`--appendfsync`** decides when the log is on the disk: `always` before the batch of commands it
  belongs to is answered, `everysec` once a second (lose at most about a second on a power cut), `no`
  whenever the operating system decides. A process that is merely killed loses nothing in any of the
  three, since what was written is in the operating system's hands.
- **A rewrite** replaces a log by the shortest one that rebuilds the same data: written to
  `shard-<i>.aof.tmp`, synced, then renamed over the old file, so a crash in between leaves one whole
  file or the other. `BGREWRITEAOF` asks for it; a log that has doubled since the last rewrite and
  holds `--rewrite-min` bytes or more is rewritten on its own.
- **At the start**, each shard replays its log before the server takes clients, and reports how many
  commands it replayed. A command cut off at the end of a log — the process died while writing it — is
  dropped and the file is rewritten; anything else that is not a valid command stops the start with the
  file's name, rather than letting the server run on half its data.
- **The number of shards is part of the data**: a key's shard depends on it. `D/sedis.meta` records it,
  and a start with a different `--shards` is refused.

## Let it crash

![Let it crash, and notice what was lost](docs/images/let-it-crash.svg)

The shards run under a supervisor (`std::supervisor`), each in a `Slot` (`std::actor`): an address that
outlives the actor behind it. When a shard crashes — a bug, a log it cannot write, or `DEBUG CRASH` —
the supervisor starts it again at the same address, the new shard replays its log, and the server says
so (`shard 2 restarted -- ...`). The other shards and every connection carry on.

- **What was on its way to the crashed shard is lost** — a slot drops the mail its actor had not read,
  so a request that kills an actor cannot kill its successor too. A connection numbers the batches it
  sends each shard, and a shard answers them in order, so the answer to batch *k* shows that every
  earlier open batch was lost. Those commands are answered with `ERR the shard restarted before it
  answered; the command may or may not have run` — it may have, if the shard crashed after writing its
  log. A connection that has waited a second for a shard sends it an empty batch, whose answer settles
  the question when nothing else follows.
- **What was in the shard comes back from its log** with `--dir`. Without it, the restarted shard starts
  empty: its part of the keyspace is gone, the others are untouched.
- **More than 5 crashes within 10 seconds** and the supervisor gives up: the server reports it and ends,
  rather than going on with a part of its data missing.

## How fast it is

Measured against the same server written in Python -- `python/pysedis.py`, as `asyncio` (one thread, the
design of Redis itself), as a thread per connection, and as that on the free-threaded Python 3.14t --
and against Memurai, a Redis 7.2 for Windows written in C++. The same load generator drives all of them
(`loadgen.skn`), and every answer is checked. Six-core laptop, three rounds, medians. The comparison in
full, with charts, is [BENCHMARK.md](BENCHMARK.md); every number is in
[bench/results-2026-09-28.md](bench/results-2026-09-28.md).

`SET`, operations per second:

| load | Sedis | Python asyncio | Python threads | Python threads, no GIL | Memurai (C++) |
|---|---|---|---|---|---|
| 1 client, one command at a time | 14 689 | 25 085 | 25 776 | 25 941 | 32 270 |
| 16 clients, one command at a time | 74 316 | 29 438 | 35 265 | 88 515 | 65 885 |
| 16 clients, pipelines of 16 | 317 640 | 214 939 | 192 429 | 235 798 | 597 506 |

**Read the ratios, not the rates.** One run, one laptop, one day -- and a laptop's day moves these
numbers by more than most changes do. In the round before this one every contender, Memurai included,
measured 12-28 % higher without a byte of its binary changing. Within a run the contenders meet the same
machine, so the comparison holds; across runs only the ratios do.

- **One client alone waits longest on Sedis**: 65 us a round trip (p99 107) against the Pythons' 36-37
  and Memurai's 29. A request crosses three threads -- the runtime's I/O thread, the connection actor, a
  shard -- and each crossing is a mailbox and a wake-up. That is the price of actors that share nothing.
  What that price is has been measured, and it is **not** that the code runs slower in the server: it is
  a fixed cost per WAKE-UP, about 10 us for two parks and 7 us for a cold actor's first work, while the
  bytecode in between is 1.11x slower than the same work without a hand-off. A thread pays it once when
  it is woken, however much work it then does -- so with one command in flight the whole cost falls on
  that one command.
- **Sixteen clients spread over the shards, and there Sedis is ahead of Memurai**: 74.3k against its
  65.9k, which is 1.13x. That is also 2.5x Python's `asyncio` and 2.1x its threads under the GIL.
  Free-threaded Python, whose threads share one dict, is 1.19x Sedis. This is the load sharding was
  built for, and the only one on which Sedis leads a C++ server.
- **With pipelining Sedis is ahead of every Python on four of the six commands** -- the three crossings
  are paid once per batch -- 1.35x the best Python on `SET`, 1.4x on `GET`, 1.6x on `MSET`; the
  free-threaded build stays ahead on `LPUSH` and `HSET`, where the value rather than the key lookup does
  the work. Memurai is 1.9x Sedis.
  **So pipeline if you can: it is the one change on the client side that pays here.** Every command in a
  batch shares one wake-up per thread instead of paying its own, and the server's own work per command
  halves -- 8.8 us for parsing and dispatching one command sent alone, 4.4 us each in batches of sixteen.
  Several connections do the same thing for the same reason.
- **Persistence costs Sedis 12-27 %**, which is what it costs Memurai (13-28 %); `asyncio` loses 29-32 %
  and the free-threaded Python 60-66 %. A batch is one write however many commands it holds, and the
  batch is what the connection read in one go.
- **A large keyspace shows the garbage collector**: with a million keys in ONE shard, a collection copies
  its 85 MB of live data and stops that shard for 72 ms -- the worst round trip a client sees. Four shards
  divide it to 14.6 ms (21 MB apiece), and the throughput there is ahead of Memurai again (77.6k against
  74.1k; the free-threaded Python is 1.25x Sedis). The others' worst round trip was 2.4-10.4 ms; the 99th
  percentile stays under half a millisecond for everyone, and Sedis with four shards has the lowest of
  them at 343 us.

To run it again:

```
python bench/bench.py --vm=path/to/skarnvm [--python=python3.14] [--python-ft=python3.14t]
                      [--memurai=memurai.exe] [--rounds=3] [--cooldown=15]
```

A laptop throttles under sustained load on all cores, and a server with many threads loses more to it
than a single-threaded one; `--cooldown` rests the machine before each contender, and the report shows
the spread of the rounds beside the median. It writes the report as Markdown and every single reading
beside it as JSON, which `bench/charts.py` draws the charts from.

## Where it differs from Redis

- **Integers have 48 bits.** A Skarn `Int` holds ±(2⁴⁷ − 1), about ±1.4 × 10¹⁴; Redis counts to 2⁶³ − 1.
  `INCR` past the limit answers Redis' overflow error, and a larger number is "not an integer or out
  of range".
- **`MSET` is not atomic across shards.** Each shard sets its own keys at once, but another client may
  see some shards' keys set before the others'.
- **`BGREWRITEAOF` is not in the background.** Each shard rewrites its own log at once and serves
  nothing else meanwhile; the other shards carry on. The answer is `OK` when all are done.
- **A log that cannot be written crashes its shard** (answering `OK` for a change that is not kept would
  be a lie), which is then started again; if it keeps failing, the server ends.
- **`DEBUG`** has only `CRASH key`, and only with `--enable-debug`.
- No RDB snapshots: the append-only file is the only persistence.
- No sets, sorted sets, streams, transactions or pub/sub.
- `INFO` answers only its server section.

## How it is built

![The path of one command](docs/images/command-path.svg)

![Sharding: which shard a key belongs to](docs/images/sharding.svg)

- **The acceptor** activates its listener, so new clients arrive as tickets in its inbox, and starts a
  connection actor for each.
- **A connection actor** activates its socket and waits on it and on its inbox at once. It decodes the
  commands with std::resp's `RespReader`, sends each to the shard its key hashes to, and writes the
  answers back in the order the commands came. Every command gets a sequence number; an answer that
  arrives early waits until the ones before it are written. All the commands of one read go to a shard
  as ONE message, and come back as one — with pipelining, one message each way instead of one per
  command. A command with several keys (`MGET`, `DEL`, `MSET`, ...) is split by shard and its answers
  are put together again.
- **A shard** owns a part of the keyspace. It is an actor with a heap of its own, so a garbage
  collection stops only the shard that needs it, and the shards run on several cores.

| file | what it is |
|---|---|
| `sedis.skn` | the program: options, and starting the server |
| `server.skn` | the acceptor and the connection actor |
| `shard.skn` | the shard actor and how a key finds its shard |
| `commands.skn` | the command table and what each command does to a keyspace |
| `deque.skn` | a ring buffer, the list type |
| `glob.skn` | Redis' glob patterns, for `KEYS` |
| `aof.skn` | the append-only file: what is written, replay, rewrite |
| `selftest.skn` | the script `--selftest` runs |
| `loadgen.skn` | a load generator for any RESP server: clients as tasks, pipelining, every answer checked |
| `python/pysedis.py` | the same server in Python, for the measurement |
| `bench/bench.py` | the measurement |
| `tests/run_tests.py` | the tests |

## Tests

```
python tests/run_tests.py [--exe path/to/skarnvm]
```

It type-checks the program, runs the self-test, and starts a real server process that a RESP client
written in Python — a second implementation of the protocol — talks to: exact bytes for every answer,
binary values, 200 pipelined commands, a command sent one byte at a time, inline commands, the error
texts, hashes, lists, `WRONGTYPE`, a key that expires, what clients ask for at start, `KEYS` with every
kind of pattern, two connections at once, `QUIT`, and a protocol error, which closes that connection
only.

Then persistence, with server processes killed hard: `always` and `everysec`, every kind of value and
expiry surviving, `BGREWRITEAOF` shrinking the logs, a rewrite past `--rewrite-min`, a command cut off
at the end of a log, a corrupt log, and data written by another number of shards. (Whether `fsync`
really reaches the disk no test can show short of pulling the plug.) And crashes: a pipeline across a
`DEBUG CRASH`, the lost commands answered as lost within a few seconds, the data back from the log, the
same address for a new connection, the restart reported, and six crashes in a row ending the server.
The self-test runs twice, in memory and on a data directory.

`redis-cli` and the Python library `redis` (redis-py) have been tried against it by hand.

## Contributing

Issues and pull requests are welcome. Two things to know before you write code:

- **The sources are formatted by the language server**, not by hand: `skarn_lsp --format file.skn`,
  80 columns, and comments wrapped to the same width. A pull request that reformats unrelated lines is
  hard to read.
- **`python tests/run_tests.py` must pass**, and anything that changes behaviour needs a case in it. The
  tests start real server processes and kill them hard on purpose; they are the reason the persistence
  and crash paths can be trusted.

## Versioning

Sedis follows the Skarn driver it is written for: a release names the `skarnvm` version it was tested
against. The RESP surface follows Redis 7.2 for the commands it has; where it deviates on purpose, see
[Where it differs from Redis](#where-it-differs-from-redis).

## License

MIT -- see [LICENSE](LICENSE).
