# run_tests.py -- the tests of Sedis.
#
#   1. every entry program type-checks (`skarnvm --dump-ast`);
#   2. the self-test (`sedis.skn --selftest`) runs and ends with "selftest: N steps ok"; a start that
#      fails ends with exit code 1 and its reason on standard error, without an error trace;
#   3. a real server process on a free port answers a RESP client written here in Python -- a second
#      implementation of the protocol, independent of std::resp: exact bytes for every answer,
#      binary values, pipelining, a command split into single bytes, inline commands, the error
#      texts, two connections at once, QUIT, and a protocol error closing the connection;
#   4. persistence, with processes killed hard: fsync always and everysec, BGREWRITEAOF, a rewrite
#      past --rewrite-min, a command cut off at the end of a log, a corrupt log, and data written
#      by another number of shards;
#   5. crash recovery: DEBUG CRASH kills a shard, its supervisor starts it again at the same address
#      with its data replayed, the lost commands of a pipeline say so, and too many crashes end the
#      server.
#
# Usage (from anywhere; Python 3.9+, standard library only):
#   python Sedis/tests/run_tests.py
#   python Sedis/tests/run_tests.py --exe path/to/skarnvm
#
# The driver: --exe, else $SKARNVM, else the first that exists of x64/Release, x64/Debug, build,
# build/Release and build/Debug in the directory above Sedis/ (skarnvm or skarnvm.exe).
#
# Exit code: 0 = all passed, 1 = a failure, 2 = no driver found.

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SEDIS = Path(__file__).resolve().parent.parent
IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")


def find_driver(explicit):
    if explicit:
        return Path(explicit)
    if os.environ.get("SKARNVM"):
        return Path(os.environ["SKARNVM"])
    base = SEDIS.parent
    for d in ("x64/Release", "x64/Debug", "build", "build/Release", "build/Debug"):
        for name in ("skarnvm.exe", "skarnvm"):
            p = base / d / name
            if p.is_file():
                return p
    return None


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print("  PASS  " + name)
        else:
            self.failed.append(name)
            print("  FAIL  " + name + ("  -- " + detail if detail else ""))


def entry_programs():
    files = sorted(SEDIS.glob("*.skn"))
    imported = set()
    for f in files:
        for ln in f.read_text(encoding="utf-8").splitlines():
            m = IMPORT.match(ln)
            if m:
                imported.add(m.group(1) + ".skn")
    return [f for f in files if f.name not in imported]


# ---- a RESP client, independent of std::resp ----

class Resp:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def send(self, data):
        self.sock.sendall(data)

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("the server closed the connection")
        self.buf += chunk

    def _line(self):
        while b"\r\n" not in self.buf:
            self._fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def raw_reply(self):
        """One complete reply, as the exact bytes the server sent."""
        out = bytearray()

        def take_line():
            line = self._line()
            out.extend(line + b"\r\n")
            return line

        def one():
            line = take_line()
            t, rest = line[:1], line[1:]
            if t in (b"+", b"-", b":"):
                return
            n = int(rest)
            if t == b"$":
                if n < 0:
                    return
                while len(self.buf) < n + 2:
                    self._fill()
                out.extend(self.buf[:n + 2])
                self.buf = self.buf[n + 2:]
            elif t == b"*":
                for _ in range(max(n, 0)):
                    one()
            else:
                raise ValueError("unknown type byte in " + repr(line))

        one()
        return bytes(out)


def command(*words):
    out = b"*%d\r\n" % len(words)
    for w in words:
        if isinstance(w, str):
            w = w.encode()
        out += b"$%d\r\n%s\r\n" % (len(w), w)
    return out


def bulk(v):
    if isinstance(v, str):
        v = v.encode()
    return b"$%d\r\n%s\r\n" % (len(v), v)


def parse_array(raw):
    """The bulk strings of an array reply, as text."""
    out = []
    lines = raw.split(b"\r\n")
    i = 1
    while i < len(lines) - 1:
        if lines[i].startswith(b"$"):
            out.append(lines[i + 1].decode())
            i += 2
        else:
            i += 1
    return out


def connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Resp(s)


def live_server(exe, res):
    """Start a server process and talk to it."""
    tmp = Path(tempfile.mkdtemp(prefix="sedis_test_"))
    port_file = tmp / "port"
    proc = subprocess.Popen([str(exe), "sedis.skn", "--port=0", "--port-file=" + str(port_file), "--shards=3"],
                            cwd=str(SEDIS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 15
        while not port_file.exists() or not port_file.read_text().strip():
            if proc.poll() is not None or time.time() > deadline:
                res.check("server starts", False, (proc.stdout.read() or b"").decode(errors="replace"))
                return
            time.sleep(0.05)
        port = int(port_file.read_text().strip())
        res.check("server starts", True)

        c = connect(port)

        def ask(name, data, want):
            c.send(data)
            got = c.raw_reply()
            res.check(name, got == want, "expected {!r}, got {!r}".format(want, got))

        ask("PING", command("PING"), b"+PONG\r\n")
        value = b"a\r\nb\x00c\xff" * 3
        ask("SET a binary value", command("SET", "bin", value), b"+OK\r\n")
        ask("GET it back byte for byte", command("GET", "bin"), bulk(value))
        ask("GET a missing key", command("GET", "nope"), b"$-1\r\n")
        ask("INCRBY", command("INCRBY", "n", "5"), b":5\r\n")
        ask("MSET over the shards", command("MSET", "a", "1", "b", "2", "c", "3", "d", "4"), b"+OK\r\n")
        ask("MGET keeps the order", command("MGET", "d", "x", "a", "c"),
            b"*4\r\n" + bulk("4") + b"$-1\r\n" + bulk("1") + bulk("3"))
        ask("DEL counts over the shards", command("DEL", "a", "b", "x", "c"), b":3\r\n")
        ask("unknown command", command("NOSUCH", "p"),
            b"-ERR unknown command 'NOSUCH', with args beginning with: 'p' \r\n")
        ask("wrong arity", command("GET", "a", "b"), b"-ERR wrong number of arguments for 'get' command\r\n")
        ask("inline PING", b"PING\r\n", b"+PONG\r\n")
        ask("inline ECHO ended by \\n alone", b"ECHO  hi\n", bulk("hi"))

        # A command split into single bytes.
        data = command("SET", "split", "value")
        for i in range(len(data)):
            c.send(data[i:i + 1])
        got = c.raw_reply()
        res.check("a command sent one byte at a time", got == b"+OK\r\n", repr(got))

        # Pipelining: 200 commands in one write; the answers in order.
        batch = b""
        want = b""
        for i in range(100):
            batch += command("SET", "k%d" % i, "v%d" % i) + command("GET", "k%d" % i)
            want += b"+OK\r\n" + bulk("v%d" % i)
        c.send(batch)
        got = b"".join(c.raw_reply() for _ in range(200))
        res.check("200 pipelined commands, answered in order", got == want)

        # A second connection sees the first one's writes, while the first stays open.
        d = connect(port)
        d.send(command("GET", "k42"))
        got = d.raw_reply()
        res.check("a second connection", got == bulk("v42"), repr(got))
        c.send(command("DBSIZE"))
        got = c.raw_reply()
        res.check("DBSIZE over the shards", got == b":104\r\n", repr(got))

        # QUIT: OK, then the server closes.
        d.send(command("QUIT"))
        got = d.raw_reply()
        closed = False
        try:
            d.raw_reply()
        except (EOFError, OSError):
            closed = True
        res.check("QUIT answers OK and closes", got == b"+OK\r\n" and closed, repr(got))

        # A protocol error is answered, and the connection is closed.
        e = connect(port)
        e.send(b"*1\r\n:1\r\n")
        got = e.raw_reply()
        closed = False
        try:
            e.raw_reply()
        except (EOFError, OSError):
            closed = True
        res.check("a protocol error is answered and closes the connection",
                  got.startswith(b"-ERR protocol error") and closed, repr(got))

        # Types, expiry, and what clients send when they start -- exact bytes.
        wrongtype = b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
        ask("HSET", command("HSET", "h", "f", "v"), b":1\r\n")
        ask("HGETALL", command("HGETALL", "h"), b"*2\r\n" + bulk("f") + bulk("v"))
        ask("GET on a hash is WRONGTYPE", command("GET", "h"), wrongtype)
        ask("RPUSH", command("RPUSH", "l", "a", "b"), b":2\r\n")
        ask("LRANGE", command("LRANGE", "l", "0", "-1"), b"*2\r\n" + bulk("a") + bulk("b"))
        ask("LPOP with a count on a missing key is the null array", command("LPOP", "nol", "2"), b"*-1\r\n")
        ask("TYPE", command("TYPE", "l"), b"+list\r\n")
        ask("SET with PX", command("SET", "brief", "v", "PX", "100"), b"+OK\r\n")
        c.send(command("PTTL", "brief"))
        got = c.raw_reply()
        ms = int(got[1:-2]) if got.startswith(b":") else -9
        res.check("PTTL counts down from 100", 0 < ms <= 100, repr(got))
        time.sleep(0.3)
        ask("the key is gone after its time", command("GET", "brief"), b"$-1\r\n")
        ask("COMMAND DOCS (redis-cli asks at start)", command("COMMAND", "DOCS"), b"*0\r\n")
        ask("CONFIG GET save (redis-benchmark asks at start)", command("CONFIG", "GET", "save"),
            b"*2\r\n" + bulk("save") + bulk(""))
        ask("CLIENT SETINFO (redis-py sends it)", command("CLIENT", "SETINFO", "lib-name", "x"), b"+OK\r\n")

        # KEYS with Redis' glob patterns (the examples of the KEYS documentation, and more).
        names = ["hello", "hallo", "hxllo", "hllo", "heeeello", "hbllo", "h*llo", "hat", "cat"]
        c.send(b"".join(command("SET", n, "1") for n in names))
        for _ in names:
            c.raw_reply()
        patterns = [
            ("h?llo", ["hallo", "hbllo", "hello", "hxllo", "h*llo"]),
            ("h*llo", ["hallo", "hbllo", "heeeello", "hello", "hllo", "hxllo", "h*llo"]),
            ("h[ae]llo", ["hallo", "hello"]),
            ("h[^e]llo", ["hallo", "hbllo", "hxllo", "h*llo"]),
            ("h[a-b]llo", ["hallo", "hbllo"]),
            ("h\\*llo", ["h*llo"]),
            ("?at", ["cat", "hat"]),
            ("*", None),
        ]
        for pat, want in patterns:
            c.send(command("KEYS", pat))
            got = parse_array(c.raw_reply())
            if want is None:
                ok = set(names) <= set(got)
            else:
                ok = sorted(got) == sorted(want)
            res.check("KEYS " + pat, ok, "got {}".format(sorted(got)))

        # The server is still there for the others.
        ask("the first connection still works", command("PING"), b"+PONG\r\n")
    except Exception as ex:  # a hang or a broken connection is a failure, not a crash of the runner
        res.check("live server", False, "{}: {}".format(type(ex).__name__, ex))
    finally:
        proc.kill()
        proc.wait()
        try:
            port_file.unlink()
            tmp.rmdir()
        except OSError:
            pass


# ---- persistence: real processes, killed hard ----

class Server:
    """A sedis process on a free port, with the given options."""

    def __init__(self, exe, *options):
        self.tmp = Path(tempfile.mkdtemp(prefix="sedis_port_"))
        self.port_file = self.tmp / "port"
        self.log_file = self.tmp / "sedis.log"
        self.proc = subprocess.Popen([str(exe), "sedis.skn", "--port=0", "--port-file=" + str(self.port_file),
                                      "--logfile=" + str(self.log_file)] + list(options),
                                     cwd=str(SEDIS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # What the server prints (standard output and error, merged), read as it arrives: the driver
        # writes it line by line, so it is here while the server still runs -- and a killed server
        # loses none of it.
        self.printed = bytearray()
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.port = None
        deadline = time.time() + 30
        while self.port is None:
            if self.port_file.exists() and self.port_file.read_text().strip():
                self.port = int(self.port_file.read_text().strip())
            elif self.proc.poll() is not None or time.time() > deadline:
                break
            else:
                time.sleep(0.05)

    def _read(self):
        while True:
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                return
            with self.lock:
                self.printed.extend(chunk)

    def started(self):
        return self.port is not None

    def printed_so_far(self):
        with self.lock:
            return bytes(self.printed).decode(errors="replace")

    def prints_while_running(self, text, wait_s=5.0):
        """True once `text` has been printed while the process is still running."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if text in self.printed_so_far():
                return self.proc.poll() is None
            if self.proc.poll() is not None:
                return False
            time.sleep(0.02)
        return False

    def output(self):
        """What the process wrote to its log file and printed; ends it first."""
        log = self.log_file.read_text(errors="replace") if self.log_file.exists() else ""
        self.kill()
        return log + self.printed_so_far()

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.reader.join(timeout=5)
        for f in (self.port_file, self.log_file):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass


def call(conn, *words):
    conn.send(command(*words))
    return conn.raw_reply()


def persistence(exe, res):
    data = Path(tempfile.mkdtemp(prefix="sedis_data_"))
    try:
        # 1. fsync always, then the process is killed: everything answered is back.
        srv = Server(exe, "--dir=" + str(data), "--appendfsync=always", "--shards=3")
        res.check("a server with --dir starts", srv.started(), srv.output() if not srv.started() else "")
        if not srv.started():
            return
        c = connect(srv.port)
        future = int(time.time() * 1000) + 3600 * 1000
        setup = [
            (("SET", "s", "v1"), b"+OK\r\n"),
            (("SET", "s", "v2"), b"+OK\r\n"),
            (("INCRBY", "n", "7"), b":7\r\n"),
            (("APPEND", "s", "!"), b":3\r\n"),
            (("HSET", "h", "a", "1", "b", "2"), b":2\r\n"),
            (("HDEL", "h", "a"), b":1\r\n"),
            (("RPUSH", "l", "x", "y", "z"), b":3\r\n"),
            (("LPOP", "l"), bulk("x")),
            (("SET", "ttl", "t", "EX", "3600"), b"+OK\r\n"),
            (("SET", "at", "t"), b"+OK\r\n"),
            (("PEXPIREAT", "at", str(future)), b":1\r\n"),
            (("SET", "brief", "b", "PX", "200"), b"+OK\r\n"),
            (("MSET", "m1", "1", "m2", "2"), b"+OK\r\n"),
            (("DEL", "m2"), b":1\r\n"),
            (("SET", "gone", "g"), b"+OK\r\n"),
            (("EXPIRE", "gone", "-1"), b":1\r\n"),
        ]
        ok = True
        for words, want in setup:
            got = call(c, *words)
            if got != want:
                ok = False
                res.check("setting up: " + " ".join(words), False, repr(got))
        res.check("the writes are answered", ok)
        srv.kill()
        time.sleep(0.3)  # "brief" ends meanwhile

        srv = Server(exe, "--dir=" + str(data), "--appendfsync=always", "--shards=3")
        out = ""
        if not srv.started():
            res.check("the server restarts on its data", False, srv.output())
            return
        c = connect(srv.port)
        checks = [
            (("GET", "s"), bulk("v2!")),
            (("GET", "n"), bulk("7")),
            (("HGETALL", "h"), b"*2\r\n" + bulk("b") + bulk("2")),
            (("LRANGE", "l", "0", "-1"), b"*2\r\n" + bulk("y") + bulk("z")),
            (("GET", "m1"), bulk("1")),
            (("EXISTS", "m2", "gone", "brief"), b":0\r\n"),
            (("PTTL", "at"), None),
            (("TTL", "ttl"), None),
        ]
        for words, want in checks:
            got = call(c, *words)
            if want is None:
                n = int(got[1:-2]) if got.startswith(b":") else -9
                limit = 3600 * 1000 if words[0] == "PTTL" else 3600
                res.check("after a kill: " + " ".join(words) + " keeps its end", 0 < n <= limit, repr(got))
            else:
                res.check("after a kill: " + " ".join(words), got == want, repr(got))
        res.check("the replay report reaches standard output while the server runs",
                  srv.prints_while_running("commands replayed"), srv.printed_so_far().strip())
        out = srv.output()
        res.check("the restart reports what it replayed", "commands replayed" in out, out.strip())

        # 2. BGREWRITEAOF: many changes to one key, then a rewrite; the files shrink, the data stays.
        srv = Server(exe, "--dir=" + str(data), "--shards=3")
        c = connect(srv.port)
        batch = b"".join(command("INCR", "counter") for _ in range(2000))
        c.send(batch)
        for _ in range(2000):
            c.raw_reply()
        before = sum(f.stat().st_size for f in data.glob("*.aof"))
        got = call(c, "BGREWRITEAOF")
        after = sum(f.stat().st_size for f in data.glob("*.aof"))
        res.check("BGREWRITEAOF answers OK", got == b"+OK\r\n", repr(got))
        res.check("the logs shrink ({} -> {} bytes)".format(before, after), after < before / 10, "")
        res.check("no temporary file is left", not list(data.glob("*.tmp")))
        srv.kill()
        srv = Server(exe, "--dir=" + str(data), "--shards=3")
        c = connect(srv.port)
        got = call(c, "MGET", "counter", "s", "n")
        res.check("after the rewrite and a kill, the data is the same",
                  got == b"*3\r\n" + bulk("2000") + bulk("v2!") + bulk("7"), repr(got))
        srv.kill()

        # 3. The log grows past --rewrite-min: it is rewritten on its own.
        srv = Server(exe, "--dir=" + str(data), "--shards=3", "--rewrite-min=20000")
        c = connect(srv.port)
        c.send(b"".join(command("SET", "big", "v%d" % i) for i in range(3000)))
        for _ in range(3000):
            c.raw_reply()
        total = sum(f.stat().st_size for f in data.glob("*.aof"))
        res.check("a log past --rewrite-min is rewritten on its own ({} bytes)".format(total), total < 20000 * 3,
                  "")
        srv.kill()

        # 4. A command cut off at the end of a log: the server starts, keeps the rest, and repairs the file.
        victim = data / "shard-0.aof"
        whole = victim.read_bytes()
        victim.write_bytes(whole + b"*3\r\n$3\r\nSET\r\n$4\r\ncut")
        srv = Server(exe, "--dir=" + str(data), "--shards=3")
        if srv.started():
            c = connect(srv.port)
            got = call(c, "GET", "counter")
            out = srv.output()
            res.check("a cut-off last command: the server starts and keeps the data",
                      got == bulk("2000") and "incomplete last command" in out, out.strip())
            res.check("... and the log is whole again", victim.read_bytes().endswith(b"\r\n")
                      and b"cut" not in victim.read_bytes())
        else:
            res.check("a cut-off last command: the server starts", False, srv.output())

        # 5. Garbage in the middle of a log: the server refuses to start rather than lose data silently.
        victim.write_bytes(b"*1\r\n$4\r\nNOPE\r\n" + victim.read_bytes())
        srv = Server(exe, "--dir=" + str(data), "--shards=3")
        out = srv.output()
        res.check("a corrupt log stops the start, naming the file",
                  not srv.started() and "shard-0.aof" in out, out.strip())
        victim.write_bytes(whole)

        # 6. The data was written by 3 shards: another number is refused.
        srv = Server(exe, "--dir=" + str(data), "--shards=5")
        out = srv.output()
        res.check("another --shards on the same data is refused",
                  not srv.started() and "shards=3" in out, out.strip())

        # 7. everysec: what was written a second and more before the kill is kept.
        srv = Server(exe, "--dir=" + str(data), "--shards=3", "--appendfsync=everysec")
        c = connect(srv.port)
        call(c, "SET", "slow", "kept")
        got = call(c, "CONFIG", "GET", "appendonly")
        res.check("CONFIG GET appendonly says yes", got == b"*2\r\n" + bulk("appendonly") + bulk("yes"), repr(got))
        time.sleep(1.5)
        srv.kill()
        srv = Server(exe, "--dir=" + str(data), "--shards=3")
        c = connect(srv.port)
        got = call(c, "GET", "slow")
        res.check("everysec: a write a second before the kill is kept", got == bulk("kept"), repr(got))
        srv.kill()
    except Exception as ex:
        res.check("persistence", False, "{}: {}".format(type(ex).__name__, ex))
    finally:
        for f in data.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            data.rmdir()
        except OSError:
            pass


# ---- let it crash: a shard dies, its supervisor starts it again ----

def shard_of(key, shards):
    """The shard of a key, as Sedis computes it (FNV-1a, 32 bits)."""
    h = 2166136261
    for c in key.encode():
        h = ((h ^ c) * 16777619) & 0xFFFFFFFF
    return h % shards


def crash_recovery(exe, res):
    data = Path(tempfile.mkdtemp(prefix="sedis_crash_"))
    try:
        srv = Server(exe, "--dir=" + str(data), "--shards=3", "--enable-debug")
        if not srv.started():
            res.check("a server with --enable-debug starts", False, srv.output())
            return
        c = connect(srv.port)
        doomed = shard_of("victim", 3)
        other = next("k%d" % i for i in range(100) if shard_of("k%d" % i, 3) != doomed)
        same = next("s%d" % i for i in range(100) if shard_of("s%d" % i, 3) == doomed)
        call(c, "SET", "victim", "kept")
        call(c, "SET", other, "untouched")
        # One pipeline across the crash: what went to the crashing shard in the same batch is lost
        # and says so; what went to the others is answered; the order holds.
        c.send(command("SET", same, "maybe") + command("DEBUG", "CRASH", "victim") + command("GET", other))
        t0 = time.time()
        got = [c.raw_reply() for _ in range(3)]
        took = time.time() - t0
        lost = b"-ERR the shard restarted before it answered; the command may or may not have run\r\n"
        res.check("a pipeline across a crash: the lost commands say so, the rest is answered, in order",
                  got == [lost, lost, bulk("untouched")], repr(got))
        res.check("the loss is noticed within a few seconds ({:.1f} s)".format(took), took < 5)
        got = call(c, "GET", "victim")
        res.check("the restarted shard has its data back from its log", got == bulk("kept"), repr(got))
        got = call(c, "SET", "victim", "again")
        res.check("... and serves again", got == b"+OK\r\n", repr(got))
        d = connect(srv.port)
        got = call(d, "GET", "victim")
        res.check("a new connection reaches it at the same address", got == bulk("again"), repr(got))
        time.sleep(0.2)
        log = srv.log_file.read_text(errors="replace") if srv.log_file.exists() else ""
        res.check("the restart is reported", "shard {} restarted".format(doomed) in log, log.strip())
        res.check("... on standard output too, while the server runs",
                  srv.prints_while_running("shard {} restarted".format(doomed)), srv.printed_so_far().strip())

        # More than 5 crashes within 10 s: the supervisor gives up, and the server ends with it.
        for _ in range(6):
            try:
                call(c, "DEBUG", "CRASH", "victim")
            except (EOFError, OSError):
                break
        deadline = time.time() + 15
        while srv.proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        ended = srv.proc.poll() is not None
        log = srv.log_file.read_text(errors="replace") if srv.log_file.exists() else ""
        res.check("too many crashes: the supervisor gives up and the server ends",
                  ended and "gave up" in log, log.strip()[-300:])
        srv.reader.join(timeout=5)
        res.check("... and says so on standard error", "gave up" in srv.printed_so_far(),
                  srv.printed_so_far().strip()[-300:])
        srv.kill()

        # Without --enable-debug, DEBUG is refused.
        srv = Server(exe, "--shards=2")
        c = connect(srv.port)
        got = call(c, "DEBUG", "CRASH", "x")
        res.check("DEBUG is off by default", got.startswith(b"-ERR DEBUG is off"), repr(got))
        srv.kill()
    except Exception as ex:
        res.check("crash recovery", False, "{}: {}".format(type(ex).__name__, ex))
    finally:
        remove_tree(data)


def remove_tree(d):
    for f in d.iterdir():
        try:
            f.unlink()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Run the tests of Sedis.")
    ap.add_argument("--exe", help="the skarnvm driver")
    opts = ap.parse_args()
    exe = find_driver(opts.exe)
    if exe is None or not exe.is_file():
        print("run_tests: no skarnvm driver found (use --exe or $SKARNVM)")
        return 2
    print("driver: " + str(exe))
    res = Results()

    print("[type-check]")
    for f in entry_programs():
        r = subprocess.run([str(exe), "--dump-ast", f.name], cwd=str(SEDIS), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        err = r.stderr.decode(errors="replace")
        res.check(f.name, r.returncode == 0 and "error:" not in err, err.strip())

    print("[self-test]")
    data = Path(tempfile.mkdtemp(prefix="sedis_selftest_"))
    for label, extra in (("in memory", []), ("with --dir", ["--dir=" + str(data)])):
        try:
            r = subprocess.run([str(exe), "sedis.skn", "--selftest"] + extra, cwd=str(SEDIS),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        except subprocess.TimeoutExpired:
            res.check("sedis.skn --selftest, " + label, False, "no end within 60 s (a lost answer?)")
            continue
        lines = r.stdout.decode(errors="replace").strip().splitlines()
        last = lines[-1] if lines else ""
        res.check("sedis.skn --selftest, " + label,
                  r.returncode == 0 and re.match(r"^selftest: \d+ steps ok$", last) is not None, last)
    remove_tree(data)

    print("[failed start]")
    r = subprocess.run([str(exe), "sedis.skn", "--shards=0"], cwd=str(SEDIS), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=60)
    err = r.stderr.decode(errors="replace")
    res.check("a failed start: exit code 1, the reason on standard error, no error trace",
              r.returncode == 1 and err.strip() == "sedis: --shards must be 1 or more" and not r.stdout,
              "exit {}: {}".format(r.returncode, err.strip()))

    print("[live server]")
    live_server(exe, res)

    print("[persistence]")
    persistence(exe, res)

    print("[crash recovery]")
    crash_recovery(exe, res)

    print()
    print("==== sedis: {} passed, {} failed ====".format(res.passed, len(res.failed)))
    return 0 if not res.failed else 1


if __name__ == "__main__":
    sys.exit(main())
