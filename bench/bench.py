"""bench.py -- Sedis against the same server in Python, and against Memurai (a Redis 7.2 for Windows).

Every contender is driven by the SAME load generator, ../loadgen.skn, so the client costs the same for
all of them. Three phases:

  1. throughput and latency: six commands (set get incr lpush hset mset) under three loads --
     1 client without pipelining (latency), 16 clients without pipelining (concurrency), 16 clients
     with 16 commands per pipeline (throughput). Every contender runs every cell in every round; the
     order of the contenders rotates from round to round; each round starts every server afresh and
     warms it up for a second first.
  2. persistence: SET and INCR with an append-only file, fsync once a second, for the contenders that
     have one.
  3. a large keyspace: 1,000,000 keys loaded, then SET on random ones of them -- tail latency where a
     garbage collector has a lot to walk. Sedis with 1 and with 4 shards, and its own GC report.

Run:  python bench.py --vm=<skarnvm> [--python=<python3.14>] [--python-ft=<python3.14t>]
                      [--memurai=<memurai.exe>] [--rounds=3] [--cooldown=15] [--phases=123]
                      [--commands=set,get,...] [--note="..."] [--out=results.md]

A laptop throttles under sustained load on all cores, and a contender with many threads (Sedis, the
threaded Pythons) suffers more than a single-threaded one: every phase therefore rests --cooldown seconds
before each contender, and phase 1 reports the spread of the rounds beside the median. --commands limits
phase 1 to some commands (a quick trial run); --note is written into the report's head.

Prints the tables (medians over the rounds) and writes them, with every raw run, to --out -- and every
single reading, of all three phases, to the same name with a .json suffix. charts.py draws from it.
"""

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEDIS = Path(__file__).resolve().parent.parent
LINE = re.compile(r"ops/s (\d+) p50us (\d+) p99us (\d+) maxus (\d+)")
ERRORS = re.compile(r"errors (\d+)")

COMMANDS = ["set", "get", "incr", "lpush", "hset", "mset"]
LOADS = [(1, 1), (16, 1), (16, 16)]

# Every reading of every round, flat, in the order it was taken. The report's tables carry medians and
# ranges, which is what a reader wants and what a chart can just about be drawn from -- but only just:
# two published charts had to be reconstructed from printed tables because phases 2 and 3 kept nothing
# else. A measurement costs an evening and a list costs nothing, so nothing is discarded any more.
RECORDS = []


def record(**kw):
    RECORDS.append(kw)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, argv, port_file=None, port=None, cwd=None):
        self.proc = subprocess.Popen(argv, cwd=str(cwd or SEDIS), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        self.port = port
        while True:
            if port_file is not None and port_file.exists() and port_file.read_text().strip():
                self.port = int(port_file.read_text().strip())
            if self.port is not None and self.answers():
                break
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError("the server did not start: " + " ".join(argv))
            time.sleep(0.1)

    def answers(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1) as s:
                s.sendall(b"*1\r\n$4\r\nPING\r\n")
                return s.recv(16).startswith(b"+PONG")
        except OSError:
            return False

    def call(self, *words):
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as s:
            s.sendall(b"*%d\r\n" % len(words) + b"".join(b"$%d\r\n%s\r\n" % (len(w), w.encode())
                                                            for w in words))
            time.sleep(0.2)
            return s.recv(65536).decode(errors="replace")

    def stop(self):
        self.proc.kill()
        self.proc.wait()


def start(contender, opts, tmp, persist=False, extra=()):
    """Start one contender; persistence gets a fresh data file or directory in tmp.

    A `sedis` spec is ("sedis", shards[, cwd[, extra_argv]]): the two optional slots let a SECOND variant
    of the same server -- a copy in another directory, with its own options -- be a contender of its own.
    The kind string stays "sedis" on purpose, so the variant inherits phase 3's GC report, which is gated
    on exactly that string.
    """
    kind = contender[0]
    port_file = tmp / ("port-%d" % time.monotonic_ns())
    data = tmp / ("data-%d" % time.monotonic_ns())
    if kind == "sedis":
        shards = contender[1]
        cwd = contender[2] if len(contender) > 2 and contender[2] else None
        argv = [opts.vm, "sedis.skn", "--port=0", "--port-file=" + str(port_file),
                "--shards=%d" % shards]
        argv += list(contender[3]) if len(contender) > 3 else []
        argv += list(extra)
        if persist:
            argv += ["--dir=" + str(data), "--appendfsync=everysec"]
        return Server(argv, port_file=port_file, cwd=cwd)
    if kind in ("asyncio", "threads"):
        py = contender[1]
        argv = [py, "python/pysedis.py", "--port=0", "--port-file=" + str(port_file), "--mode=" + kind]
        if persist:
            argv += ["--aof=" + str(data)]
        return Server(argv, port_file=port_file)
    if kind == "memurai":
        port = free_port()
        argv = [opts.memurai, "--port", str(port), "--save", ""]
        if persist:
            data.mkdir()
            argv += ["--appendonly", "yes", "--appendfsync", "everysec", "--dir", str(data)]
        else:
            argv += ["--appendonly", "no"]
        return Server(argv, port=port, cwd=tmp)
    raise ValueError(kind)


def load(opts, port, command, clients, pipeline, seconds=2, keys=10000):
    r = subprocess.run([opts.vm, "loadgen.skn", "--port=%d" % port, "--command=" + command,
                        "--clients=%d" % clients, "--pipeline=%d" % pipeline, "--seconds=%d" % seconds,
                        "--keys=%d" % keys],
                       cwd=str(SEDIS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    out = r.stdout.decode(errors="replace")
    m = LINE.search(out)
    e = ERRORS.search(out)
    if not m or not e or int(e.group(1)) != 0:
        raise RuntimeError("load generator: " + out.strip())
    return tuple(int(x) for x in m.groups())  # ops/s, p50, p99, max


# The worker variant's directory, if it is to be measured. Its own server.skn waits on all of one
# actor's connections at once, so the number of worker actors is a knob: busy threads should be about the
# machine's logical CPUs, and the shards take their share of those.
def worker_spec(opts, shards):
    return ("sedis", shards, opts.worker_dir, ["--workers=%d" % max(1, (os.cpu_count() or 2) - shards)])


def contenders(opts):
    out = [("Sedis, 4 shards", ("sedis", 4))]
    if opts.worker_dir:
        # a DISTINCT display name: phase 1 keys its readings by name, so two equal names would merge
        # their measurements without a word and print the column twice
        out.append(("Sedis workers, 4 shards", worker_spec(opts, 4)))
    out.append(("Python asyncio", ("asyncio", opts.python)))
    out.append(("Python threads", ("threads", opts.python)))
    if opts.python_ft:
        out.append(("Python threads, no GIL", ("threads", opts.python_ft)))
    if opts.memurai:
        out.append(("Memurai (C++)", ("memurai",)))
    return out


def check_sedis_sources(opts):
    """Refuse a run that could not fail and would mean nothing.

    `sedis.skn` does not reject unknown options, so a wrong --worker-dir (or a typo in the flag) would
    start the STOCK server, ignore --workers, and compare two identical configurations without a word of
    complaint -- and Server sends stdout and stderr to DEVNULL, so nothing would show. The cheapest test
    that excludes it: each variant's own source must be the one it claims to be.
    """
    want = [(str(SEDIS), False)]
    if opts.worker_dir:
        want.append((opts.worker_dir, True))
    for cwd, is_worker in want:
        src = Path(cwd) / "server.skn"
        if not src.is_file():
            sys.exit("no server.skn in " + cwd)
        has = "selectIo" in src.read_text(encoding="utf-8")
        if has != is_worker:
            sys.exit("%s: server.skn %s selectIo, but it was taken for the %s variant"
                     % (cwd, "uses" if has else "does not use", "worker" if is_worker else "stock"))
        print("  contender source: %-58s %s" % (cwd, "workers" if is_worker else "one actor per client"),
              flush=True)


def median(xs):
    return int(statistics.median(xs)) if xs else 0


def phase1(opts, tmp, report):
    cs = contenders(opts)
    runs = {}  # (name, command, load) -> [(ops, p50, p99, max)]
    for rnd in range(opts.rounds):
        order = cs[rnd % len(cs):] + cs[:rnd % len(cs)]
        for name, c in order:
            time.sleep(opts.cooldown)  # every contender starts on a machine that has cooled down alike
            srv = start(c, opts, tmp)
            try:
                load(opts, srv.port, "set", 16, 1, seconds=1)  # warm-up
                for command in opts.commands:
                    for clients, pipeline in LOADS:
                        r = load(opts, srv.port, command, clients, pipeline)
                        runs.setdefault((name, command, (clients, pipeline)), []).append(r)
                        record(phase=1, contender=name, command=command, clients=clients,
                               pipeline=pipeline, persist=False, round=rnd + 1,
                               ops=r[0], p50=r[1], p99=r[2], max=r[3])
                        # every figure its table needs, so the console alone can rebuild them
                        print("round %d  %-24s %-6s c=%-2d P=%-2d  %8d ops/s  p50 %5d  p99 %5d  max %6d us"
                              % (rnd + 1, name, command, clients, pipeline, r[0], r[1], r[2], r[3]),
                              flush=True)
            finally:
                srv.stop()
    names = [n for n, _ in cs]
    for clients, pipeline in LOADS:
        title = {(1, 1): "1 client, no pipelining (latency)",
                 (16, 1): "16 clients, no pipelining (concurrency)",
                 (16, 16): "16 clients, pipelines of 16 (throughput)"}[(clients, pipeline)]
        report.append("\n### Phase 1 -- %s: ops/s, median (lowest-highest) of %d rounds\n"
                      % (title, opts.rounds))
        report.append("| command | " + " | ".join(names) + " |")
        report.append("|---|" + "---|" * len(names))
        for command in opts.commands:
            cells = []
            for n in names:
                xs = [r[0] for r in runs[(n, command, (clients, pipeline))]]
                cells.append("%d (%d-%d)" % (median(xs), min(xs), max(xs)))
            report.append("| %s | %s |" % (command, " | ".join(cells)))
        report.append("\np50 / p99 latency of a round trip, us (median of the rounds):\n")
        report.append("| command | " + " | ".join(names) + " |")
        report.append("|---|" + "---|" * len(names))
        for command in opts.commands:
            cells = ["%d / %d" % (median([r[1] for r in runs[(n, command, (clients, pipeline))]]),
                                  median([r[2] for r in runs[(n, command, (clients, pipeline))]]))
                     for n in names]
            report.append("| %s | %s |" % (command, " | ".join(cells)))
    report.append("\n<details><summary>every run</summary>\n")
    for (n, command, ld), rs in sorted(runs.items()):
        report.append("- %s, %s, c=%d P=%d: %s" % (n, command, ld[0], ld[1], rs))
    report.append("\n</details>")


def phase2(opts, tmp, report):
    cs = [c for c in contenders(opts) if c[1][0] != "threads"]  # the threaded server logs too, but
    cs += [c for c in contenders(opts) if c[0] == "Python threads, no GIL"]  # one Python is enough
    runs = {}
    for rnd in range(opts.rounds):
        order = cs[rnd % len(cs):] + cs[:rnd % len(cs)]
        for name, c in order:
            time.sleep(opts.cooldown)
            for persist in (False, True):
                srv = start(c, opts, tmp, persist=persist)
                try:
                    load(opts, srv.port, "set", 16, 1, seconds=1)
                    for command in ("set", "incr"):
                        for clients, pipeline in ((16, 1), (16, 16)):
                            r = load(opts, srv.port, command, clients, pipeline)
                            runs.setdefault((name, persist, command, (clients, pipeline)), []).append(r[0])
                            record(phase=2, contender=name, command=command, clients=clients,
                                   pipeline=pipeline, persist=persist, round=rnd + 1,
                                   ops=r[0], p50=r[1], p99=r[2], max=r[3])
                            print("round %d  %-24s aof=%-5s %-4s c=%d P=%-2d %8d ops/s  "
                                  "p50 %5d  p99 %5d  max %6d us"
                                  % (rnd + 1, name, persist, command, clients, pipeline,
                                     r[0], r[1], r[2], r[3]), flush=True)
                finally:
                    srv.stop()
    report.append("\n### Phase 2 -- an append-only file, fsync every second: ops/s without / with "
                  "(median of %d rounds)\n" % opts.rounds)
    report.append("| contender | set c=16 P=1 | set c=16 P=16 | incr c=16 P=1 | incr c=16 P=16 |")
    report.append("|---|---|---|---|---|")
    for name, _ in cs:
        cells = []
        for command in ("set", "incr"):
            for ld in ((16, 1), (16, 16)):
                off = median(runs[(name, False, command, ld)])
                on = median(runs[(name, True, command, ld)])
                cells.append("%d / %d (%+.0f %%)" % (off, on, 100.0 * (on - off) / off if off else 0))
        report.append("| %s | %s |" % (name, " | ".join(cells)))


def phase3(opts, tmp, report):
    keys = 1000000
    # Built by hand, because this phase wants one and four shards to show sharding dividing the GC pause.
    # The filter below drops EVERY sedis-kind contender, so a second variant has to be listed here too --
    # otherwise it silently does not run in this phase at all.
    cs = [("Sedis, 1 shard", ("sedis", 1)), ("Sedis, 4 shards", ("sedis", 4))]
    if opts.worker_dir:
        cs += [("Sedis workers, 1 shard", worker_spec(opts, 1)),
               ("Sedis workers, 4 shards", worker_spec(opts, 4))]
    cs += [c for c in contenders(opts) if c[1][0] != "sedis"]
    rows = []
    for name, c in cs:
        time.sleep(opts.cooldown)
        srv = start(c, opts, tmp)
        try:
            t0 = time.time()
            subprocess.run([opts.vm, "loadgen.skn", "--port=%d" % srv.port, "--command=fill", "--clients=4",
                            "--pipeline=16", "--keys=%d" % keys], cwd=str(SEDIS), stdout=subprocess.DEVNULL,
                           timeout=600, check=True)
            filled = time.time() - t0
            gc_before = srv.call("SEDIS.GC", "RESET") if c[0] == "sedis" else ""
            r = load(opts, srv.port, "set", 16, 1, seconds=5, keys=keys)
            gc = srv.call("SEDIS.GC") if c[0] == "sedis" else ""
            pauses = re.findall(r"maxPauseUs=(\d+)", gc)
            live = re.findall(r"meanLiveMB=(\d+)", gc)
            rows.append((name, filled, r, max((int(x) for x in pauses), default=-1),
                         max((int(x) for x in live), default=-1)))
            record(phase=3, contender=name, command="set", clients=16, pipeline=1, persist=False,
                   round=1, keys=keys, fill_s=round(filled, 3), ops=r[0], p50=r[1], p99=r[2], max=r[3],
                   gc_pause_us=[int(x) for x in pauses], gc_live_mb=[int(x) for x in live])
            print("%-24s fill %.1f s  %8d ops/s  p50 %d  p99 %d  max %d us  gc max pause %s us"
                  % (name, filled, r[0], r[1], r[2], r[3], pauses), flush=True)
            del gc_before
        finally:
            srv.stop()
    report.append("\n### Phase 3 -- 1,000,000 keys, then SET on random ones: 16 clients, 5 s\n")
    report.append("| contender | load 1M keys | ops/s | p50 us | p99 us | max us | longest GC pause (Sedis) |")
    report.append("|---|---|---|---|---|---|---|")
    for name, filled, r, pause, live in rows:
        gc = "%d us (a shard's mean live data %d MB)" % (pause, live) if pause >= 0 else "-"
        report.append("| %s | %.1f s | %d | %d | %d | %d | %s |" % (name, filled, r[0], r[1], r[2], r[3], gc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vm", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--python-ft")
    ap.add_argument("--memurai")
    ap.add_argument("--worker-dir", help="a second Sedis to measure beside the first: the directory of a "
                                        "copy whose connections are served by worker actors")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cooldown", type=int, default=15, help="seconds of rest before each contender")
    ap.add_argument("--phases", default="123")
    ap.add_argument("--commands", default=",".join(COMMANDS), help="phase 1's commands, comma-separated")
    ap.add_argument("--note", default="", help="free text for the report's head")
    ap.add_argument("--out", default="results.md")
    opts = ap.parse_args()
    opts.vm = str(Path(opts.vm).resolve())
    if opts.worker_dir:
        opts.worker_dir = str(Path(opts.worker_dir).resolve())
    opts.commands = [c for c in opts.commands.split(",") if c]
    unknown = [c for c in opts.commands if c not in COMMANDS]
    if unknown:
        ap.error("unknown command(s): " + ", ".join(unknown))
    check_sedis_sources(opts)
    tmp = Path(tempfile.mkdtemp(prefix="sedis_bench_"))
    report = ["# Sedis measurement, %s\n" % time.strftime("%Y-%m-%d %H:%M"),
              "Contenders: " + ", ".join(n for n, _ in contenders(opts)) + ". Load generator: loadgen.skn, "
              "3-byte values, 10,000 keys (phases 1-2). Machine: %d logical CPUs. Rest before each "
              "contender: %d s." % (os.cpu_count(), opts.cooldown)]
    if opts.note:
        report.append("\n" + opts.note)
    # Written after EVERY phase, not once at the end: a run that is interrupted -- by the machine, not by
    # itself -- then still leaves behind the phases it finished.
    started = time.strftime("%Y-%m-%d %H:%M")

    def flush():
        Path(opts.out).write_text("\n".join(report) + "\n", encoding="utf-8")
        # The same run, machine-readable: the report is for a reader, this is for charts.py and for
        # whatever question gets asked of these numbers later.
        meta = {"date": started, "cpus": os.cpu_count(), "rounds": opts.rounds,
                "cooldown": opts.cooldown, "phases": opts.phases, "commands": opts.commands,
                "contenders": [n for n, _ in contenders(opts)], "vm": opts.vm, "note": opts.note}
        Path(opts.out).with_suffix(".json").write_text(
            json.dumps({"meta": meta, "records": RECORDS}, indent=1), encoding="utf-8")

    if "1" in opts.phases:
        phase1(opts, tmp, report)
        flush()
    if "2" in opts.phases:
        phase2(opts, tmp, report)
        flush()
    if "3" in opts.phases:
        phase3(opts, tmp, report)
        flush()
    flush()
    print()
    print("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
