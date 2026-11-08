"""ESC/POS helpers for the Epson TM-U220 (9-pin impact, 76 mm) over raw TCP 9100.
Python 3 standard library only. Import it, or run:

    python tmu220.py selftest             checks the dithering and command bytes, no printer
    python tmu220.py id <ip>              asks the printer for maker, model, firmware, font
    python tmu220.py demo <ip>            prints a sample receipt (text, image, chart, red)
    python tmu220.py demo --out file.bin  writes the sample receipt to a file instead
"""
import math, random, socket, sys, time

WIDTH = 200    # dots per line on 76 mm paper (69.5 mm: 180, 57.5 mm: 150)
ASPECT = 1.11  # one dot row is ~11% taller than one dot column is wide: scale y by this for circles
ASPECT_FINE = ASPECT / 2  # rows in bit_image(fine=True) are half as far apart
ESC, GS, FS = b'\x1b', b'\x1d', b'\x1c'
INIT = ESC + b'@'
CUT = b'\n' * 4 + GS + b'VB\x00'  # feed to the cutter and cut (autocutter models)
LEFT, CENTER, RIGHT = ESC + b'a\x00', ESC + b'a\x01', ESC + b'a\x02'
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


# ---------------------------------------------------------------- text

def text(s):
    return s.encode('ascii', 'replace') + b'\n'


def size(s, double_height=False, double_width=False):
    """ESC ! (the TM-U220 ignores GS !). Bit 0 keeps Font B, the default (40 per line).
    Double height prints in two passes: one direction (ESC U 1) keeps them aligned."""
    n = 0x01 | (0x10 if double_height else 0) | (0x20 if double_width else 0)
    out = ESC + b'!' + bytes([n]) + text(s) + ESC + b'!\x01'
    return ESC + b'U\x01' + out + ESC + b'U\x00' if double_height else out


def bold(s):
    return ESC + b'E\x01' + text(s) + ESC + b'E\x00'


def underline(s):
    return ESC + b'-\x01' + text(s) + ESC + b'-\x00'


def red(data):
    """Second ribbon colour; needs a two-colour ERC-38(B/R) ribbon."""
    return ESC + b'r\x01' + data + ESC + b'r\x00'


def upside_down(blocks):
    """Rotates a whole page 180 degrees, for a printer mounted so its output reads upside down.
    blocks: a list in reading order, one line each, and each must restore any alignment or
    colour it sets, because they print last-first. Images go in as Canvas.flipped_image()."""
    return ESC + b'{\x01' + b''.join(reversed(blocks)) + ESC + b'{\x00'


# ---------------------------------------------------------------- dithering

def _blue_noise(n=32, sigma=1.5, seed=1):
    """Void-and-cluster threshold matrix (Ulichney): rank[y*n+x], a permutation of 0..n*n-1."""
    k = [[math.exp(-(min(dx, n - dx) ** 2 + min(dy, n - dy) ** 2) / (2 * sigma * sigma))
          for dx in range(n)] for dy in range(n)]
    on, energy = [False] * (n * n), [0.0] * (n * n)

    def put(i, sign):
        on[i] = sign > 0
        py, px = divmod(i, n)
        for y in range(n):
            row, base = k[(y - py) % n], y * n
            for x in range(n):
                energy[base + x] += sign * row[(x - px) % n]

    cluster = lambda: max((i for i in range(n * n) if on[i]), key=energy.__getitem__)
    void = lambda: min((i for i in range(n * n) if not on[i]), key=energy.__getitem__)
    for i in random.Random(seed).sample(range(n * n), n * n // 10):
        put(i, 1)
    for _ in range(2000):  # even out the start: tightest cluster -> largest void
        c = cluster(); put(c, -1)
        v = void(); put(v, 1)
        if v == c:
            break
    start, ones, rank = (on[:], energy[:]), sum(on), [0] * (n * n)
    for r in range(ones - 1, -1, -1):
        c = cluster(); put(c, -1); rank[c] = r
    on[:], energy[:] = start[0][:], start[1][:]
    for r in range(ones, n * n):
        v = void(); put(v, 1); rank[v] = r
    return n, rank


_BLUE = None


def dot_on(x, y, level, method='grid'):
    """level 0 (white) .. 1 (black). 'grid' (4x4 Bayer) for charts and flat fills,
    'blue' (blue noise) for photos and complex shapes."""
    global _BLUE
    if method == 'grid':
        return level * 16 > BAYER[y % 4][x % 4] + 0.5
    if _BLUE is None:
        _BLUE = _blue_noise()
    n, rank = _BLUE
    return level * n * n > rank[(y % n) * n + x % n] + 0.5


def _fine_tone(level):
    """Fine mode hits each spot with ~2 rows of dots, so darkness ~ 1-(1-coverage)^2.
    Exponent 1.5 picked from printed ramps (2.0 looked worse; 1.0 not tried with this correction)."""
    return 1 - math.sqrt(1 - level ** 1.5)


# ---------------------------------------------------------------- images

class Canvas:
    """1-bit image, printed with ESC * m=0 in 8-dot bands. Height is rounded up to a multiple of 8."""

    def __init__(self, w=WIDTH, h=8):
        assert 0 < w <= WIDTH, f'at most {WIDTH} dots wide'
        self.w, self.h = w, -(-h // 8) * 8
        self.px = bytearray(self.w * self.h)

    def set(self, x, y, on=True):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y * self.w + x] = on

    def fill(self, level, method='grid', fine=False):
        """level(x, y) -> 0..1 for every dot. fine=True if this canvas prints with bit_image(fine=True)."""
        for y in range(self.h):
            for x in range(self.w):
                lv = min(1.0, max(0.0, level(x, y)))
                self.set(x, y, dot_on(x, y, _fine_tone(lv) if fine else lv, method))

    def regions(self, region, levels, method='grid'):
        """region(x, y) -> index into levels, or -1 for blank. Shaded fills with a 1-dot
        outline on every border, so pale areas keep their edges."""
        for y in range(self.h):
            for x in range(self.w):
                r = region(x, y)
                if r < 0:
                    continue
                edge = any(region(x + dx, y + dy) != r for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                self.set(x, y, edge or dot_on(x, y, levels[r], method))

    def line(self, x0, y0, x1, y1, dash=0):
        n = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(n + 1):
            if not dash or (i // dash) % 2 == 0:
                self.set(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n))

    def _band(self, y0):
        data = bytearray()
        for x in range(self.w):
            b = 0
            for bit in range(8):
                if self.px[(y0 + bit) * self.w + x]:
                    b |= 0x80 >> bit  # top dot = MSB
            data.append(b)
        return ESC + b'*\x00' + bytes([self.w & 255, self.w >> 8]) + bytes(data)

    def bit_image(self, fine=False):
        """Whole image, on its own lines, printed in one direction (ESC U 1) so bands line up.
        fine=False: one row per dot pitch (2/144"); bands of 8 rows, ESC 3 16 so they touch.
        fine=True: rows are 1/144" apart (twice as many rows for the same height; use
        ASPECT_FINE). Each 16-row band prints in two passes, the second half a dot lower:
        smoother curves and edges, at half the speed. Shade with fill(level, 'blue', fine=True);
        grid shading looked bad in fine mode at every tone tried."""
        out = ESC + b'U\x01'
        if not fine:
            out += ESC + b'3\x10'
            for y0 in range(0, self.h, 8):
                out += self._band(y0) + b'\n'
            return out + ESC + b'2' + ESC + b'U\x00'
        for y0 in range(0, self.h, 16):
            for half, feed in ((0, b'\x01'), (1, b'\x0f')):  # 1 + 15 = 16/144", one band
                band = Canvas(self.w, 8)
                for r in range(8):
                    src = y0 + 2 * r + half
                    if src < self.h:
                        band.px[r * self.w:(r + 1) * self.w] = self.px[src * self.w:(src + 1) * self.w]
                out += band._band(0) + ESC + b'J' + feed  # ESC J: print, feed n/144"
        return out + ESC + b'U\x00'

    def flipped_image(self, fine=False):
        """bit_image() rotated 180 degrees, as one block for upside_down(). ESC { is off during
        the image because the pixels are already rotated, so no bands or passes need reordering."""
        # shortcut: only full-width canvases were printed; a narrower one lands on the reader's right, pad it to WIDTH.
        c = Canvas(self.w, self.h)
        c.px = self.px[::-1]
        return ESC + b'{\x00' + c.bit_image(fine) + ESC + b'{\x01'

    def inline(self):
        """An 8-dot-high canvas as one band without a line feed, to put inside a text line
        (legend swatches, bars with labels). Wrap the lines in ESC U 1 / ESC U 0."""
        assert self.h == 8, 'inline images are one band (8 dots) high'
        return self._band(0)


def nv_store(*images):
    """FS q. WRITES NV MEMORY: at most 10 writes a day, and it replaces everything stored, so
    pass all images at once. Send it as its own job, wait=15 (shorter is untested). Widths must
    be multiples of 8. Images are numbered from 1 in the order given."""
    out = FS + b'q' + bytes([len(images)])
    for c in images:
        assert c.w % 8 == 0, 'NV image width must be a multiple of 8'
        rows = c.h // 8
        out += bytes([c.w // 8, 0, rows & 255, rows >> 8])
        bands = [c._band(y0)[5:] for y0 in range(0, c.h, 8)]  # column order: top byte first
        out += bytes(band[x] for x in range(c.w) for band in bands)
    return out


def nv_print(n=1):
    """FS p at m=1. m=0 prints half as wide, probably because stored columns are half-dot positions."""
    return FS + b'p' + bytes([n, 1]) + b'\n'


# ---------------------------------------------------------------- printer I/O

def send(ip, data, port=9100, wait=2.0):
    """Retries the connection: the printer doesn't answer for a few seconds after power-on."""
    for attempt in range(3):
        try:
            s = socket.create_connection((ip, port), timeout=5)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(3)
    with s:
        s.settimeout(300)  # sendall's timeout covers the whole job, and the printer takes data as it prints
        s.sendall(data)
        time.sleep(wait)  # let the printer take everything before the socket closes


def printer_id(ip, port=9100):
    """GS I n: tells a TM-U220 apart from other printers that listen on 9100."""
    out = {}
    with socket.create_connection((ip, port), timeout=5) as s:
        for n, name in ((0x42, 'maker'), (0x43, 'model'), (0x41, 'firmware'), (0x45, 'font')):
            s.sendall(GS + b'I' + bytes([n]))
            s.settimeout(1.0)
            try:
                out[name] = s.recv(256).strip(b'_\x00 ').decode('ascii', 'replace')
            except socket.timeout:
                out[name] = None
    return out


# ---------------------------------------------------------------- demo and self-check

def demo():
    out = INIT + CENTER + size('PRINT TEST', True, True) + text(time.strftime('%Y-%m-%d %H:%M')) + LEFT
    out += bold('Bold') + underline('Underline') + size('Double height', double_height=True)
    out += red(text('Red (two-colour ribbon)')) + text('-' * 40)

    sphere = Canvas(WIDTH, 192)  # photo-like shading: blue noise, fine mode
    def lit(x, y):
        dx, dy = (x - 100) / 42, (y - 96) * ASPECT_FINE / 42
        d2 = dx * dx + dy * dy
        return 1 - max(0.0, -0.5 * dx - 0.6 * dy + 0.62 * math.sqrt(1 - d2)) ** 1.5 if d2 <= 1 else 0.0
    sphere.fill(lit, 'blue', fine=True)
    out += bold('IMAGE (blue noise, fine)') + sphere.bit_image(fine=True) + text('-' * 40)

    values = [12, 19, 7, 23, 16, 28, 21, 31]  # chart: grid fills with outlines
    chart = Canvas(WIDTH, 80)
    def bar(x, y):
        i = x // 25
        return 0 if 4 <= x % 25 <= 20 and y >= 79 - round(values[i] * 2.4) else -1
    chart.regions(bar, [0.45], 'grid')
    chart.line(0, 79, WIDTH - 1, 79)
    out += bold('BAR CHART (grid)') + text(''.join(f'{v:^5}' for v in values)) + chart.bit_image()
    out += text(''.join(f'{m:^5}' for m in ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug')))
    return out + CUT


def selftest():
    c = Canvas(WIDTH, 64)
    for method in ('grid', 'blue'):
        for lv in (0.1, 0.3, 0.5, 0.8):
            c.fill(lambda x, y: lv, method)
            got = sum(c.px) / len(c.px)
            assert abs(got - lv) < (1 / 16 if method == 'grid' else 0.01), (method, lv, got)  # grid: 16 steps
    assert sorted(_BLUE[1]) == list(range(_BLUE[0] ** 2)), 'blue noise is not a permutation'
    img = Canvas(WIDTH, 20).bit_image()  # 20 rows -> 3 bands
    assert img.startswith(ESC + b'U\x01' + ESC + b'3\x10') and img.endswith(ESC + b'2' + ESC + b'U\x00')
    assert img.count(ESC + b'*\x00' + bytes([WIDTH, 0])) == 3
    assert len(img) == 6 + 3 * (5 + WIDTH + 1) + 5
    assert len(Canvas(30, 8).inline()) == 5 + 30
    fine = Canvas(WIDTH, 32).bit_image(fine=True)  # 2 bands x 2 passes
    assert fine.count(ESC + b'J\x01') == 2 and fine.count(ESC + b'J\x0f') == 2
    assert fine.count(ESC + b'*\x00') == 4 and b'\n' not in fine
    assert _fine_tone(0.0) == 0.0 and _fine_tone(1.0) == 1.0
    c.fill(lambda x, y: 0.5, 'blue', fine=True)  # 1 - sqrt(1 - 0.5 ** 1.5) = 0.196
    assert abs(sum(c.px) / len(c.px) - 0.196) < 0.01, 'fine tone'
    nv = Canvas(8, 16)
    nv.set(0, 0); nv.set(1, 8)
    assert nv_store(nv) == FS + b'q\x01\x01\x00\x02\x00' + b'\x80\x00\x00\x80' + bytes(12)
    assert nv_print(2) == FS + b'p\x02\x01\n'
    assert size('x', True, True).startswith(ESC + b'U\x01' + ESC + b'!\x31')
    assert size('x', double_width=True).startswith(ESC + b'!\x21')
    assert upside_down([b'first', b'last']) == ESC + b'{\x01lastfirst' + ESC + b'{\x00'
    corner = Canvas(8, 8)
    corner.set(0, 0)
    flip = corner.flipped_image()
    assert flip.startswith(ESC + b'{\x00') and flip.endswith(ESC + b'{\x01')
    assert bytes(7) + b'\x01' in flip and corner.px[0] == 1, 'top-left dot must move to bottom-right'
    data = demo()
    assert GS + b'v0' not in data and GS + b'!' not in data, 'TM-U220 ignores GS v 0 and GS !'
    print(f'selftest OK (demo receipt {len(data)} bytes)')


if __name__ == '__main__':
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ''), sys.argv[2:]
    if cmd == 'selftest':
        selftest()
    elif cmd == 'id' and args:
        print(printer_id(args[0]))
    elif cmd == 'demo' and args[:1] == ['--out'] and len(args) == 2:
        open(args[1], 'wb').write(demo())
        print(f'wrote {args[1]}')
    elif cmd == 'demo' and args:
        send(args[0], demo())
        print('sent')
    else:
        sys.exit(__doc__)
