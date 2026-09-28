"""pysedis -- the Python counterpart of Sedis, for the measurement in ../bench.py.

The same protocol (RESP2, the commands the benchmark sends plus what clients ask for at start) and the
same optional append-only file, written as a Python programmer would write it -- in two designs:

  --mode=asyncio   one thread, an asyncio Protocol per connection: the design of Redis itself, and the
                   idiomatic way to write a network server in Python.
  --mode=threads   a thread per connection over one shared dict, with 16 striped locks so that a
                   read-modify-write (INCR, LPUSH) cannot interleave. On a free-threaded Python
                   (python3.14t) the threads run in parallel; on the GIL build they take turns.

Run:  python pysedis.py [--port=P] [--port-file=F] [--mode=asyncio|threads] [--aof=FILE]
      --aof=FILE  append every change to FILE and fsync it once a second (appendfsync everysec)

Only the standard library. Written to be fast within that: the parser works on one bytearray with
bytes.find, the answers of one read are joined into one write.
"""

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from collections import deque

WRONGTYPE = b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
NOTINT = b"-ERR value is not an integer or out of range\r\n"
OK = b"+OK\r\n"
NULL = b"$-1\r\n"


def bulk(v):
    return b"$%d\r\n%s\r\n" % (len(v), v)


def integer(n):
    return b":%d\r\n" % n


class ProtocolError(Exception):
    pass


def parse(buf, pos):
    """The next command in buf from pos: (words, new pos), or (None, pos) if it has not all arrived."""
    n = len(buf)
    if pos >= n:
        return None, pos
    if buf[pos] != 42:  # '*': otherwise an inline command
        end = buf.find(b"\n", pos)
        if end < 0:
            return None, pos
        return bytes(buf[pos:end]).split(), end + 1
    end = buf.find(b"\r\n", pos)
    if end < 0:
        return None, pos
    count = int(buf[pos + 1:end])
    p = end + 2
    words = []
    for _ in range(count):
        if p >= n:
            return None, pos
        if buf[p] != 36:
            raise ProtocolError("expected '$'")
        end = buf.find(b"\r\n", p)
        if end < 0:
            return None, pos
        size = int(buf[p + 1:end])
        start = end + 2
        if start + size + 2 > n:
            return None, pos
        words.append(bytes(buf[start:start + size]))
        p = start + size + 2
    return words, p


def encode_command(words):
    out = [b"*%d\r\n" % len(words)]
    for w in words:
        out.append(b"$%d\r\n%s\r\n" % (len(w), w))
    return b"".join(out)


def to_int(b):
    try:
        v = int(b)
    except ValueError:
        return None
    if b != str(v).encode():  # no '+', no leading zero, no spaces -- as Redis
        return None
    return v


class Store:
    """The data and the commands. `lock_for(key)` gives the lock a threaded server takes."""

    WRITES = {b"SET", b"INCR", b"LPUSH", b"RPUSH", b"LPOP", b"HSET", b"MSET", b"DEL", b"FLUSHDB"}

    def __init__(self, threaded, aof_path):
        self.data = {}
        self.locks = [threading.Lock() for _ in range(16)] if threaded else None
        self.aof = open(aof_path, "ab") if aof_path else None
        self.aof_lock = threading.Lock()
        self.dirty = False

    def lock_for(self, key):
        return self.locks[hash(key) & 15]

    def run(self, words):
        """The answer to one command, as bytes."""
        name = words[0].upper()
        # MSET takes each key's lock itself; every other write holds its key's lock throughout
        if name in self.WRITES and name != b"MSET" and self.locks is not None and len(words) > 1:
            with self.lock_for(words[1]):
                return self._run(name, words)
        return self._run(name, words)

    def _run(self, name, w):
        d = self.data
        if name == b"GET":
            v = d.get(w[1])
            if v is None:
                return NULL
            return bulk(v) if isinstance(v, bytes) else WRONGTYPE
        if name == b"SET":
            d[w[1]] = w[2]
            self.log(w)
            return OK
        if name == b"INCR":
            v = d.get(w[1], b"0")
            if not isinstance(v, bytes):
                return WRONGTYPE
            n = to_int(v)
            if n is None:
                return NOTINT
            n += 1
            d[w[1]] = str(n).encode()
            self.log(w)
            return integer(n)
        if name in (b"LPUSH", b"RPUSH"):
            lst = d.get(w[1])
            if lst is None:
                lst = d[w[1]] = deque()
            elif not isinstance(lst, deque):
                return WRONGTYPE
            if name == b"LPUSH":
                lst.extendleft(w[2:])
            else:
                lst.extend(w[2:])
            self.log(w)
            return integer(len(lst))
        if name == b"LPOP":
            lst = d.get(w[1])
            if lst is None:
                return NULL
            if not isinstance(lst, deque):
                return WRONGTYPE
            v = lst.popleft()
            if not lst:
                del d[w[1]]
            self.log(w)
            return bulk(v)
        if name == b"HSET":
            h = d.get(w[1])
            if h is None:
                h = d[w[1]] = {}
            elif not isinstance(h, dict):
                return WRONGTYPE
            added = 0
            for i in range(2, len(w) - 1, 2):
                if w[i] not in h:
                    added += 1
                h[w[i]] = w[i + 1]
            self.log(w)
            return integer(added)
        if name == b"MSET":
            # a threaded server takes each key's lock in turn, as Sedis sets each shard's keys
            for i in range(1, len(w) - 1, 2):
                if self.locks is not None:
                    with self.lock_for(w[i]):
                        d[w[i]] = w[i + 1]
                else:
                    d[w[i]] = w[i + 1]
            self.log(w)
            return OK
        if name == b"MGET":
            out = [b"*%d\r\n" % (len(w) - 1)]
            for k in w[1:]:
                v = d.get(k)
                out.append(bulk(v) if isinstance(v, bytes) else NULL)
            return b"".join(out)
        if name == b"DEL":
            n = 0
            for k in w[1:]:
                if d.pop(k, None) is not None:
                    n += 1
            self.log(w)
            return integer(n)
        if name == b"DBSIZE":
            return integer(len(d))
        if name == b"FLUSHDB":
            d.clear()
            self.log(w)
            return OK
        if name == b"PING":
            return b"+PONG\r\n" if len(w) == 1 else bulk(w[1])
        if name == b"ECHO":
            return bulk(w[1])
        if name == b"COMMAND":
            return b"*0\r\n"
        if name == b"CONFIG":
            key = w[2].lower() if len(w) > 2 else b""
            if key == b"save":
                return b"*2\r\n" + bulk(b"save") + bulk(b"")
            if key == b"appendonly":
                return b"*2\r\n" + bulk(b"appendonly") + bulk(b"yes" if self.aof else b"no")
            return b"*0\r\n"
        return b"-ERR unknown command '%s'\r\n" % w[0]

    def log(self, words):
        if self.aof is not None:
            with self.aof_lock:
                self.aof.write(encode_command(words))
                self.dirty = True

    def flush(self):
        """Hand what was logged to the operating system (after each batch, as Sedis does)."""
        if self.aof is not None and self.dirty:
            with self.aof_lock:
                self.aof.flush()

    def sync_loop(self):
        """appendfsync everysec."""
        while True:
            time.sleep(1)
            with self.aof_lock:
                if self.dirty:
                    self.aof.flush()
                    os.fsync(self.aof.fileno())
                    self.dirty = False


def answer_all(store, buf):
    """Run every complete command in buf; answers joined, and the bytes consumed."""
    out = []
    pos = 0
    while True:
        words, pos2 = parse(buf, pos)
        if words is None:
            break
        pos = pos2
        if words:
            out.append(store.run(words))
    store.flush()
    return b"".join(out), pos


# ---- asyncio ----

class Connection(asyncio.Protocol):
    def __init__(self, store):
        self.store = store
        self.buf = bytearray()

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def data_received(self, data):
        self.buf += data
        try:
            answers, used = answer_all(self.store, self.buf)
        except ProtocolError as e:
            self.transport.write(b"-ERR protocol error: %s\r\n" % str(e).encode())
            self.transport.close()
            return
        del self.buf[:used]
        if answers:
            self.transport.write(answers)


async def serve_asyncio(store, port, port_file):
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: Connection(store), "127.0.0.1", port)
    bound = server.sockets[0].getsockname()[1]
    announce(bound, port_file, "asyncio")
    async with server:
        await server.serve_forever()


# ---- threads ----

def serve_threads(store, port, port_file):
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lst.bind(("127.0.0.1", port))
    lst.listen(128)
    announce(lst.getsockname()[1], port_file, "threads")
    while True:
        conn, _ = lst.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=client_thread, args=(store, conn), daemon=True).start()


def client_thread(store, conn):
    buf = bytearray()
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                return
            buf += data
            answers, used = answer_all(store, buf)
            del buf[:used]
            if answers:
                conn.sendall(answers)
    except (ProtocolError, OSError):
        pass
    finally:
        conn.close()


def announce(port, port_file, mode):
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print("pysedis listening on port %d (%s, Python %s, GIL %s)" % (port, mode, sys.version.split()[0],
                                                                    "on" if gil else "off"), flush=True)
    if port_file:
        with open(port_file, "w") as f:
            f.write(str(port))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6380)
    ap.add_argument("--port-file")
    ap.add_argument("--mode", choices=["asyncio", "threads"], default="asyncio")
    ap.add_argument("--aof")
    o = ap.parse_args()
    store = Store(o.mode == "threads", o.aof)
    if store.aof is not None:
        threading.Thread(target=store.sync_loop, daemon=True).start()
    if o.mode == "asyncio":
        asyncio.run(serve_asyncio(store, o.port, o.port_file))
    else:
        serve_threads(store, o.port, o.port_file)


if __name__ == "__main__":
    main()
