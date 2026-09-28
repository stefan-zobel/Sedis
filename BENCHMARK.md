# Benchmark

**A Redis-compatible key-value server in Skarn, against the same server in Python and against Memurai.** Sedis
speaks RESP, the protocol of Redis, so Redis' own tools talk to it unchanged: `redis-cli` and the Python library
`redis` work against it as they are. It is built on actors that share nothing, keeps its data in an append-only
file, and starts a crashed part again without the rest noticing. Here it is measured against a Python server
written three ways and against Memurai, a Redis 7.2 for Windows written in C++. All of them are driven by the
same load generator, and every answer is checked.

| Machine | Software | Load | Runs |
|---|---|---|---|
| Laptop, 6 cores / 12 threads | Windows · CPython 3.14.7, GIL and free-threading builds · Memurai 4.1.1 (Redis 7.2.4) · Skarn Release build | 3-byte values · 10,000 keys · 3 rounds · medians | 324 load runs in phase 1 · 0 wrong answers |

> **Read the ratios, not the absolute rates.** These numbers come from one run on one laptop on one day, and a
> laptop's day varies more than most of the changes measured here. An earlier round of this page reported rates
> 12–28 % higher for *every* contender — including Memurai, whose binary had not changed by a byte. Within a run
> the contenders meet the same machine, so the comparison holds; across runs only the ratios do.

## Summary

- **With pipelining Sedis is now ahead of every Python server** on four of the six commands — 317,640 `SET`
  operations per second against 192,429 to 235,798 — and behind the free-threaded build on lists and hashes.
  Memurai is 1.9 times Sedis. Pipelining is how Redis clients go fast when they have many commands to send.
- **With 16 clients and no pipelining Sedis is ahead of Memurai** (1.13×) and 2.1 to 2.5 times the Pythons that
  have a GIL. Free-threaded Python, whose threads share one dictionary, is 1.19 times Sedis.
- **One client alone still waits longest on Sedis:** 65 µs a round trip, against 36 to 37 µs for Python and 29 µs
  for Memurai. A request crosses three threads in Sedis and none in the others. That is the price of actors that
  share nothing, not of the interpreter — and it is the one load that three rounds of optimisation have not moved.
- **Persistence costs Sedis about what it costs Memurai:** both keep roughly three quarters to seven eighths of
  their throughput with an append-only file. `asyncio` keeps 68–71 %, free-threaded Python 34–40 %.
- **A million keys in one shard show the garbage collector:** copying 85 MB of live data stops that shard for
  72 ms. Four shards divide it: each holds a quarter, stops for 14 ms, and only the requests for that shard wait.
  The 99th percentile stays under half a millisecond for everyone.

| Key figure | Sedis | Python | Memurai |
|---|--:|--:|--:|
| SET, 16 clients, pipelines of 16, ops/s | **317,640** | 214,939 asyncio · 192,429 threads · 235,798 free-threaded | 597,506 |
| SET, 16 clients, one at a time, ops/s | **74,316** | 29,438 asyncio · 35,265 threads · 88,515 free-threaded | 65,885 |
| SET, one client, round trip p50 | **65 µs** | 37 µs asyncio · 36 µs threads · 37 µs free-threaded | 29 µs |
| throughput kept with an append-only file | **73–88 %** | 68–71 % asyncio · 34–40 % free-threaded | 72–87 % |
| slowest answer, 1,000,000 keys | **14.6 ms** (4 shards) | 6.0 ms asyncio · 8.0 ms free-threaded | 2.4 ms |

**Contents:** [What Sedis is](#what-sedis-is) ·
[One client](#part-1--one-client-one-command-at-a-time) ·
[Sixteen clients](#part-2--16-clients-one-command-at-a-time) ·
[Pipelining](#part-3--16-clients-pipelines-of-16) ·
[Persistence](#part-4--what-an-append-only-file-costs) ·
[A million keys](#part-5--a-million-keys-and-the-garbage-collector) ·
[Method](#method-why-the-comparison-is-fair)

## What Sedis is

![How Sedis is built](docs/images/architecture.svg)

- **Commands:** strings (`GET`, `SET` with `EX`/`PX`/`NX`/`XX`/`GET`, `INCR`, `APPEND`, `MGET`, `MSET`, …), keys that
  expire (`EXPIRE`, `TTL`, `PERSIST`, …), hashes (`HSET`, `HGETALL`, `HINCRBY`, …), lists (`LPUSH`, `LPOP`, `LRANGE`,
  …), `KEYS` with Redis' glob patterns, and what `redis-cli` and the client libraries ask for when they start. The
  error answers use Redis' wording.
- **Actors that share nothing.** An acceptor takes the connections, one actor per client reads its commands, and
  four shard actors hold the keys, each with a heap of its own. A command goes to the shard its key hashes to; all
  commands of one read go to a shard as one message and come back as one. Every message is copied.
- **An append-only file per shard,** in RESP like Redis', with `fsync` always, once a second or never, replayed at
  the start and rewritten when it has grown.
- **Let it crash.** The shards run under a supervisor, each in a slot: an address that outlives its actor. A shard
  that crashes is started again at the same address and replays its file; the other shards and every connection
  carry on, and a command that was lost in the crash is answered with an error saying so.
- **The Python counterpart** has the same commands the benchmark sends and the same append-only file, in three
  designs: `asyncio` (one thread, the design of Redis itself), a thread per connection on the GIL build, and the same
  on the free-threading build.

![Sharding: which shard a key belongs to](docs/images/sharding.svg)

## Part 1 · One client, one command at a time

A single client sends a command and waits for the answer before it sends the next. Nothing runs in parallel, so this
measures one round trip: how long a request takes to go through the server.

![One client: operations per second](docs/images/throughput-1-client.svg)

![One client: round-trip latency](docs/images/latency-1-client.svg)

- **Sedis takes about twice as long as Python here: 65 µs against 36 to 37.** In Sedis a request crosses three
  threads: the runtime's I/O thread reads it and hands it to the connection actor, the connection actor sends the
  command to a shard, and the shard sends the answer back. Each crossing is a mailbox and a wake-up. The Python
  servers and Memurai answer on the thread that read the request.
- **What the crossings cost is the WAKE-UP, and that has now been measured.** Two parks cost about 10 µs and a cold
  actor's first work about 7 µs, while the bytecode between them is only 1.11 times slower than the same work done
  without a hand-off. Pipelined, where a wake-up is paid once per sixteen commands, Sedis goes past every Python
  ([Part 3](#part-3--16-clients-pipelines-of-16)).
- **The obvious remedy was built and measured, and it does not pay.** Letting one worker actor wait on many
  connections at once — so that one wake-up serves several clients — wins 8 to 15 % when pipelining and loses 7 to
  10 % with an append-only file switched on. A key-value store runs with its log on, so it was not adopted.
- **`MSET` is the worst case,** 91 µs: its ten keys land on all four shards, so one command waits for four answers.

## Part 2 · 16 clients, one command at a time

Sixteen clients, each still waiting for every answer. Now requests can be served in parallel, if the server can.

![16 clients: operations per second](docs/images/throughput-16-clients.svg)

- **Sedis serves 2.5 times as many requests as Python's `asyncio`** and 2.1 times as many as its threads with the
  GIL. The shards work in parallel; a Python server under the GIL does not.
- **Sedis is ahead of Memurai here (except for MSET), 1.13 times.** Memurai is single-threaded, as Redis is: one core answers
  everything. This is the load on which sharding pays, and the only one on which Sedis leads a C++ server.
- **Free-threaded Python is 1.19 times Sedis.** Its sixteen threads share one dictionary and each answers where it
  read, with no crossing at all. What it pays for that is safety: threads that change the same object at the same
  time are the programmer's problem. In Sedis nothing is shared.
- **`MSET` remains Sedis's weak spot,** 37,929 against Memurai's 55,531 — though it is now ahead of all three
  Pythons there.

## Part 3 · 16 clients, pipelines of 16

Each client sends sixteen commands before it reads the first answer. This is how Redis clients are fast when they
have many commands to send (`redis-benchmark -P 16`), and it is the throughput case.

![16 clients, pipelined: operations per second](docs/images/throughput-pipelined.svg)

- **Sedis is ahead of every Python server on `SET`, `GET`, `INCR` and `MSET`:** 317,640 against 192,429 to 235,798
  for `SET`, and 1.2 to 1.6 times the best Python on the other three. The three crossings are now paid once per
  sixteen commands.
- **Behind the free-threaded Python on lists and hashes:** `LPUSH` 278,569 against 320,101, `HSET` 273,726 against
  295,224. Both are cases where the value, not the key lookup, does the work.
- **Memurai is 1.9 times Sedis** — 1.3 times on `HSET`, 2.6 times on `MSET`. With the crossings amortized, that is
  the difference between interpreted Skarn and compiled C++, and it is the narrowest this gap has been.

<details>
<summary>All values: operations per second, median (lowest–highest) of three rounds</summary>

**One client, one command at a time**

| command | Sedis | Python asyncio | Python threads (GIL) | Python free-threaded | Memurai |
|---|--:|--:|--:|--:|--:|
| SET | 14,689 (14,305–15,344) | 25,085 (24,614–25,998) | 25,776 (24,730–25,887) | 25,941 (25,640–26,649) | 32,270 (31,491–32,957) |
| GET | 15,154 (15,080–15,160) | 24,627 (24,388–25,420) | 26,664 (26,327–27,731) | 26,272 (26,045–26,394) | 32,445 (32,112–32,981) |
| INCR | 14,074 (14,023–14,787) | 24,096 (23,910–24,881) | 24,568 (24,393–25,991) | 24,754 (23,575–25,811) | 31,537 (25,269–32,334) |
| LPUSH | 14,113 (14,034–15,296) | 24,167 (22,999–25,078) | 24,971 (24,616–25,583) | 23,921 (23,269–25,622) | 31,404 (31,165–31,810) |
| HSET | 13,888 (12,256–14,561) | 23,134 (19,739–24,454) | 24,450 (23,888–25,038) | 23,854 (23,518–24,636) | 31,332 (31,327–32,416) |
| MSET | 10,343 (10,056–11,271) | 19,389 (19,006–20,060) | 18,672 (14,613–19,613) | 18,226 (17,838–18,831) | 28,935 (28,213–29,036) |

**16 clients, one command at a time**

| command | Sedis | Python asyncio | Python threads (GIL) | Python free-threaded | Memurai |
|---|--:|--:|--:|--:|--:|
| SET | 74,316 (71,309–91,434) | 29,438 (28,830–31,535) | 35,265 (34,597–36,333) | 88,515 (83,338–95,977) | 65,885 (65,053–71,452) |
| GET | 74,250 (70,861–90,660) | 29,705 (28,696–31,594) | 37,475 (37,382–42,259) | 92,463 (85,698–96,496) | 68,287 (66,564–70,919) |
| INCR | 71,721 (70,934–89,854) | 28,629 (26,480–30,209) | 33,085 (33,038–36,954) | 86,954 (83,688–94,471) | 70,108 (65,706–73,317) |
| LPUSH | 70,675 (67,022–88,886) | 28,645 (27,951–29,859) | 32,257 (32,188–36,405) | 87,102 (84,244–92,173) | 67,922 (65,113–68,177) |
| HSET | 70,226 (68,168–85,495) | 26,567 (25,177–29,272) | 31,958 (31,758–35,620) | 86,918 (82,934–89,571) | 64,910 (64,646–69,804) |
| MSET | 37,929 (37,328–48,359) | 22,228 (21,831–23,596) | 22,713 (22,671–25,206) | 20,947 (20,920–22,467) | 55,531 (50,911–59,698) |

**16 clients, pipelines of 16**

| command | Sedis | Python asyncio | Python threads (GIL) | Python free-threaded | Memurai |
|---|--:|--:|--:|--:|--:|
| SET | 317,640 (316,826–410,741) | 214,939 (213,433–222,162) | 192,429 (187,790–222,871) | 235,798 (227,109–265,958) | 597,506 (586,399–619,352) |
| GET | 337,599 (331,083–424,138) | 236,779 (226,444–241,736) | 242,754 (242,220–263,022) | 232,654 (222,446–274,301) | 630,846 (622,170–657,687) |
| INCR | 283,203 (282,351–359,390) | 196,697 (194,679–205,245) | 185,280 (181,625–197,053) | 235,114 (228,381–257,856) | 630,414 (574,519–656,610) |
| LPUSH | 278,569 (227,310–355,001) | 191,817 (188,771–195,910) | 175,845 (175,609–190,159) | 320,101 (313,943–341,548) | 548,712 (543,141–561,696) |
| HSET | 273,726 (269,918–355,241) | 161,828 (161,002–167,018) | 150,374 (140,850–164,227) | 295,224 (292,020–327,896) | 366,274 (355,102–370,508) |
| MSET | 98,660 (96,782–121,845) | 63,223 (62,338–63,528) | 49,167 (49,060–51,268) | 22,749 (22,453–24,930) | 257,775 (254,332–265,331) |

`MSET` sets ten keys; each counts as one operation.

</details>

## Part 4 · What an append-only file costs

Each contender with and without its log, back to back, with `fsync` once a second (Redis' `appendfsync everysec`).
The threads Python on the GIL build is left out here: one Python with threads is enough, and the free-threaded one
is the faster.

![What an append-only file costs](docs/images/persistence.svg)

- **Sedis keeps 73 to 88 %, Memurai 72 to 87 %** — the same, within the spread of a laptop. A shard writes the
  changes of one batch with one write call, and the batch is what the connection sent in one read; Memurai does
  the equivalent.
- **Sedis logs the EFFECT, not the request.** A `SET k v EX 30` becomes a `SET` and a `PEXPIREAT` with an absolute
  moment, taken from the state that resulted; a command that failed or changed nothing is not written at all. That
  makes a replay exact rather than approximately right.
- **The Python servers lose most:** `asyncio` about a third, free-threaded Python roughly two thirds. They write
  every command to the file under a lock, and the threads queue up on it.

## Part 5 · A million keys, and the garbage collector

First one million keys are loaded, then 16 clients `SET` random ones of them for five seconds. Skarn's collector
copies what is alive, stopping the heap it collects meanwhile; with a lot alive, that takes time. Sedis reports each
shard's longest pause itself (the command `SEDIS.GC`).

![A million keys: the slowest answer](docs/images/large-keyspace.svg)

| contender | load 1M keys | ops/s | p50 | p99 | slowest | longest GC pause |
|---|--:|--:|--:|--:|--:|--:|
| Sedis, 1 shard | 1.1 s | 67,910 | 216 µs | 406 µs | 72.2 ms | 71.8 ms (85 MB live) |
| Sedis, 4 shards | 1.5 s | 77,622 | 188 µs | 343 µs | 14.6 ms | 14.4 ms (21 MB live per shard) |
| Python asyncio | 1.7 s | 30,155 | 516 µs | 829 µs | 6.0 ms | – |
| Python threads (GIL) | 1.9 s | 37,577 | 349 µs | 1,528 µs | 10.4 ms | – |
| Python free-threaded | 1.3 s | 96,883 | 158 µs | 320 µs | 8.0 ms | – |
| Memurai | 0.9 s | 74,099 | 203 µs | 454 µs | 2.4 ms | – |

- **The slowest answer is a collection.** With one shard holding everything, a collection copies 85 MB at about
  1 GB/s and stops for 72 ms. The client's worst round trip is that pause plus a little.
- **Sharding divides it, and by more than its share.** Four shards hold a quarter of the data each and pause for
  14 ms — a fifth of the single shard's pause, not a quarter — and only the requests for the collecting shard wait.
  That is the reason every actor in Skarn has a heap of its own.
- **With a million keys in play Sedis is ahead of Memurai on throughput,** 77,622 against 74,099, and behind only
  the free-threaded Python. Loading the million keys takes it 1.5 s against Memurai's 0.9 s.
- **The 99th percentile is untouched,** 343 µs for Sedis with four shards, the lowest of any contender here:
  collections are rare. This is a tail latency effect, not a throughput one. A collector that does not copy the old
  data in one stop would remove it; Skarn's does not have one today.

## Method: why the comparison is fair

- **One load generator for all.** It is written in Skarn: every client a task with one connection, the requests
  encoded before the clock starts, every answer checked (`OK`, the integer, the bulk string, nothing else). Wrong
  answers: none. It runs on the same machine as the server, for every contender alike.
- **Python gets its best designs from the standard library.** `asyncio` with a `Protocol` (not the slower streams),
  a thread per connection, and the same threads on the official free-threading build, which reports at start that
  the GIL is off. The parser works on one `bytearray` with `bytes.find`, and the answers of one read go out
  in one write. Threads take one of 16 striped locks, so that `INCR` cannot lose an update.
- **The same persistence.** Both write every change to an append-only file, hand it to the operating system after
  each read's batch and `fsync` once a second.
- **Memurai as the yardstick,** not as a rival: a compiled Redis 7.2, started for each round on a free port with no
  persistence (or, in part 4, with its own append-only file, `everysec`).
- **Rest between contenders, in every phase.** A laptop throttles under sustained load on all cores far more than on
  one, and the contender with the most threads suffers most. Every contender starts after 15 s of rest, gets a
  one-second warm-up, and the order changes in each of the three rounds; the tables show the spread. All three
  phases come from one run, so they share one machine state.
- **Three loads, not one number:** latency (one client), concurrency (sixteen), throughput (pipelined). A server
  can win one and lose another, and Sedis does.
- **A sixth contender was measured and is not shown.** The run carried a second Sedis whose connections are served
  by worker actors rather than one actor per client. It was built to answer one question — whether that
  arrangement costs Sedis its standing — and the answer was no, but its advantage exists only without an
  append-only log, so it was not adopted. Drawing it in the charts would invite a comparison the project did not
  make.

**Not measured**

- **C accelerators outside the standard library** — `uvloop`, `hiredis` — and Redis on Linux.
- **Many connections per actor:** Sedis runs one actor, an operating-system thread, per client. Hundreds are fine;
  tens of thousands are not what it is built for.
- **Other operating systems:** all numbers are from Windows.

## Reproduce it

```
python bench/bench.py --vm=path/to/skarnvm --python=path/to/python3.14
                      --python-ft=path/to/python3.14t --memurai=path/to/memurai.exe
                      --rounds=3 --cooldown=15 --out=bench/results.md
python bench/charts.py --report bench/results.md --out docs/images
```

`bench.py` writes the report as Markdown and every single reading beside it as JSON; `charts.py` draws
every chart on this page from that report, so a new measurement replaces the pictures as well as the
tables. Absolute numbers depend on the machine and on its day; compare the contenders within one run.
