"""charts.py -- the benchmark's pictures, drawn from the benchmark's numbers.

The first set of charts was written by hand, with every bar's coordinates worked out one at a time. That
is affordable exactly once: the second measurement round made every one of them wrong, and there are now
three places that show the same charts (the wiki page, BENCHMARK.md and the README). This draws them.

It reads a report `bench.py` wrote -- the Markdown tables are the source, because they carry everything a
chart needs (the median for the bar, lowest-highest for the whisker, p50/p99, the with/without pair, the
GC pause) and every past report has them, which is what makes the OLD charts reproducible and therefore
this generator checkable. `--check` does exactly that: regenerate an earlier round's charts and compare
them byte for byte with the ones that were drawn by hand.

Run:  python charts.py --report results-2026-09-28.md --out ../docs/images
      python charts.py --report results-2026-09-25.md --check <dir with the hand-drawn SVGs>
      python charts.py --report ... --out ... --sync "<wiki>/images/sedis"

Python 3.9, standard library only, like every other runner here.
"""

import argparse
import decimal
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------------------------------
# The house style. Byte-identical to the hand-drawn charts, and the reason a generated chart and a hand-
# drawn one can be compared at all. The colour variables are declared once for light and once for dark;
# a chart names a contender only through its `--<key>` variable, never a literal colour.
# ---------------------------------------------------------------------------------------------------

STYLE = """<style>
svg{--surface:#ffffff;--ink:#17202c;--muted:#586476;--faint:#8b95a5;--rule:#dbe0e7;--grid:#e7ebf0;\
--skarn:#0b7d74;--skarn1:#5fb3aa;--asyncio:#3572a5;--threads:#8a94a6;--ftthreads:#b3306b;\
--memurai:#7a4fa3;--gc:#c0392b}
@media (prefers-color-scheme: dark){svg{--surface:#161b22;--ink:#e6ebf1;--muted:#a2adbd;--faint:#74808f;\
--rule:#2b3440;--grid:#232b36;--skarn:#3cc2b4;--skarn1:#8adfd5;--asyncio:#6aa6dc;--threads:#9aa4b5;\
--ftthreads:#ec79a8;--memurai:#b38ae0;--gc:#f07c6c}}
.bg{fill:var(--surface);stroke:var(--rule)}
text{font-family:ui-monospace, SFMono-Regular, 'Cascadia Mono', Consolas, Menlo, monospace;\
font-size:12px;fill:var(--muted)}
.t{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;font-size:16px;\
font-weight:600;fill:var(--ink)}
.st{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;\
font-size:12.5px;fill:var(--muted)}
.ax{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;font-size:12px;\
fill:var(--faint)}
.lab{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;\
font-size:13px;fill:var(--ink)}
.box{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;\
font-size:13px;fill:var(--ink)}
.small{font-family:-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;\
font-size:11.5px;fill:var(--muted)}
.val{fill:var(--ink);font-weight:600}
.g{stroke:var(--grid);stroke-width:1}
.b{stroke:var(--rule);stroke-width:1}
.frame{fill:none;stroke:var(--faint);stroke-width:1.2}
.arrow{stroke:var(--muted);stroke-width:1.4;fill:none;marker-end:url(#ah)}
.whisk{stroke:var(--ink);stroke-width:1;opacity:.55}
.pale{fill-opacity:.35}
.f-skarn{fill:var(--skarn)} .s-skarn{stroke:var(--skarn);fill:none}
.f-skarn1{fill:var(--skarn1)} .s-skarn1{stroke:var(--skarn1);fill:none}
.f-asyncio{fill:var(--asyncio)} .s-asyncio{stroke:var(--asyncio);fill:none}
.f-threads{fill:var(--threads)} .s-threads{stroke:var(--threads);fill:none}
.f-ftthreads{fill:var(--ftthreads)} .s-ftthreads{stroke:var(--ftthreads);fill:none}
.f-memurai{fill:var(--memurai)} .s-memurai{stroke:var(--memurai);fill:none}
.f-gc{fill:var(--gc)} .s-gc{stroke:var(--gc);fill:none}
</style>"""

# ---------------------------------------------------------------------------------------------------
# Contender -> colour and display name. This mapping existed NOWHERE before: it was implicit in each
# hand-drawn chart, which is why the same contender could have been given two colours in two pictures
# without anything noticing. `bench.py`'s name is the key, because that is what a report table says.
# ---------------------------------------------------------------------------------------------------

SERIES = {
    "Sedis, 4 shards":        ("Sedis (Skarn)",        "skarn"),
    "Sedis, 1 shard":         ("Sedis, 1 shard",       "skarn1"),
    "Python asyncio":         ("Python asyncio",       "asyncio"),
    "Python threads":         ("Python threads (GIL)", "threads"),
    "Python threads, no GIL": ("Python free-threaded", "ftthreads"),
    "Memurai (C++)":          ("Memurai (C++)",        "memurai"),
}

# The published charts show these five, in this order. A sixth contender may be MEASURED -- the
# 2026-09-28 run had one, the worker variant that was not adopted -- without being drawn; that belongs in
# the report's prose, not in a picture that invites a comparison the project did not make.
PUBLISHED = ["Sedis, 4 shards", "Python asyncio", "Python threads",
             "Python threads, no GIL", "Memurai (C++)"]

COMMANDS = ["set", "get", "incr", "lpush", "hset", "mset"]

W = 780  # every chart is this wide; only the height differs


# ---------------------------------------------------------------------------------------------------
# Reading a report
# ---------------------------------------------------------------------------------------------------

def _rows(text, heading, nth=0):
    """The rows of the nth Markdown table after `heading`, as lists of stripped cells."""
    i = text.find(heading)
    if i < 0:
        raise SystemExit("no heading %r in the report" % heading)
    tables, cur = [], []
    for line in text[i + len(heading):].splitlines():
        s = line.strip()
        if s.startswith("|"):
            cur.append([c.strip() for c in s.strip("|").split("|")])
        elif cur:
            tables.append(cur)
            cur = []
            if len(tables) > nth + 1:
                break
        if s.startswith("### ") and tables:
            break
    if cur:
        tables.append(cur)
    if len(tables) <= nth:
        raise SystemExit("no table %d after %r" % (nth, heading))
    t = tables[nth]
    return t[0], [r for r in t[2:] if r]  # header, body (row 1 is the |---| rule)


OPS = re.compile(r"^(\d+)\s*\((\d+)-(\d+)\)$")
LAT = re.compile(r"^(\d+)\s*/\s*(\d+)$")
AOF = re.compile(r"^(\d+)\s*/\s*(\d+)\s*\(")
GCR = re.compile(r"^(\d+) us")

LOAD_HEADING = {
    (1, 1): "### Phase 1 -- 1 client, no pipelining (latency)",
    (16, 1): "### Phase 1 -- 16 clients, no pipelining (concurrency)",
    (16, 16): "### Phase 1 -- 16 clients, pipelines of 16 (throughput)",
}


def read_report(path):
    """Everything the six charts need, keyed the way they ask for it."""
    text = Path(path).read_text(encoding="utf-8")
    data = {"ops": {}, "lat": {}, "aof": {}, "big": []}
    for load, heading in LOAD_HEADING.items():
        head, body = _rows(text, heading, 0)
        for row in body:
            for name, cell in zip(head[1:], row[1:]):
                m = OPS.match(cell)
                if m:
                    data["ops"][(name, row[0], load)] = tuple(int(x) for x in m.groups())
        head, body = _rows(text, heading, 1)
        for row in body:
            for name, cell in zip(head[1:], row[1:]):
                m = LAT.match(cell)
                if m:
                    data["lat"][(name, row[0], load)] = tuple(int(x) for x in m.groups())
    head, body = _rows(text, "### Phase 2 --", 0)
    for row in body:
        for col, cell in zip(head[1:], row[1:]):
            m = AOF.match(cell)
            if m:
                # "set c=16 P=1" -> ("set", (16, 1))
                w = col.split()
                load = (int(w[1].split("=")[1]), int(w[2].split("=")[1]))
                data["aof"][(row[0], w[0], load)] = (int(m.group(1)), int(m.group(2)))
    _, body = _rows(text, "### Phase 3 --", 0)
    for row in body:
        m = GCR.match(row[6])
        data["big"].append({"name": row[0], "ops": int(row[2]), "p50": int(row[3]),
                            "p99": int(row[4]), "max": int(row[5]),
                            "gc": int(m.group(1)) if m else None})
    return data


# ---------------------------------------------------------------------------------------------------
# SVG plumbing
# ---------------------------------------------------------------------------------------------------

def f1(v):
    """One decimal place, and a tie goes DOWN.

    Binary floating point decides ties by accident: a bar centre of 434.55 is 434.549999... one way of
    reaching it and 434.550000...1 another, so "%.1f" prints 434.5 or 434.6 depending on which additions
    ran. The hand-drawn charts resolved every such tie downwards; rounding the accumulated value to six
    places first and then deciding the tie explicitly makes the output depend on the geometry alone.
    """
    return str(decimal.Decimal(repr(round(v, 6))).quantize(decimal.Decimal("0.1"),
                                                           rounding=decimal.ROUND_HALF_DOWN))


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(title, subtitle, height, body):
    label = esc("%s. %s" % (title, subtitle))
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
           'role="img" aria-label="%s">' % (W, height, W, height, label),
           "<title>%s</title>" % label,
           STYLE,
           '<rect class="bg" x="0.5" y="0.5" width="%d" height="%d" rx="10"/>' % (W - 1, height - 1),
           '<text class="t" x="24" y="34">%s</text>' % esc(title),
           '<text class="st" x="24" y="55">%s</text>' % esc(subtitle)]
    out += body
    out.append("</svg>")
    return "\n".join(out) + "\n"


def legend(names, y=70.0):
    """Laid out, not hand-placed. 13px sans averages 7 px a character here; 20 px between entries."""
    out, x = [], 24.0
    for n in names:
        text, key = SERIES[n]
        out.append('<rect x="%s" y="%s" width="12" height="12" rx="2" class="f-%s"/>' % (f1(x), f1(y), key))
        out.append('<text x="%s" y="%s" class="lab">%s</text>' % (f1(x + 18), f1(y + 10), esc(text)))
        x += 18 + 7 * len(text) + 20
    return out


def nice_step(top):
    """The smallest 1/2/2.5/5-style step that covers `top` in at most five gridlines."""
    if top <= 0:
        return 1
    mag = 10 ** len(str(int(top)))
    while True:
        for f in (0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0):
            step = mag * f
            if step > 0 and top / step <= 5 + 1e-9:
                return step
        mag *= 10


# ---------------------------------------------------------------------------------------------------
# Chart 1-3: grouped vertical bars, one group per command, one bar per contender
# ---------------------------------------------------------------------------------------------------

PLOT_L, PLOT_R, BASE, PLOT_H = 76, 756, 348.0, 252.0


def grouped_bars(data, load, title, subtitle, names=None):
    names = names or PUBLISHED
    vals = [data["ops"][(n, c, load)] for c in COMMANDS for n in names]
    step = nice_step(max(v[2] for v in vals))
    lines = int(round(max(v[2] for v in vals) / step + 0.4999))
    while lines * step < max(v[2] for v in vals):
        lines += 1
    top = step * lines
    scale = PLOT_H / top

    body = legend(names)
    for k in range(lines + 1):
        y = BASE - k * PLOT_H / lines
        cls = "b" if k == 0 else "g"
        body.append('<line x1="%d" y1="%s" x2="%d" y2="%s" class="%s"/>'
                    % (PLOT_L, f1(y), PLOT_R, f1(y), cls))
        body.append('<text x="68" y="%s" text-anchor="end">%s</text>'
                    % (f1(y + 4), "{:,}".format(int(round(k * step)))))

    pitch = (PLOT_R - PLOT_L) / float(len(COMMANDS))
    d = (pitch - 18) / float(len(names))
    bw = round(d * 0.897, 1)
    for gi, command in enumerate(COMMANDS):
        left = PLOT_L + gi * pitch + 10
        for si, n in enumerate(names):
            med, lo, hi = data["ops"][(n, command, load)]
            x = left + si * d
            h = med * scale
            body.append('<rect x="%s" y="%s" width="%s" height="%s" rx="1.5" class="f-%s"/>'
                        % (f1(x), f1(BASE - h), f1(bw), f1(h), SERIES[n][1]))
            cx = x + bw / 2.0
            body.append('<line x1="%s" y1="%s" x2="%s" y2="%s" class="whisk"/>'
                        % (f1(cx), f1(BASE - lo * scale), f1(cx), f1(BASE - hi * scale)))
        mid = left + (len(names) // 2) * d + bw / 2.0
        body.append('<text x="%s" y="368" text-anchor="middle" class="lab">%s</text>'
                    % (f1(mid), command.upper()))
    body.append('<text x="%d" y="392" class="ax">operations per second · bar: median of three '
                'rounds · line: lowest to highest</text>' % PLOT_L)
    return svg(title, subtitle, 410, body)


# ---------------------------------------------------------------------------------------------------
# Chart 4: one client's round trip -- bar to p50, line on to p99
# ---------------------------------------------------------------------------------------------------

def latency_chart(data, command="set", names=None):
    names = names or PUBLISHED
    x0, pxus = 210.0, 3.5
    rows = [(n, data["lat"][(n, command, (1, 1))]) for n in names]
    body = []
    # One tick past what the data needs: the p50 / p99 label is drawn BEYOND the p99 whisker, so the
    # axis has to reach further than the longest bar or the widest label falls off the card.
    top = max(p99 for _, (_, p99) in rows)
    for k in range(0, int(top / 20) + 3):
        x = x0 + k * 20 * pxus
        body.append('<line x1="%s" y1="76" x2="%s" y2="252" class="%s"/>'
                    % (f1(x), f1(x), "b" if k == 0 else "g"))
        body.append('<text x="%s" y="270" text-anchor="middle">%d</text>' % (f1(x), k * 20))
    body.append('<text x="%s" y="290" text-anchor="middle" class="ax">microseconds for one %s, sent, '
                'answered, read</text>' % (f1(x0 + (x - x0) / 2.0), command.upper()))
    for i, (n, (p50, p99)) in enumerate(rows):
        y = 80 + i * 36
        key = SERIES[n][1]
        body.append('<text x="198" y="%d" text-anchor="end" class="lab">%s</text>'
                    % (y + 13, esc(SERIES[n][0])))
        body.append('<rect x="%d" y="%d" width="%s" height="18" rx="2" class="f-%s"/>'
                    % (int(x0), y, f1(p50 * pxus), key))
        e50, e99 = x0 + p50 * pxus, x0 + p99 * pxus
        body.append('<line x1="%s" y1="%d" x2="%s" y2="%d" class="s-%s" stroke-width="2"/>'
                    % (f1(e50), y + 9, f1(e99), y + 9, key))
        body.append('<line x1="%s" y1="%d" x2="%s" y2="%d" class="s-%s" stroke-width="2"/>'
                    % (f1(e99), y + 5, f1(e99), y + 13, key))
        body.append('<text x="%s" y="%d" class="val">%d / %d</text>' % (f1(e99 + 9), y + 13, p50, p99))
    return svg("One client, one command at a time: round-trip latency",
               "%s · bar to the median (p50), line to p99 · labels: p50 / p99 in µs"
               % command.upper(), 310, body)


# ---------------------------------------------------------------------------------------------------
# Chart 5: a million keys -- the slowest answer, with the GC pause marked
# ---------------------------------------------------------------------------------------------------

def keyspace_chart(data):
    x0, pxms = 200.0, 3.6
    # A run may hold contenders that are measured but not published -- the 2026-09-28 round carried a
    # second Sedis whose connections were served by worker actors, an arrangement that was compared and
    # then not adopted. Drawing it would invite a comparison the project did not make.
    rows = [r for r in data["big"] if r["name"] in SERIES]
    body = []
    top = max(r["max"] for r in rows) / 1000.0
    for k in range(0, int(top / 20) + 2):
        x = x0 + k * 20 * pxms
        body.append('<line x1="%s" y1="74" x2="%s" y2="288" class="%s"/>'
                    % (f1(x), f1(x), "b" if k == 0 else "g"))
        body.append('<text x="%s" y="304" text-anchor="middle">%d</text>' % (f1(x), k * 20))
    body.append('<text x="%s" y="324" text-anchor="middle" class="ax">milliseconds · bar: the '
                'slowest round trip in 5 s · red mark: the longest GC pause</text>'
                % f1(x0 + (x - x0) / 2.0))
    for i, r in enumerate(rows):
        y = 83 + i * 36
        name = r["name"]
        label, key = SERIES.get(name, (name, "skarn"))
        ms = r["max"] / 1000.0
        end = x0 + ms * pxms
        body.append('<text x="188" y="%d" text-anchor="end" class="lab">%s</text>'
                    % (y + 13, esc(name if name.startswith("Sedis") else label)))
        body.append('<rect x="%d" y="%d" width="%s" height="18" rx="2" class="f-%s"/>'
                    % (int(x0), y, f1(ms * pxms), key))
        text = "%.1f ms" % ms
        tx = end
        if r["gc"] is not None:
            gms = r["gc"] / 1000.0
            gx = x0 + gms * pxms
            body.append('<line x1="%s" y1="%d" x2="%s" y2="%d" class="s-gc" stroke-width="3"/>'
                        % (f1(gx), y - 3, f1(gx), y + 21))
            text = "%.1f ms  (GC %.1f ms)" % (ms, gms)
            tx = max(end, gx)
        body.append('<text x="%s" y="%d" class="val">%s</text>' % (f1(tx + 9), y + 13, text))
    return svg("A million keys: the slowest answer",
               "SET on random keys of 1,000,000, 16 clients · p99 stays near 1 ms for all; "
               "the worst case differs", 350, body)


# ---------------------------------------------------------------------------------------------------
# Chart 6: what the append-only log costs -- the share of throughput kept
# ---------------------------------------------------------------------------------------------------

AOF_ROWS = [("set", (16, 1), "SET"), ("set", (16, 16), "SET, pipelined"),
            ("incr", (16, 1), "INCR"), ("incr", (16, 16), "INCR, pipelined")]


def persistence_chart(data, names):
    x0, pxpc = 230.0, 4.1
    body = []
    for k in range(0, 6):
        x = x0 + k * 20 * pxpc
        body.append('<line x1="%s" y1="70" x2="%s" y2="368" class="%s"/>'
                    % (f1(x), f1(x), "b" if k == 0 else "g"))
        body.append('<text x="%s" y="384" text-anchor="middle">%d %%</text>' % (f1(x), k * 20))
    body.append('<text x="%s" y="404" text-anchor="middle" class="ax">share of the throughput kept with '
                'the log (16 clients, fsync once a second)</text>' % f1(x0 + (x - x0) / 2.0))
    for gi, n in enumerate(names):
        top = 76 + gi * 76
        key = SERIES[n][1]
        body.append('<text x="218" y="%d" text-anchor="end" class="lab">%s</text>'
                    % (top + 34, esc(SERIES[n][0])))
        for ri, (command, load, label) in enumerate(AOF_ROWS):
            off, on = data["aof"][(n, command, load)]
            pc = 100.0 * on / off if off else 0.0
            y = top + ri * 15
            pale = ' fill-opacity=".55"' if "pipelined" in label else ""
            body.append('<rect x="%d" y="%d" width="%s" height="12" rx="1.5" class="f-%s"%s/>'
                        % (int(x0), y, f1(pc * pxpc), key, pale))
            body.append('<text x="%s" y="%d" class="small">%.0f %%  %s</text>'
                        % (f1(x0 + pc * pxpc + 6), y + 10, pc, label))
    return svg("What an append-only file costs",
               "each contender with and without its log, back to back · 100 % = nothing lost",
               420, body)


# ---------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------
# The diagram kit
#
# The charts and the diagrams share one style block, so they share dark mode, typography and the palette:
# a shard drawn in the architecture picture is the same colour as the Sedis bar in a chart. A diagram is
# written as boxes and arrows in absolute coordinates -- there is no layout engine here, and for four
# pictures there does not need to be.
# ---------------------------------------------------------------------------------------------------

DEFS = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted)"/></marker></defs>')


def box(x, y, w, h, key, lines, opacity=".14", stack=0):
    """A titled box. `stack` draws that many offset copies behind it -- "several of these"."""
    out = []
    for k in range(stack, 0, -1):
        off = k * 6
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" class="f-%s" fill-opacity=".10"/>'
                   % (x + off, y + off, w, h, key))
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" class="frame"/>'
                   % (x + off, y + off, w, h))
    if stack:
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" class="bg"/>' % (x, y, w, h))
    out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" class="f-%s" fill-opacity="%s"/>'
               % (x, y, w, h, key, opacity))
    out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" class="frame"/>' % (x, y, w, h))
    cx = x + w // 2
    if not lines:          # a frame that is filled with free text by the caller
        return out
    if len(lines) == 1:
        out.append('<text x="%d" y="%d" text-anchor="middle" class="box">%s</text>'
                   % (cx, y + h // 2 + 5, esc(lines[0])))
    else:
        top = y + h // 2 - 8 * (len(lines) - 1)
        out.append('<text x="%d" y="%d" text-anchor="middle" class="box">%s</text>'
                   % (cx, top, esc(lines[0])))
        for i, line in enumerate(lines[1:]):
            out.append('<text x="%d" y="%d" text-anchor="middle" class="small">%s</text>'
                       % (cx, top + 18 * (i + 1), esc(line)))
    return out


def arrow(x1, y1, x2, y2):
    return ['<path d="M%d,%d L%d,%d" class="arrow"/>' % (x1, y1, x2, y2)]


def tag(x, y, text, cls="small", anchor=None):
    a = ' text-anchor="%s"' % anchor if anchor else ""
    return ['<text x="%d" y="%d"%s class="%s">%s</text>' % (x, y, a, cls, esc(text))]


NOTE_COLS = 118  # 11.5px sans, from x=24 to the right margin of a 780-wide card


def wrap(line, cols=NOTE_COLS):
    out, cur = [], ""
    for word in line.split():
        if cur and len(cur) + 1 + len(word) > cols:
            out.append(cur)
            cur = word
        else:
            cur = cur + " " + word if cur else word
    if cur:
        out.append(cur)
    return out


def with_notes(title, subtitle, body, top, *lines):
    """Finish a diagram: wrap the notes, then make the card exactly tall enough to hold them.

    SVG has no text flow -- a <text> runs off the edge of the card rather than breaking -- so the number
    of note lines is not known until they are wrapped, and the height cannot be a constant.
    """
    rows = []
    for line in lines:
        rows += wrap(line)
    out = list(body)
    for i, row in enumerate(rows):
        out += tag(24, top + 17 * i, row)
    return svg(title, subtitle, top + 17 * (len(rows) - 1) + 20, [DEFS] + out)


# --- 1. how Sedis is built -------------------------------------------------------------------------

def architecture():
    b = []
    b += box(24, 110, 120, 60, "threads", ["clients", "RESP over TCP"])
    b += box(186, 80, 150, 50, "skarn", ["acceptor", "an activated listener"])
    b += box(186, 166, 150, 56, "skarn", ["connection", "one actor per client"], stack=2)
    for i, (y, name) in enumerate(((92, "shard 0"), (148, "shard 1"), (204, "shard 2, 3 \u2026"))):
        b += box(430, y, 150, 48, "skarn", [name, "a heap of its own"])
    b += box(620, 128, 140, 56, "memurai", ["supervisor", "restarts a crashed shard"])
    b += box(430, 268, 150, 46, "skarn1", ["append-only log", "one file per shard"])
    b += arrow(144, 130, 186, 108)
    b += arrow(144, 150, 186, 196)
    b += arrow(261, 130, 261, 166)
    b += arrow(348, 196, 430, 116)
    b += arrow(348, 196, 430, 172)
    b += arrow(348, 196, 430, 228)
    b += arrow(620, 156, 580, 116)
    b += arrow(620, 156, 580, 172)
    b += arrow(620, 156, 580, 228)
    b += arrow(505, 252, 505, 268)
    return with_notes(
        "How Sedis is built", "actors that share nothing: every message is copied", b, 344,
        "A command goes to the shard its key hashes to; all commands of one socket read go to a shard "
        "as one message, and come back as one.",
        "Each shard lives in a slot, an address that outlives its actor: a restarted shard answers "
        "where its predecessor did, and a send in between waits instead of being dropped.",
        "A shard appends the EFFECT of a command to its own log \u2014 the resulting state, not the "
        "request \u2014 and replays it before the acceptor starts.")


# --- 2. the path of one command --------------------------------------------------------------------

def command_path():
    b = []
    b += box(24, 96, 132, 56, "threads", ["socket read", "one wake-up"])
    b += box(188, 96, 156, 56, "skarn", ["RespReader", "frames, keeps a tail"])
    b += box(376, 96, 162, 56, "skarn", ["split by key", "FNV-1a mod N"])
    for y, name in ((72, "shard 0"), (128, "shard 1"), (184, "shard 2")):
        b += box(576, y, 180, 46, "skarn", [name])
    b += box(376, 212, 162, 56, "skarn", ["pending", "by sequence number"])
    b += box(188, 212, 156, 56, "skarn", ["one write", "the whole round at once"])
    b += box(24, 212, 132, 56, "threads", ["socket write", "one flush"])
    b += arrow(156, 124, 188, 124)
    b += arrow(344, 124, 376, 124)
    b += arrow(538, 118, 576, 95)
    b += arrow(538, 122, 576, 151)
    b += arrow(538, 126, 576, 207)
    b += arrow(576, 207, 538, 232)
    b += arrow(576, 151, 538, 236)
    b += arrow(576, 95, 538, 240)
    b += arrow(376, 240, 344, 240)
    b += arrow(188, 240, 156, 240)
    b += tag(546, 168, "one Batch per shard, not per command")
    return with_notes(
        "The path of one command", "from a socket read to the answer, in one round", b, 300,
        "One socket read can carry many commands \u2014 a pipelining client sends sixteen. They are "
        "split by shard, and each shard gets ONE message whatever the count.",
        "Every command keeps its sequence number, so answers that come back out of order are put back "
        "in order before anything is written.",
        "The write happens once per round, not once per answer: what costs is the wake-up, and this "
        "pays for it once.")


# --- 3. sharding and key routing --------------------------------------------------------------------

def sharding():
    b = []
    b += box(24, 108, 190, 56, "threads", ["MGET k1 k2 k3", "one command, three keys"])
    b += box(254, 108, 160, 56, "skarn", ["FNV-1a mod N", "the key picks the shard"])
    for y, name, keys in ((72, "shard 0", "k1"), (128, "shard 1", "k3"), (184, "shard 2", "k2")):
        b += box(454, y, 150, 46, "skarn", [name + "  \u2190  " + keys])
    b += box(644, 128, 112, 56, "memurai", ["merge", "one answer"])
    b += arrow(214, 136, 254, 136)
    b += arrow(414, 132, 454, 95)
    b += arrow(414, 136, 454, 151)
    b += arrow(414, 140, 454, 207)
    b += arrow(604, 95, 644, 150)
    b += arrow(604, 151, 644, 154)
    b += arrow(604, 207, 644, 158)
    return with_notes(
        "Sharding: which shard a key belongs to",
        "a key's hash decides, and a multi-key command is split and merged", b, 272,
        "A shard is a whole key-value store with a heap of its own, so a collection stops one shard and "
        "not the server: four shards divide the pause by four.",
        "A multi-key command is cut along the shards and put back together by the merge it needs \u2014 "
        "Sum (DEL, EXISTS), AllOk (MSET), Gather (MGET), Concat.",
        "The order of the answer follows the order of the request, not the order the shards answered "
        "in.")


# --- 4. let it crash, and how a lost request is found ------------------------------------------------

def let_it_crash():
    b = []
    b += box(24, 100, 156, 56, "memurai", ["supervisor", "one_for_one"])
    b += box(232, 76, 190, 46, "skarn", ["slot 0  \u2014  running"])
    b += box(232, 132, 190, 46, "gc", ["slot 1  \u2014  restarting"])
    b += box(232, 188, 190, 46, "skarn", ["slot 2  \u2014  running"])
    b += box(470, 108, 286, 90, "skarn1", [])
    b += tag(490, 134, "A slot outlives its actor.", cls="box")
    b += tag(490, 156, "A restarted shard answers where")
    b += tag(490, 173, "its predecessor did; a send in")
    b += tag(490, 190, "between waits. There is no kill.")
    b += arrow(180, 118, 232, 99)
    b += arrow(180, 128, 232, 155)
    b += arrow(180, 138, 232, 211)
    b += tag(24, 176, "5 restarts in 10 s,")
    b += tag(24, 193, "then it gives up")
    b += box(24, 262, 140, 44, "threads", ["connection"])
    for i, (x, n) in enumerate(((216, "batch 7"), (330, "batch 8"), (444, "batch 9"))):
        b += box(x, 262, 96, 44, "skarn", [n])
    b += box(584, 262, 172, 44, "skarn1", ["answer to 9"])
    b += arrow(164, 284, 216, 284)
    b += arrow(312, 284, 330, 284)
    b += arrow(426, 284, 444, 284)
    b += arrow(540, 284, 584, 284)
    return with_notes(
        "Let it crash, and notice what was lost",
        "a supervisor restarts a shard; batch numbers find the requests that died with it", b, 334,
        "Mail is FIFO per sender, so the answer to batch 9 PROVES that every earlier open batch to that "
        "shard was lost when it crashed \u2014 no monitor and no timeout is needed.",
        "A connection that has waited a second with nothing back sends an empty probe batch, so the "
        "proof arrives even when the client has gone quiet.",
        "A lost part is answered honestly: the command may or may not have run. Both halves are needed "
        "\u2014 removing either one makes the self-test hang.")


CHARTS = {
    "throughput-1-client.svg":
        lambda d: grouped_bars(d, (1, 1), "One client, one command at a time",
                               "operations per second for each command · the latency of one round "
                               "trip decides"),
    "throughput-16-clients.svg":
        lambda d: grouped_bars(d, (16, 1), "16 clients, one command at a time",
                               "operations per second for each command · concurrency without "
                               "pipelining"),
    "throughput-pipelined.svg":
        lambda d: grouped_bars(d, (16, 16), "16 clients, pipelines of 16 commands",
                               "operations per second for each command · the throughput case"),
    "latency-1-client.svg": latency_chart,
    "large-keyspace.svg": keyspace_chart,
    "persistence.svg": lambda d: persistence_chart(
        d, [n for n in ["Sedis, 4 shards", "Memurai (C++)", "Python asyncio", "Python threads, no GIL"]
            if any(k[0] == n for k in d["aof"])]),
    # The diagrams explain rather than measure, so they ignore the report entirely.
    "architecture.svg": lambda d: architecture(),
    "command-path.svg": lambda d: command_path(),
    "sharding.svg": lambda d: sharding(),
    "let-it-crash.svg": lambda d: let_it_crash(),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="a results-*.md written by bench.py")
    ap.add_argument("--out", help="directory to write the SVGs to")
    ap.add_argument("--check", help="directory of existing SVGs to compare against, byte for byte")
    ap.add_argument("--sync", help="a second directory to copy the finished SVGs to (the wiki)")
    ap.add_argument("--only", help="comma-separated chart file names, for a quick look")
    opts = ap.parse_args()
    data = read_report(opts.report)

    wanted = CHARTS
    if opts.only:
        wanted = dict((k, v) for k, v in CHARTS.items() if k in opts.only.split(","))

    made = {}
    for name, fn in sorted(wanted.items()):
        try:
            made[name] = fn(data)
        except KeyError as e:
            print("  %-28s skipped: the report has no %s" % (name, e))

    if opts.check:
        bad = 0
        for name, text in sorted(made.items()):
            old = Path(opts.check) / name
            if not old.is_file():
                print("  %-28s MISSING in %s" % (name, opts.check))
                bad += 1
                continue
            was = old.read_text(encoding="utf-8")
            if was == text:
                print("  %-28s identical (%d bytes)" % (name, len(text)))
            else:
                bad += 1
                a, b = was.splitlines(), text.splitlines()
                print("  %-28s DIFFERS: %d vs %d lines" % (name, len(a), len(b)))
                shown = 0
                for i in range(max(len(a), len(b))):
                    x = a[i] if i < len(a) else "<none>"
                    y = b[i] if i < len(b) else "<none>"
                    if x != y:
                        print("      line %d\n        was: %s\n        now: %s" % (i + 1, x, y))
                        shown += 1
                        if shown == 6:
                            print("      ...")
                            break
        return 1 if bad else 0

    if not opts.out:
        sys.exit("either --out or --check")
    out = Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(made.items()):
        (out / name).write_text(text, encoding="utf-8")
        print("  %-28s %6d bytes" % (name, len(text)))
    if opts.sync:
        dst = Path(opts.sync)
        dst.mkdir(parents=True, exist_ok=True)
        for name in sorted(made):
            shutil.copyfile(str(out / name), str(dst / name))
        print("  synced %d files to %s" % (len(made), dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
