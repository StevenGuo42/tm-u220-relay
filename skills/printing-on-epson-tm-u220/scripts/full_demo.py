"""Full demo for the TM-U220: every working feature, as examples to copy. All data is made up.

    python full_demo.py selftest         checks the chart maths, no printer
    python full_demo.py <ip>             prints it (roughly 40 cm of paper)
    python full_demo.py --out file.bin   writes it to a file instead
"""
import math, sys, time
import tmu220 as p
from tmu220 import ASPECT, ASPECT_FINE, ESC, FS, WIDTH, Canvas, bold, text

ONE_WAY, TWO_WAY = ESC + b'U\x01', ESC + b'U\x00'
COLS = 40  # Font B characters per line
PIE = (('Rent', 0.45, 0.85), ('Food', 0.25, 0.5), ('Travel', 0.2, 0.25), ('Other', 0.1, 0.08))
THIS_YEAR = (12, 19, 7, 23, 16, 28, 21, 31)
LAST_YEAR = (9, 14, 11, 18, 20, 22, 17, 24)
VISITORS = (8, 11, 15, 14, 22, 27, 31, 36, 30, 24, 16, 10)
RAINFALL = (30, 28, 25, 20, 14, 9, 5, 6, 11, 17, 24, 29)


def heading(s):
    return text('-' * COLS) + p.CENTER + bold(s) + p.LEFT


def row(left, right):
    assert len(left) + len(right) <= COLS, (left, right)
    return text(f'{left:<{COLS - len(right)}}{right}')


# ---------------------------------------------------------------- text

def styles():
    out = heading('TEXT')
    out += ESC + b'M\x00' + text('Font A: 35 per line') + text('1234567890' * 3 + '12345') + ESC + b'M\x01'
    out += text('Font B: 40 per line') + text('1234567890' * 4)
    out += bold('Bold') + p.underline('Underline')
    out += p.size('Double height', double_height=True)
    out += p.size('Double width', double_width=True)
    out += p.size('Both', True, True)
    out += text('Left') + p.CENTER + text('Centre') + p.RIGHT + text('Right') + p.LEFT
    out += p.red(text('Red text (two-colour ribbon)'))
    out += FS + b'.' + ESC + b't\x00' + 'Accented: caf\xe9 cr\xe8me na\xefve\n'.encode('cp437') + FS + b'&'  # FS .: single byte
    # shortcut: GB18030 models only, other Chinese models need their own encoding
    out += ONE_WAY + '中文打印测试 GB18030\n'.encode('gb18030') + TWO_WAY  # 16-dot font: two passes
    return out


def sale():
    items = (('Espresso', 2, 3.50), ('Cappuccino', 1, 4.25), ('Butter croissant', 3, 2.75))
    out = heading('RECEIPT') + row('Item', 'Qty   Price')
    for name, qty, price in items:
        out += row(name, f'{qty:>3} {qty * price:>7.2f}')
    total = sum(qty * price for _, qty, price in items)
    out += row('', '-' * 11)
    out += p.size(f'{"TOTAL":<10}{total:>10.2f}', double_height=True, double_width=True)  # 20 per line
    return out + p.red(text('PAID BY CARD')) + p.CENTER + text('Thank you!') + p.LEFT


# ---------------------------------------------------------------- shading

def ramp(method, fine=False):
    rows, tick = (48, 6) if fine else (24, 3)
    c = Canvas(WIDTH, rows)
    c.fill(lambda x, y: x / (WIDTH - 1), method, fine)
    for y in range(tick):
        for x in range(WIDTH):
            c.set(x, y, x in (0, 1, 50, 100, 150, 198, 199))
    return c


def curves(fine):
    rows, step = (96, 0.5) if fine else (48, 1)
    c = Canvas(WIDTH, rows)
    for r in range(rows):
        y = r * step
        for x in range(WIDTH):
            ring = abs(math.hypot(x - 50, (y - 24) * ASPECT) - 20) < 1
            wave = x >= 100 and abs(y - 24 - 16 * math.sin((x - 100) / 9)) < 1.2
            c.set(x, r, ring or wave)
    return c


def mandelbrot():
    c = Canvas(WIDTH, 288)
    def level(x, y):
        cr, ci = -2.2 + x * 0.015, (y - 144) * ASPECT_FINE * 0.015
        zr = zi = 0.0
        for n in range(48):
            zr, zi = zr * zr - zi * zi + cr, 2 * zr * zi + ci
            if zr * zr + zi * zi > 4:
                return n / 24  # fill() clamps: darkest near the edge of the set
        return 1.0
    c.fill(level, 'blue', fine=True)
    return c


def shading():
    out = heading('SHADING') + text('0%        25%       50%       75%   100%')
    out += text('Grid: charts, flat fills') + ramp('grid').bit_image()
    out += text('Blue noise: photos, gradients') + ramp('blue').bit_image()
    out += text('Blue noise, fine mode') + ramp('blue', True).bit_image(fine=True)
    out += heading('RESOLUTION')
    out += text('Normal: 8 rows per pass') + curves(False).bit_image()
    out += text('Fine: 2 passes, half a dot apart') + curves(True).bit_image(fine=True)
    out += heading('PICTURE (blue noise, fine)') + mandelbrot().bit_image(fine=True)
    return out


# ---------------------------------------------------------------- charts

def swatch(level):
    c = Canvas(20, 8)
    c.regions(lambda x, y: 0 if 0 <= x < 20 and 1 <= y <= 6 else -1, [level])
    return c


def line_key(dash=0):
    c = Canvas(20, 8)
    c.line(0, 3, 19, 3, dash)
    return c


def legend(entries):
    """(canvas, label) rows: identity is never only in a texture."""
    return ONE_WAY + b''.join(key.inline() + text(' ' + label) for key, label in entries) + TWO_WAY


def grouped_bars():
    c = Canvas(WIDTH, 80)
    for units in (10, 20, 30):
        c.line(0, 79 - 2 * units, WIDTH - 1, 79 - 2 * units, dash=2)
    def region(x, y):
        if not (0 <= x < WIDTH and y < 80):
            return -1
        i, col = x // 25, x % 25
        s = 0 if 3 <= col <= 11 else 1 if 13 <= col <= 21 else -1
        return s if s >= 0 and y >= 79 - 2 * (THIS_YEAR, LAST_YEAR)[s][i] else -1
    c.regions(region, [0.7, 0.2])
    c.line(0, 79, WIDTH - 1, 79)
    return c


def line_chart():
    """Fine mode: 2 rows per dot, so diagonals step by half a dot."""
    c = Canvas(WIDTH, 160)
    for units in (10, 20, 30):
        c.line(0, 159 - 4 * units, WIDTH - 1, 159 - 4 * units, dash=3)
    c.line(0, 159, WIDTH - 1, 159)
    c.line(0, 0, 0, 159)
    for values, dash in ((VISITORS, 0), (RAINFALL, 6)):
        for i in range(len(values) - 1):
            c.line(i * 18, 159 - 4 * values[i], (i + 1) * 18, 159 - 4 * values[i + 1], dash)
    return c


def pie_region(x, y):
    dx, dy = x - 100, (y - 44) * ASPECT
    if dx * dx + dy * dy > 44 * 44:
        return -1
    angle = (math.atan2(dx, -dy) / (2 * math.pi)) % 1  # clockwise from 12 o'clock
    upto = 0.0
    for i, (_, share, _) in enumerate(PIE):
        upto += share
        if angle < upto:
            return i
    return len(PIE) - 1  # rounding


def pie():
    c = Canvas(WIDTH, 88)
    c.regions(pie_region, [level for _, _, level in PIE])
    return c


def budget_bars():
    """Bars inside text lines: label, 90-dot bar for 0..125%, value. One line in red."""
    out = b''
    for name, used in (('Rent', 92), ('Food', 74), ('Travel', 112), ('Other', 38)):
        bar = Canvas(90, 8)
        filled = round(90 * used / 125)
        bar.regions(lambda x, y: 0 if 0 <= x < filled and 1 <= y <= 6 else -1, [0.5])
        line = f'{name:<9}'.encode() + bar.inline() + text(f' {used}%' + (' OVER' if used > 100 else ''))
        out += p.red(line) if used > 100 else line
    return ONE_WAY + out + TWO_WAY


def charts():
    out = heading('BAR CHART') + text('Sales per month, lines every 10')
    out += legend(((swatch(0.7), 'This year'), (swatch(0.2), 'Last year')))
    out += grouped_bars().bit_image()
    out += text(''.join(f'{m:^5}' for m in ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug')))
    out += heading('LINE CHART') + text('Per month, lines every 10')
    out += legend(((line_key(), 'Visitors, hundreds'), (line_key(4), 'Rainy hours')))
    out += line_chart().bit_image(fine=True)
    out += text(''.join(f'{m:<11}' for m in ('Jan', 'Apr', 'Jul', 'Oct')))
    out += heading('PIE CHART') + pie().bit_image()
    out += legend([(swatch(level), f'{name} {share:.0%}') for name, share, level in PIE])
    out += heading('BUDGET USED') + budget_bars()
    return out


# ---------------------------------------------------------------- whole receipt

def receipt():
    out = p.INIT + p.CENTER + p.size('FULL DEMO', True, True) + text(time.strftime('%Y-%m-%d %H:%M')) + p.LEFT
    out += styles() + sale() + shading() + charts()
    return out + text('-' * COLS) + p.CENTER + text('End of full demo') + p.LEFT + p.CUT


def selftest():
    area = [0] * len(PIE)
    for y in range(88):
        for x in range(WIDTH):
            r = pie_region(x, y)
            if r >= 0:
                area[r] += 1
    for got, (name, share, _) in zip(area, PIE):
        assert abs(got / sum(area) - share) < 0.02, (name, got / sum(area))
    assert sum(share for _, share, _ in PIE) == 1.0
    assert max(max(THIS_YEAR), max(LAST_YEAR)) * 2 < 80 and max(max(VISITORS), max(RAINFALL)) * 4 < 160
    assert grouped_bars().h == 80 and sum(line_chart().px) > 0
    print(f'selftest OK (full demo {len(receipt())} bytes)')


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == 'selftest':
        selftest()
    elif arg == '--out' and len(sys.argv) == 3:
        open(sys.argv[2], 'wb').write(receipt())
        print(f'wrote {sys.argv[2]}')
    elif arg and not arg.startswith('-'):
        p.send(arg, receipt())
        print('sent')
    else:
        sys.exit(__doc__)
