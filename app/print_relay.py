#!/usr/bin/env python3
"""Print relay: HTTP -> Epson TM-U220 (9-pin impact, ESC/POS over raw TCP 9100).

A web page can't open a raw TCP connection, so pages (the Meal Plan Generator) open a link to
this relay, and scripts POST JSON to it. The relay lays the job out for the printer and sends it.
It covers everything in the printing-on-epson-tm-u220 skill: Font A/B, bold, underline, double
height/width, alignment, red, accented text, GB18030 Chinese, images (grid / blue-noise dither,
fine mode), bar / line / pie charts with legends, inline bars, upside-down pages, stored (NV)
logos with a daily write budget, cut, cancelling a running job, and asking the printer its model.

    python3 print_relay.py --printer <ip>        print to <ip>:9100, listens on :8765
    python3 print_relay.py --dry-run jobs        save .bin + .txt per job instead of printing
    python3 print_relay.py --selftest            render every feature, no printer needed

Settings also come from the environment: PRINTER_IP, RELAY_PORT, RELAY_BIND, RELAY_DRY_RUN,
RELAY_TOKEN, RELAY_STATE, RELAY_NV_DAILY_LIMIT. Needs tmu220.py (from the skill) next to it.
Python 3.8+, standard library only. Keep it on your home network: GET /print and /test are open.
"""
import argparse, base64, html, json, math, os, re, socket, threading, time, unicodedata, zlib
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tmu220 as p
from tmu220 import ESC, FS, Canvas

VERSION = '2.0'
PAPER = {'76': 200, '69.5': 180, '57.5': 150}       # printable dots per line
ASCII_DOTS = {'A': 200 / 35, 'B': 5.0}               # Font A 35 per line, Font B 40 per line (76 mm)
CJK_DOTS = 16                                         # Chinese 16x16 font
ALIGN = {'left': p.LEFT, 'center': p.CENTER, 'right': p.RIGHT}
ONE_WAY, TWO_WAY = ESC + b'U\x01', ESC + b'U\x00'
CLEAR_BUFFER = bytes.fromhex('10140801031401060208')  # real-time: stop a running job (new connection)
NV_HARD_LIMIT = 10                                    # Epson: at most 10 NV writes per day
LEVELS = [0.7, 0.2, 0.45, 0.85, 0.08, 0.6]            # default grid shades for series / slices
DASHES = [0, 6, 2, 10]
MAX_BODY = 8 * 1024 * 1024
MAX_IMAGE_ROWS = 1600                                 # ~28 cm of paper per image
SWAP = {'¼': ' 1/4', '½': ' 1/2', '¾': ' 3/4', '⅓': ' 1/3', '⅔': ' 2/3', '×': 'x', '·': '-', '•': '*',
        '–': '-', '—': '-', '≈': '~', '…': '...', '°': '', '’': "'", '‘': "'", '“': '"', '”': '"', '→': '->', '\t': ' '}


class JobError(ValueError):
    """A problem with the job itself: reported to the client as 400."""


# ================================================================ text
def kind(c):
    """'a' ASCII, 'l' accented Latin (cp437, single-byte mode), 'c' Chinese/full-width (GBK), None."""
    o = ord(c)
    if o < 128:
        return 'a'
    if o < 0x250 and unicodedata.category(c).startswith('L'):
        try:
            c.encode('cp437')
            return 'l'
        except UnicodeEncodeError:
            pass
    try:
        if len(c.encode('gbk')) == 2:
            return 'c'
    except UnicodeEncodeError:
        pass
    return None


def asciify(c):
    return ''.join(x for x in unicodedata.normalize('NFKD', c) if ' ' <= x < '\x7f')


def clean(s):
    out = []
    for ch in str(s if s is not None else ''):
        for c in SWAP.get(ch, ch):
            k = kind(c)
            if k == 'a':
                out.append(c if ' ' <= c < '\x7f' else ' ')
            elif k:
                out.append(c)
            else:
                out.append(asciify(c) or '?')
    return re.sub(r'(?<=\S)  +(?=\d/\d)', ' ', ''.join(out))


def char_dots(c, font='B', dw=False):
    return (CJK_DOTS if kind(c) == 'c' else ASCII_DOTS[font]) * (2 if dw else 1)


def width(s, font='B', dw=False):
    return sum(char_dots(c, font, dw) for c in s)


def wrap(s, avail, font='B', dw=False):
    """Greedy wrap by dots. Breaks at spaces; a run of Chinese moves to the next line whole
    when it fits there, and splits between characters only when wider than a line."""
    w = lambda t: width(t, font, dw)
    toks = []
    for tok in re.findall(r'[\x21-\x7eÀ-ɏ]+|[ ]+|[^\x00-\x7fÀ-ɏ]+', s):
        toks.extend(list(tok) if kind(tok[0]) == 'c' and w(tok) > avail else [tok])
    lines, cur = [], ''
    for tok in toks:
        if tok.isspace():
            if cur and w(cur + tok) <= avail:
                cur += tok
            elif cur:
                lines.append(cur.rstrip()); cur = ''
            continue
        if w(cur + tok) <= avail:
            cur += tok
            continue
        if cur.strip():
            lines.append(cur.rstrip())
        cur = ''
        while w(tok) > avail:
            n = max(1, int(avail // char_dots(tok[0], font, dw)))
            lines.append(tok[:n]); tok = tok[n:]
        cur = tok
    if cur.strip() or not lines:
        lines.append(cur.rstrip())
    return lines


def encode_text(t):
    """-> (prefix, body, suffix, has_chinese). Chinese lines go out as GB18030 (Kanji mode, the
    power-on default on this model); accented Latin lines switch to single-byte cp437 with FS ."""
    kinds = {kind(c) for c in t}
    if 'c' in kinds:
        t = ''.join((asciify(c) or '?') if kind(c) == 'l' else c for c in t)
        return b'', t.encode('gb18030', 'replace'), b'', True
    if 'l' in kinds:
        return FS + b'.' + ESC + b't\x00', t.encode('cp437', 'replace'), FS + b'&', False
    return b'', t.encode('ascii', 'replace'), b'', False


def styled_line(t, font='B', bold=False, underline=False, red=False, align='left', dh=False, dw=False):
    """One printed line that restores every mode it sets (needed for upside-down pages)."""
    pre, body, post, cjk = encode_text(t)
    mode = (0x01 if font == 'B' else 0) | (0x10 if dh else 0) | (0x20 if dw else 0)
    on, off = ESC + b'!' + bytes([mode]), b''
    if cjk and (dh or dw):               # Kanji size is FS !, not ESC ! (not yet checked on paper)
        on += FS + b'!' + bytes([(0x04 if dw else 0) | (0x08 if dh else 0)]); off = FS + b'!\x00' + off
    if bold:
        on += ESC + b'E\x01'; off = ESC + b'E\x00' + off
    if underline:
        on += ESC + b'-\x01' + (FS + b'-\x01' if cjk else b'')
        off = ESC + b'-\x00' + (FS + b'-\x00' if cjk else b'') + off
    if red:
        on += ESC + b'r\x01'; off = ESC + b'r\x00' + off
    if align != 'left':
        on += ALIGN[align]; off = p.LEFT + off
    seg = on + pre + body + b'\n' + post + off + ESC + b'!\x01'
    return ONE_WAY + seg + TWO_WAY if (cjk or dh) else seg   # two-pass glyphs: print one way


# ================================================================ images
def decode_image(raw):
    """PNG (non-interlaced; gray, RGB, palette, +alpha; 1-16 bit) or PGM/PBM -> (w, h, rows of 0..255)."""
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return _png(raw)
    if raw[:2] in (b'P1', b'P2', b'P4', b'P5'):
        return _pnm(raw)
    raise JobError('image must be PNG, PGM or PBM')


def _png(data):
    pos, idat, plte, trns, hdr = 8, b'', None, None, None
    while pos + 8 <= len(data):
        n = int.from_bytes(data[pos:pos + 4], 'big')
        typ, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b'IHDR':
            hdr = body
        elif typ == b'PLTE':
            plte = body
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IDAT':
            idat += body
        elif typ == b'IEND':
            break
    if not hdr:
        raise JobError('PNG has no header')
    w, h = int.from_bytes(hdr[0:4], 'big'), int.from_bytes(hdr[4:8], 'big')
    depth, ctype, interlace = hdr[8], hdr[9], hdr[12]
    if interlace:
        raise JobError('interlaced PNG is not supported; save it without interlacing')
    if ctype not in (0, 2, 3, 4, 6) or w * h > 16_000_000:
        raise JobError('unsupported or oversized PNG')
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bits = ch * depth
    stride, bpp = (w * bits + 7) // 8, max(1, bits // 8)
    raw = zlib.decompress(idat)
    prev, i, rows = bytearray(stride), 0, []
    for _ in range(h):
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride]); i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line); prev = line
    mx = (1 << depth) - 1
    out = []
    for line in rows:
        r_out = []
        for x in range(w):
            if depth < 8:
                s = [(line[(x * depth) // 8] >> (8 - depth - (x * depth) % 8)) & mx]
            else:
                step = depth // 8
                s = [line[(x * ch + k) * step] for k in range(ch)]   # high byte of 16-bit samples
            if ctype == 3:
                r, g, b = plte[3 * s[0]:3 * s[0] + 3]
                a = trns[s[0]] if trns and s[0] < len(trns) else 255
            elif ctype in (0, 4):
                r = g = b = s[0] * 255 // mx if depth < 8 else s[0]
                a = s[1] if ctype == 4 else 255
            else:
                r, g, b = s[:3]
                a = s[3] if ctype == 6 else 255
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            r_out.append(lum * a / 255 + 255 * (1 - a / 255))         # composite on white paper
        out.append(r_out)
    return w, h, out


def _pnm(data):
    magic = data[:2]
    toks, pos = [], 2
    need = 2 if magic in (b'P1', b'P4') else 3
    while len(toks) < need:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b'#':
            pos = data.index(b'\n', pos) + 1
            continue
        start = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        toks.append(int(data[start:pos]))
    pos += 1
    w, h = toks[0], toks[1]
    mx = toks[2] if need == 3 else 1
    if magic == b'P5':
        vals = list(data[pos:pos + w * h])
    elif magic == b'P4':
        stride, vals = (w + 7) // 8, []
        for y in range(h):
            row = data[pos + y * stride:pos + (y + 1) * stride]
            vals += [(row[x // 8] >> (7 - x % 8)) & 1 for x in range(w)]
    else:
        vals = [int(v) for v in data[pos:].split()][:w * h]
    if magic in (b'P1', b'P4'):
        vals = [0 if v else 255 for v in vals]                   # PBM: 1 = black
    else:
        vals = [v * 255 / mx for v in vals]
    return w, h, [vals[y * w:(y + 1) * w] for y in range(h)]


def resample(gray, w, h, tw, th):
    """Area-average downscale (nearest when enlarging) -> rows of darkness 0..1."""
    out = []
    for ty in range(th):
        y0, y1 = ty * h / th, (ty + 1) * h / th
        ys = range(int(y0), max(int(y0) + 1, min(h, math.ceil(y1))))
        row = []
        for tx in range(tw):
            x0, x1 = tx * w / tw, (tx + 1) * w / tw
            xs = range(int(x0), max(int(x0) + 1, min(w, math.ceil(x1))))
            tot = sum(gray[y][x] for y in ys for x in xs)
            row.append(1 - tot / (len(ys) * len(xs)) / 255)
        out.append(row)
    return out


# ================================================================ layout
class Ctx:
    def __init__(self, job):
        self.paper = str(job.get('paper', '76'))
        if self.paper not in PAPER:
            raise JobError('paper must be one of ' + ', '.join(PAPER))
        self.width = PAPER[self.paper]
        self.upside = bool(job.get('upside_down'))
        self.cols = int(self.width // ASCII_DOTS['B'])
        self.warnings = []


def image_bytes(canvas, fine, ctx):
    return canvas.flipped_image(fine) if ctx.upside else canvas.bit_image(fine)


def legend_lines(entries):
    """entries: (8-dot canvas, label). Identity never only in a texture: every key has a label."""
    return [(ONE_WAY + key.inline() + encode_text(' ' + clean(label))[1] + b'\n' + TWO_WAY, '[key] ' + clean(label))
            for key, label in entries]


def swatch(level):
    c = Canvas(20, 8)
    c.regions(lambda x, y: 0 if 0 <= x < 20 and 1 <= y <= 6 else -1, [level])
    return c


def line_key(dash=0):
    c = Canvas(20, 8)
    c.line(0, 3, 19, 3, dash)
    return c


def nice_step(top, target=4):
    if top <= 0:
        return 1
    raw = top / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag


def fmt_num(v):
    return str(int(v)) if float(v).is_integer() else f'{v:g}'


def text_out(t, **st):
    return (styled_line(t, **st), t)


def render_text(b, ctx):
    font = str(b.get('font', 'B')).upper()
    if font not in ASCII_DOTS:
        raise JobError('font must be A or B')
    size = b.get('size', 'normal')
    dh, dw = size in ('height', 'both'), size in ('width', 'both')
    align = b.get('align', 'left')
    if align not in ALIGN:
        raise JobError('align must be left, center or right')
    st = dict(font=font, bold=bool(b.get('bold')), underline=bool(b.get('underline')), red=bool(b.get('red')),
              align=align, dh=dh, dw=dw)
    lines = wrap(clean(b.get('text', '')).strip(), ctx.width, font, dw) if b.get('wrap', True) else [clean(b.get('text', ''))]
    return [text_out(ln, **st) for ln in lines]


def render_labeled(b, ctx):
    label = (clean(b.get('label', '')).strip() + ' ').ljust(3)
    pad = ' ' * len(label)
    red = bool(b.get('red'))
    rows = wrap(clean(b.get('text', '')).strip(), ctx.width - width(label))
    return [text_out((label if i == 0 else pad) + ln, red=red) for i, ln in enumerate(rows)]


def render_row(left, right, ctx, box=False, red=False, bold=False):
    """Left text, right text pushed to the right edge; tick box optional. Wraps the left part."""
    pre = '[ ] ' if box else ''
    avail = ctx.width - width(pre)
    left, right = clean(left).strip(), clean(right).strip()
    rows = wrap(left, avail)
    if right:
        gap = int((avail - width(rows[-1]) - width(right)) // ASCII_DOTS['B'])
        if gap >= 2:
            rows[-1] += ' ' * gap + right
        else:
            rows.append(' ' * max(0, int((avail - width(right)) // ASCII_DOTS['B'])) + right)
    return [text_out((pre if i == 0 else ' ' * len(pre)) + ln, red=red, bold=bold) for i, ln in enumerate(rows)]


def render_image(b, ctx):
    try:
        raw = base64.b64decode(b.get('data', ''), validate=False)
    except Exception:
        raise JobError('image data must be base64')
    w, h, gray = decode_image(raw)
    fine = bool(b.get('fine'))
    method = b.get('dither', 'blue')
    if method not in ('grid', 'blue', 'threshold'):
        raise JobError('dither must be grid, blue or threshold')
    tw = max(1, min(int(b.get('width') or ctx.width), ctx.width))
    th = max(1, round(h * (tw / w) / (p.ASPECT_FINE if fine else p.ASPECT)))
    if th > MAX_IMAGE_ROWS:
        raise JobError(f'image would be {th} rows tall; make it narrower (max {MAX_IMAGE_ROWS} rows)')
    lv = resample(gray, w, h, tw, th)
    gamma = float(b.get('gamma', 1.0))
    align = b.get('align', 'center')
    x0 = {'left': 0, 'center': (ctx.width - tw) // 2, 'right': ctx.width - tw}.get(align, (ctx.width - tw) // 2)
    c = Canvas(ctx.width, th)                 # always full width: flipped_image needs it
    for y in range(th):
        for x in range(tw):
            v = lv[y][x] ** gamma if gamma != 1.0 else lv[y][x]
            if b.get('invert'):
                v = 1 - v
            on = v > 0.5 if method == 'threshold' else p.dot_on(x0 + x, y, p._fine_tone(v) if fine else v, method)
            c.set(x0 + x, y, on)
    if fine and method == 'grid':
        ctx.warnings.append('grid dither in fine mode looked bad on paper; blue is recommended')
    return [(image_bytes(c, fine, ctx), f'[image {tw}x{th} dots, {method}{", fine" if fine else ""}]')]


def _series(b):
    series = b.get('series') or []
    if not series or not all(isinstance(s.get('values'), list) and s['values'] for s in series):
        raise JobError('chart needs series: [{"name": ..., "values": [...]}]')
    return series


def chart_bar(b, ctx):
    series = _series(b)
    n = len(series[0]['values'])
    labels = [clean(x) for x in b.get('labels', [])][:n]
    W, H = ctx.width, int(b.get('height', 80))
    slot = W // n
    m = len(series)
    pad = max(1, slot // 8)
    bw = (slot - 2 * pad - (m - 1)) // m
    if bw < 2:
        raise JobError('too many bars for the paper width')
    vmax = float(b.get('max') or max(max(s['values']) for s in series))
    step = float(b.get('gridline') or nice_step(vmax))
    top = math.ceil(vmax / step) * step or 1
    scale = (H - 2) / top
    c = Canvas(W, H)
    k = 1
    while k * step <= top:
        yy = H - 1 - round(k * step * scale)
        c.line(0, yy, W - 1, yy, dash=2)
        k += 1

    def region(x, y):
        if not (0 <= x < slot * n and 0 <= y < H):
            return -1
        i, col = x // slot, x % slot - pad
        if col < 0:
            return -1
        s, within = col // (bw + 1), col % (bw + 1)
        if s >= m or within >= bw:
            return -1
        v = series[s]['values'][i] if i < len(series[s]['values']) else 0
        return s if y >= H - 1 - round(max(0, v) * scale) else -1
    levels = [float(s.get('level', LEVELS[j % len(LEVELS)])) for j, s in enumerate(series)]
    c.regions(region, levels, 'grid')
    c.line(0, H - 1, W - 1, H - 1)
    out = [text_out(clean(b.get('caption') or f'Lines every {fmt_num(step)}'))]
    if m > 1 or series[0].get('name'):
        out += legend_lines([(swatch(levels[j]), s.get('name', f'Series {j + 1}')) for j, s in enumerate(series)])
    out.append((image_bytes(c, False, ctx), f'[bar chart {n} groups x {m} series]'))
    if labels:
        chars = max(1, int(slot // ASCII_DOTS['B']))
        out.append(text_out(''.join(l[:chars].center(chars) for l in labels).rstrip()))
    return out


def chart_line(b, ctx):
    series = _series(b)
    n = len(series[0]['values'])
    if n < 2:
        raise JobError('a line chart needs at least 2 points')
    fine = bool(b.get('fine', True))
    W, H = ctx.width, int(b.get('height', 80))
    rows = H * 2 if fine else H
    vmax = float(b.get('max') or max(max(s['values']) for s in series))
    vmin = float(b.get('min', 0))
    step = float(b.get('gridline') or nice_step(vmax - vmin))
    top = vmin + (math.ceil((vmax - vmin) / step) * step or 1)
    scale = (rows - 2) / (top - vmin)
    c = Canvas(W, rows)
    k = 1
    while vmin + k * step <= top:
        yy = rows - 1 - round(k * step * scale)
        c.line(0, yy, W - 1, yy, dash=3)
        k += 1
    c.line(0, rows - 1, W - 1, rows - 1)
    c.line(0, 0, 0, rows - 1)
    xs = [round(i * (W - 1) / (n - 1)) for i in range(n)]
    dashes = []
    for j, s in enumerate(series):
        d = int(s.get('dash', DASHES[j % len(DASHES)]))
        dashes.append(d)
        v = s['values']
        for i in range(min(len(v), n) - 1):
            c.line(xs[i], rows - 1 - round((v[i] - vmin) * scale), xs[i + 1], rows - 1 - round((v[i + 1] - vmin) * scale), d)
    out = [text_out(clean(b.get('caption') or f'Lines every {fmt_num(step)}'))]
    if len(series) > 1 or series[0].get('name'):
        out += legend_lines([(line_key(dashes[j]), s.get('name', f'Series {j + 1}')) for j, s in enumerate(series)])
    out.append((image_bytes(c, fine, ctx), f'[line chart {n} points x {len(series)} series]'))
    labels = [clean(x) for x in b.get('labels', [])][:n]
    if labels:
        row = [' '] * ctx.cols
        every = max(1, math.ceil(len(labels) / 5))
        for i in range(0, len(labels), every):
            lab = labels[i][:ctx.cols]
            pos = max(0, min(int(xs[i] // ASCII_DOTS['B']), ctx.cols - len(lab)))
            if all(ch == ' ' for ch in row[max(0, pos - 1):pos + len(lab) + 1]):
                row[pos:pos + len(lab)] = list(lab)
        out.append(text_out(''.join(row).rstrip()))
    return out


def chart_pie(b, ctx):
    slices = b.get('slices') or []
    if not slices or len(slices) > 6:
        raise JobError('pie needs 1 to 6 slices: [{"name": ..., "value": ...}]')
    total = sum(float(s['value']) for s in slices)
    if total <= 0:
        raise JobError('pie values must add up to more than 0')
    shares = [float(s['value']) / total for s in slices]
    levels = [float(s.get('level', [0.85, 0.5, 0.25, 0.08, 0.65, 0.35][i])) for i, s in enumerate(slices)]
    R = max(8, min(int(b.get('radius', 44)), ctx.width // 2 - 1))
    rows = math.ceil(2 * R / p.ASPECT) + 2
    cx, cy = ctx.width // 2, rows / 2

    def region(x, y):
        dx, dy = x - cx, (y - cy) * p.ASPECT
        if dx * dx + dy * dy > R * R:
            return -1
        ang = (math.atan2(dx, -dy) / (2 * math.pi)) % 1           # clockwise from 12 o'clock
        upto = 0.0
        for i, sh in enumerate(shares):
            upto += sh
            if ang < upto:
                return i
        return len(shares) - 1
    c = Canvas(ctx.width, rows)
    c.regions(region, levels, 'grid')
    out = [(image_bytes(c, False, ctx), f'[pie chart {len(slices)} slices]')]
    out += legend_lines([(swatch(levels[i]), f'{s.get("name", "")} {shares[i]:.0%}') for i, s in enumerate(slices)])
    return out


def chart_hbar(b, ctx):
    """Bars inside text lines (budget style): label, inline bar, value; rows over `over` print red."""
    rows = b.get('rows') or []
    if not rows:
        raise JobError('hbar needs rows: [{"label": ..., "value": ...}]')
    scale_max = float(b.get('max') or max(float(r['value']) for r in rows))
    over = b.get('over')
    unit = clean(b.get('unit', ''))
    lab_chars = int(b.get('label_width', 9))
    bar_w = int(b.get('bar_width', min(90, ctx.width - int(lab_chars * ASCII_DOTS['B']) - 50)))
    out = []
    for r in rows:
        v = float(r['value'])
        bar = Canvas(bar_w, 8)
        filled = round(bar_w * min(v, scale_max) / scale_max) if scale_max else 0
        bar.regions(lambda x, y: 0 if 0 <= x < filled and 1 <= y <= 6 else -1, [float(r.get('level', 0.5))])
        is_over = over is not None and v > float(over)
        label = clean(r.get('label', ''))[:lab_chars].ljust(lab_chars)
        tail = f' {fmt_num(v)}{unit}' + (' OVER' if is_over else '')
        line = encode_text(label)[1] + bar.inline() + encode_text(tail)[1] + b'\n'
        if is_over:
            line = ESC + b'r\x01' + line + ESC + b'r\x00'
        out.append((ONE_WAY + line + TWO_WAY, f'{label}[{"#" * (filled // 10)}]{tail}'))
    return out


CHARTS = {'bar': chart_bar, 'line': chart_line, 'pie': chart_pie, 'hbar': chart_hbar}

# v1 shorthand used by the Meal Plan Generator page: ["kind", ...strings]
SHORTHAND = {
    'title': lambda a: {'type': 'text', 'text': a[0], 'size': 'both', 'red': True, 'align': 'center'},
    'sub': lambda a: {'type': 'text', 'text': a[0], 'align': 'center'},
    'rule': lambda a: {'type': 'rule'},
    'head': lambda a: {'type': 'text', 'text': a[0], 'red': True, 'bold': True},
    'cat': lambda a: {'type': 'text', 'text': a[0], 'bold': True},
    'meal': lambda a: {'type': 'labeled', 'label': a[0], 'text': a[1] if len(a) > 1 else ''},
    'item': lambda a: {'type': 'item', 'name': a[0], 'qty': a[1] if len(a) > 1 else ''},
    'text': lambda a: {'type': 'text', 'text': a[0] if a else ''},
    'feed': lambda a: {'type': 'feed'},
    'cut': lambda a: {'type': 'cut'},
}


def normalize(b):
    if isinstance(b, list) and b and b[0] in SHORTHAND:
        return SHORTHAND[b[0]](b[1:])
    if isinstance(b, dict) and 'type' in b:
        return b
    raise JobError(f'unknown block: {str(b)[:60]}')


def render_block(b, ctx):
    t = b['type']
    if t == 'text':
        return render_text(b, ctx)
    if t == 'labeled':
        return render_labeled(b, ctx)
    if t == 'item':
        return render_row(b.get('name', ''), b.get('qty', ''), ctx, box=b.get('box', True), red=bool(b.get('red')))
    if t == 'columns':
        return render_row(b.get('left', ''), b.get('right', ''), ctx, red=bool(b.get('red')), bold=bool(b.get('bold')))
    if t == 'rule':
        ch = clean(b.get('char', '-'))[:1] or '-'
        return [text_out(ch * ctx.cols)]
    if t == 'feed':
        return [(b'\n', '')] * max(1, min(int(b.get('lines', 1)), 20))
    if t == 'image':
        return render_image(b, ctx)
    if t == 'chart':
        fn = CHARTS.get(b.get('kind'))
        if not fn:
            raise JobError('chart kind must be bar, line, pie or hbar')
        return fn(b, ctx)
    if t == 'legend':
        ents = []
        for e in b.get('entries', []):
            key = line_key(int(e.get('dash', 0))) if e.get('line') else swatch(float(e.get('level', 0.5)))
            ents.append((key, e.get('label', '')))
        return legend_lines(ents)
    if t == 'nv_print':
        slot = int(b.get('slot', 1))
        if not 1 <= slot <= 255:
            raise JobError('nv_print slot must be 1-255')
        if ctx.upside:
            ctx.warnings.append('stored (NV) images on upside-down pages have not been tried on paper')
        return [(ALIGN.get(b.get('align', 'center'), p.CENTER) + p.nv_print(slot) + p.LEFT, f'[stored image {slot}]')]
    if t == 'inverse':
        raise JobError('the TM-U220 cannot print white-on-black text')
    raise JobError(f'unknown block type: {t}')


def build(job):
    """Job dict -> {'data': bytes, 'preview': str, 'receipts', 'lines', 'warnings'}."""
    if not isinstance(job, dict) or not isinstance(job.get('blocks'), list):
        raise JobError('job must be {"blocks": [...]}')
    ctx = Ctx(job)
    receipts, cur = [], []
    for raw in job['blocks']:
        b = normalize(raw)
        if b['type'] == 'cut':
            receipts.append(cur); cur = []
        else:
            cur.extend(render_block(b, ctx))
    if cur or not receipts:
        receipts.append(cur)
    data, prev = b'', []
    for r in receipts:
        blocks = [x for x, _ in r]
        body = p.upside_down(blocks) if ctx.upside else b''.join(blocks)
        data += p.INIT + body + (p.CUT if job.get('cut', True) else b'\n' * 4)
        prev.append('\n'.join(s for _, s in r))
    copies = max(1, min(int(job.get('copies', 1)), 5))
    sep = '\n' + '~' * 14 + ' cut ' + '~' * 14 + '\n'
    return {'data': data * copies, 'preview': sep.join(prev), 'receipts': len(receipts) * copies,
            'lines': sum(len(r) for r in receipts) * copies, 'warnings': ctx.warnings}


# ================================================================ stored (NV) images
PRINT_LOCK = threading.Lock()


def nv_canvas(img, max_width=200):
    w, h, gray = decode_image(base64.b64decode(img['data']))
    tw = min(int(img.get('width') or w), max_width)
    tw -= tw % 8                                       # FS q needs widths in multiples of 8
    if tw < 8:
        raise JobError('stored image must be at least 8 dots wide')
    th = max(8, round(h * (tw / w) / p.ASPECT))
    lv = resample(gray, w, h, tw, th)
    c = Canvas(tw, th)
    method = img.get('dither', 'threshold')
    for y in range(th):
        for x in range(tw):
            c.set(x, y, lv[y][x] > 0.5 if method == 'threshold' else p.dot_on(x, y, lv[y][x], method))
    return c


class NVBudget:
    """Counts NV writes per day in a state file. Epson: no more than 10 a day."""

    def __init__(self, path, limit):
        self.path, self.limit = Path(path), max(0, min(limit, NV_HARD_LIMIT))
        self.lock = threading.Lock()

    def _load(self):
        try:
            s = json.loads(self.path.read_text())
        except Exception:
            s = {}
        today = time.strftime('%Y-%m-%d')
        return s if s.get('date') == today else {'date': today, 'nv_writes': 0}

    def used(self):
        return self._load()['nv_writes']

    def spend(self):
        with self.lock:
            s = self._load()
            if s['nv_writes'] >= self.limit:
                raise JobError(f'NV write budget used up for today ({self.limit}); try tomorrow')
            s['nv_writes'] += 1
            self.path.write_text(json.dumps(s))
            return s['nv_writes']


# ================================================================ demo job (every feature)
def png_gray(rows):
    """Tiny PNG writer (8-bit grayscale) for the demo and tests."""
    h, w = len(rows), len(rows[0])
    chunk = lambda t, d: len(d).to_bytes(4, 'big') + t + d + zlib.crc32(t + d).to_bytes(4, 'big')
    raw = b''.join(b'\x00' + bytes(r) for r in rows)
    ihdr = w.to_bytes(4, 'big') + h.to_bytes(4, 'big') + bytes([8, 0, 0, 0, 0])
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def _sphere_png(n=96):
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            dx, dy = (x - n / 2) / (n * 0.45), (y - n / 2) / (n * 0.45)
            d2 = dx * dx + dy * dy
            lit = max(0.0, -0.5 * dx - 0.6 * dy + 0.62 * math.sqrt(1 - d2)) if d2 <= 1 else None
            row.append(255 if lit is None else int(255 * min(1, lit) ** 0.8))
        rows.append(row)
    return base64.b64encode(png_gray(rows)).decode()


def demo_job():
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug']
    return {'id': f'demo-{time.time()}', 'label': 'Feature demo', 'blocks': [
        {'type': 'text', 'text': 'RELAY DEMO', 'size': 'both', 'red': True, 'align': 'center'},
        {'type': 'text', 'text': time.strftime('%Y-%m-%d %H:%M'), 'align': 'center'},
        {'type': 'rule'},
        {'type': 'text', 'text': 'Font A: 35 per line', 'font': 'A'},
        {'type': 'text', 'text': 'Font B: 40 per line (default)'},
        {'type': 'text', 'text': 'Bold', 'bold': True}, {'type': 'text', 'text': 'Underline', 'underline': True},
        {'type': 'text', 'text': 'Double height', 'size': 'height'}, {'type': 'text', 'text': 'Double width', 'size': 'width'},
        {'type': 'text', 'text': 'Left'}, {'type': 'text', 'text': 'Centre', 'align': 'center'}, {'type': 'text', 'text': 'Right', 'align': 'right'},
        {'type': 'text', 'text': 'Red text (two-colour ribbon)', 'red': True},
        {'type': 'text', 'text': 'Accented: café crème naïve'},
        {'type': 'text', 'text': '中文打印测试 红烧肉 + 米饭'},
        {'type': 'columns', 'left': 'Butter croissant x3', 'right': '8.25'},
        {'type': 'item', 'name': 'Pork belly 五花肉', 'qty': '2 lb'},
        {'type': 'labeled', 'label': 'D', 'text': '番茄土豆炖牛腩 x2 + 蒜蓉上海青 + 米饭'},
        {'type': 'rule'},
        {'type': 'text', 'text': 'IMAGE (blue noise, fine)', 'bold': True},
        {'type': 'image', 'data': _sphere_png(), 'dither': 'blue', 'fine': True, 'width': 120},
        {'type': 'text', 'text': 'BAR CHART', 'bold': True},
        {'type': 'chart', 'kind': 'bar', 'labels': months,
         'series': [{'name': 'This year', 'values': [12, 19, 7, 23, 16, 28, 21, 31]}, {'name': 'Last year', 'values': [9, 14, 11, 18, 20, 22, 17, 24]}]},
        {'type': 'text', 'text': 'LINE CHART', 'bold': True},
        {'type': 'chart', 'kind': 'line', 'labels': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
         'series': [{'name': 'Visitors, hundreds', 'values': [8, 11, 15, 14, 22, 27, 31, 36, 30, 24, 16, 10]},
                    {'name': 'Rainy hours', 'values': [30, 28, 25, 20, 14, 9, 5, 6, 11, 17, 24, 29]}]},
        {'type': 'text', 'text': 'PIE CHART', 'bold': True},
        {'type': 'chart', 'kind': 'pie', 'slices': [{'name': 'Rent', 'value': 45}, {'name': 'Food', 'value': 25}, {'name': 'Travel', 'value': 20}, {'name': 'Other', 'value': 10}]},
        {'type': 'text', 'text': 'BUDGET USED', 'bold': True},
        {'type': 'chart', 'kind': 'hbar', 'max': 125, 'over': 100, 'unit': '%',
         'rows': [{'label': 'Rent', 'value': 92}, {'label': 'Food', 'value': 74}, {'label': 'Travel', 'value': 112}, {'label': 'Other', 'value': 38}]},
        {'type': 'rule'},
        {'type': 'text', 'text': 'End of demo', 'align': 'center'},
    ]}


TEST_JOB = {'blocks': [['title', 'PRINT RELAY'], ['sub', 'test page'], ['rule'], ['head', 'Red heading'],
                       ['text', 'Black text, 中文 红烧肉 + 米饭'], ['item', 'Pork belly 五花肉', '2 lb'],
                       ['item', 'Baby bok choy 上海青 (or 豆苗 1 bag)', '1½ lb']]}


# ================================================================ http
class Relay(BaseHTTPRequestHandler):
    server_version = 'PrintRelay/' + VERSION
    seen = OrderedDict()
    seen_lock = threading.Lock()

    def log_message(self, fmt, *args):
        msg = re.sub(r'([?&]j=)[^ &]{24,}', lambda m: m.group(1) + '...', fmt % args)
        print('%s  %s' % (time.strftime('%H:%M:%S'), msg), flush=True)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def page(self, code, title, body):
        doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;color:#1a211d;background:#f2f5f2}}
h1{{font-size:1.4rem}} .ok{{color:#2d7a43}} .bad{{color:#a5402e}} code{{font-size:.9em}} a{{color:#0d6a58}}</style></head><body>{body}</body></html>'''
        self._send(code, doc.encode('utf-8'), 'text/html; charset=utf-8')

    @property
    def cfg(self):
        return self.server.cfg

    def authorized(self, q):
        tok = self.cfg.token
        if not tok:
            return True
        auth = self.headers.get('Authorization', '')
        got = auth[7:].strip() if auth.startswith('Bearer ') else ''
        return got == tok or q.get('t', [''])[0] == tok

    def body_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            raise JobError(f'request too large (max {MAX_BODY // 1024 // 1024} MB)')
        try:
            return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
            raise JobError('body must be JSON')

    def deliver(self, data, preview, wait=2.0):
        if self.cfg.dry_run:
            d = Path(self.cfg.dry_run); d.mkdir(parents=True, exist_ok=True)
            stem = d / (time.strftime('job-%Y%m%d-%H%M%S-') + f'{int(time.time() * 1000) % 1000:03d}')
            stem.with_suffix('.bin').write_bytes(data)
            stem.with_suffix('.txt').write_text(preview, encoding='utf-8')
            return f'saved to {stem}.bin (dry run)'
        with PRINT_LOCK:                              # one job on the wire at a time
            p.send(self.cfg.printer, data, wait=wait)
        return f'sent to {self.cfg.printer}'

    def run_job(self, job, force=False):
        """-> (status, result). Shared by GET /print, POST /print, /test, /demo. Dedupes by job id."""
        jid = str(job.get('id', '')) if isinstance(job, dict) else ''
        with self.seen_lock:
            if jid and jid in self.seen and not force:
                return 'duplicate', {'ok': True, 'duplicate': True, 'printed_at': time.strftime('%H:%M', time.localtime(self.seen[jid]))}
        out = build(job)
        where = self.deliver(out['data'], out['preview'])
        if jid:
            with self.seen_lock:
                self.seen[jid] = time.time()
                while len(self.seen) > 500:
                    self.seen.popitem(last=False)
        return 'ok', {'ok': True, 'receipts': out['receipts'], 'lines': out['lines'], 'bytes': len(out['data']),
                      'where': where, 'warnings': out['warnings']}

    def html_result(self, label, res):
        warn = ''.join(f'<p class="bad">Note: {html.escape(w)}</p>' for w in res.get('warnings', []))
        return (f'<h1 class="ok">Printing</h1><p>{html.escape(label)}: {res["receipts"]} receipt{"s" if res["receipts"] > 1 else ""}, '
                f'{res["lines"]} lines, {res["bytes"] / 1024:.1f} KB, {html.escape(res["where"])}.</p>{warn}<p>You can close this tab.</p>')

    def printer_error(self, e, as_json=False):
        msg = f'Could not reach the printer at {self.cfg.printer}:9100 ({e}). Check that it is on, has paper, and still has that address.'
        if as_json:
            return self.json(502, {'ok': False, 'error': msg})
        self.page(502, 'Printer not reachable', f'<h1 class="bad">Could not reach the printer</h1><p>{html.escape(msg)}</p><p>Then reload this tab.</p>')

    # ---- GET: browser-facing (the page can only open links)
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == '/':
                mode = f'dry run into <code>{html.escape(self.cfg.dry_run)}</code>' if self.cfg.dry_run else f'printer <code>{self.cfg.printer}:9100</code>'
                return self.page(200, 'Print relay', f'<h1>Print relay {VERSION} is running</h1><p>Sending to {mode}.</p>'
                                 f'<p>In the Meal Plan Generator, set the relay address to <code>{html.escape(self.headers.get("Host", "this-machine:8765"))}</code>.</p>'
                                 '<p><a href="/test">Print a test receipt</a> (short) &middot; <a href="/demo">Print the feature demo</a> (about 40 cm) &middot; <a href="/id">Printer model</a></p>')
            if u.path == '/health':
                return self.json(200, {'ok': True, 'version': VERSION, 'printer': self.cfg.printer, 'dry_run': bool(self.cfg.dry_run),
                                       'nv_writes_today': self.server.nv.used(), 'nv_daily_limit': self.server.nv.limit})
            if u.path == '/id':
                return self.json(200, {'dry_run': True} if self.cfg.dry_run else p.printer_id(self.cfg.printer))
            if u.path in ('/test', '/demo'):
                job = TEST_JOB if u.path == '/test' else demo_job()
                _, res = self.run_job(job, force=True)
                return self.page(200, 'Printing', self.html_result('Test receipt' if u.path == '/test' else 'Feature demo', res))
            if u.path == '/print':                    # page contract: GET /print?j=<base64url JSON job>
                try:
                    raw = q['j'][0]
                    job = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8'))
                except Exception:
                    return self.page(400, 'Bad print job', '<h1 class="bad">That print link is broken</h1><p>Press Print again on the page.</p>')
                status, res = self.run_job(job, force='again' in q)
                if status == 'duplicate':
                    return self.page(200, 'Already printed', f'<h1>Already printed</h1><p>This job printed at {res["printed_at"]}. '
                                     f'Reloading the tab does not print it twice.</p><p><a href="{html.escape(self.path)}&amp;again=1">Print it again</a></p>')
                return self.page(200, 'Printed', self.html_result(str(job.get('label', 'Print job')), res))
            return self.page(404, 'Not found', '<h1>Not found</h1><p>See <a href="/">the status page</a>.</p>')
        except JobError as e:
            return self.page(400, 'Bad print job', f'<h1 class="bad">Can\'t print that</h1><p>{html.escape(str(e))}</p>')
        except OSError as e:
            return self.printer_error(e)

    # ---- POST: scripts and agents (JSON in, JSON out)
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self.authorized(q):
            return self.json(401, {'ok': False, 'error': 'missing or wrong token'})
        try:
            if u.path == '/print':
                job = self.body_json()
                _, res = self.run_job(job, force=bool(job.get('force')))
                return self.json(200, res)
            if u.path == '/render':                   # lay out without printing: ?format=bin|text
                out = build(self.body_json())
                if q.get('format', ['bin'])[0] == 'text':
                    return self._send(200, out['preview'].encode('utf-8'), 'text/plain; charset=utf-8')
                return self._send(200, out['data'], 'application/octet-stream')
            if u.path == '/cancel':                   # real-time clear on its own connection (bypasses the lock)
                if not self.cfg.dry_run:
                    with socket.create_connection((self.cfg.printer, 9100), timeout=5) as s:
                        s.sendall(CLEAR_BUFFER)
                        time.sleep(0.5)
                return self.json(200, {'ok': True, 'note': 'lines already in the network card still print, and the final cut runs'})
            if u.path == '/nv/store':
                body = self.body_json()
                if body.get('confirm') != 'replace all stored images':
                    raise JobError('NV writes wear the printer out and replace every stored image. '
                                   'Send "confirm": "replace all stored images" to go ahead.')
                imgs = body.get('images') or []
                if not imgs:
                    raise JobError('images: [{"data": <base64 PNG/PGM/PBM>, "width": dots}] required')
                canv = [nv_canvas(i) for i in imgs]
                n = self.server.nv.spend()
                where = self.deliver(p.nv_store(*canv), f'[NV store: {len(canv)} images]', wait=15)
                return self.json(200, {'ok': True, 'slots': len(canv), 'where': where,
                                       'nv_writes_today': n, 'nv_daily_limit': self.server.nv.limit})
            if u.path == '/nv/print':
                body = self.body_json()
                _, res = self.run_job({'blocks': [{'type': 'nv_print', 'slot': int(body.get('slot', 1))}]}, force=True)
                return self.json(200, res)
            return self.json(404, {'ok': False, 'error': 'not found'})
        except JobError as e:
            return self.json(400, {'ok': False, 'error': str(e)})
        except OSError as e:
            return self.printer_error(e, as_json=True)


# ================================================================ selftest + main
def selftest():
    p.selftest()
    for upside in (False, True):
        for paper in PAPER:
            job = demo_job(); job['upside_down'] = upside; job['paper'] = paper
            data = build(job)['data']
            assert p.GS + b'v0' not in data and p.GS + b'!' not in data, 'TM-U220 ignores GS v 0 and GS !'
            assert data.count(p.CUT) == 1
    page_job = {'blocks': [['title', 'MEAL PLAN'], ['sub', 'Week of Mon, Nov 9'], ['rule'], ['head', 'Mon 11/9'],
                           ['meal', 'L', '红烧牛肉面 x3 + 蒜蓉上海青 + Baked or sautéed asparagus'], ['cut'],
                           ['item', 'Baby bok choy 上海青 (or 豆苗 1 bag)', '1½ lb'], ['feed'], ['text', 'x2 = makes 2 meals']]}
    out = build(page_job)
    assert out['receipts'] == 2 and out['data'].count(p.CUT) == 2
    out['data'].decode('gb18030')
    assert ESC + b'r\x01' in out['data'] and ONE_WAY in out['data']
    for t in ('Accented: café crème', '中文 红烧肉 mixed with café', 'x' * 90):
        for ln in wrap(clean(t), 200):
            assert width(ln) <= 200, ln
    assert encode_text(clean('café'))[0].startswith(FS + b'.')
    try:
        build({'blocks': [{'type': 'inverse'}]})
        raise AssertionError('inverse must be refused')
    except JobError:
        pass
    print(f'relay selftest OK (demo {len(build(demo_job())["data"])} bytes; upside-down and all paper widths)')


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description='HTTP print relay for the Epson TM-U220')
    ap.add_argument('--printer', default=env('PRINTER_IP'), help='printer IP (port 9100), required unless --dry-run')
    ap.add_argument('--port', type=int, default=int(env('RELAY_PORT', '8765')))
    ap.add_argument('--bind', default=env('RELAY_BIND', '0.0.0.0'))
    ap.add_argument('--dry-run', metavar='DIR', default=env('RELAY_DRY_RUN'), help='save jobs to DIR instead of printing')
    ap.add_argument('--token', default=env('RELAY_TOKEN'), help='require this token on POST endpoints')
    ap.add_argument('--state', default=env('RELAY_STATE', 'relay_state.json'), help='file that counts NV writes per day')
    ap.add_argument('--nv-daily-limit', type=int, default=int(env('RELAY_NV_DAILY_LIMIT', '3')), help='max NV writes a day (hard cap 10)')
    ap.add_argument('--selftest', action='store_true', help='render every feature and exit')
    cfg = ap.parse_args()
    if cfg.selftest:
        return selftest()
    if not cfg.printer and not cfg.dry_run:
        ap.error('set the printer address: --printer <ip> or PRINTER_IP')
    srv = ThreadingHTTPServer((cfg.bind, cfg.port), Relay)
    srv.cfg, srv.nv = cfg, NVBudget(cfg.state, cfg.nv_daily_limit)
    target = f'dry run into {cfg.dry_run}' if cfg.dry_run else f'printer {cfg.printer}:9100'
    print(f'Print relay {VERSION} on http://{cfg.bind}:{cfg.port}/  ->  {target}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
