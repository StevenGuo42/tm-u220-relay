"""Tests for print_relay.py, no printer needed:  python3 test_relay.py
Covers rendering of every block type, the PNG/PNM decoder (all colour types and filters),
and every HTTP endpoint against a dry-run server on a free port."""
import base64, json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import print_relay as R
import tmu220 as p


# ---------------------------------------------------------------- PNG writer with real filters
def paeth(a, b, c):
    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def png(w, h, ctype, depth, rows, filt=0, plte=None, trns=None):
    """rows: list of raw (unfiltered) scanline bytes."""
    bits = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype] * depth
    bpp = max(1, bits // 8)
    out, prev = b'', bytes(len(rows[0]))
    for i, r in enumerate(rows):
        f = filt if filt >= 0 else i % 5
        enc = bytearray()
        for x in range(len(r)):
            a = r[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            pred = [0, a, b, (a + b) // 2, paeth(a, b, c)][f]
            enc.append((r[x] - pred) & 255)
        out += bytes([f]) + bytes(enc)
        prev = r
    chunk = lambda t, d: len(d).to_bytes(4, 'big') + t + d + zlib.crc32(t + d).to_bytes(4, 'big')
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', w.to_bytes(4, 'big') + h.to_bytes(4, 'big') + bytes([depth, ctype, 0, 0, 0]))
    if plte:
        data += chunk(b'PLTE', plte)
    if trns:
        data += chunk(b'tRNS', trns)
    return data + chunk(b'IDAT', zlib.compress(out)) + chunk(b'IEND', b'')


def test_decoder():
    w, h = 7, 5
    grad = [[(x * 40 + y * 10) % 256 for x in range(w)] for y in range(h)]
    for filt in (0, 1, 2, 3, 4, -1):
        d = png(w, h, 0, 8, [bytes(r) for r in grad], filt)
        W, H, g = R.decode_image(d)
        assert (W, H) == (w, h) and all(abs(g[y][x] - grad[y][x]) < 0.6 for y in range(h) for x in range(w)), filt
    rgb = [bytes(v for x in range(w) for v in (grad[y][x], grad[y][x], grad[y][x])) for y in range(h)]
    assert abs(R.decode_image(png(w, h, 2, 8, rgb, -1))[2][2][3] - grad[2][3]) < 1
    rgba = [bytes(v for x in range(w) for v in (0, 0, 0, 0 if x == 0 else 255)) for y in range(h)]
    g = R.decode_image(png(w, h, 6, 8, rgba, -1))[2]
    assert g[0][0] == 255 and g[0][1] == 0, 'transparent composites to white, opaque black stays black'
    pal = bytes([0, 0, 0, 255, 255, 255])
    rows = [bytes([0b00010000, 0b00010000, 0b00010000, 0b00000000]) for _ in range(h)]     # 4-bit, high nibble first: 1,0,1,0,1,0,0
    g = R.decode_image(png(w, h, 3, 4, rows, 0, plte=pal))[2]
    assert g[0] == [255.0, 0.0, 255.0, 0.0, 255.0, 0.0, 0.0], g[0]
    bw = [bytes([0b10100000]) for _ in range(h)]                                            # 1-bit gray: 1,0,1,0...
    g = R.decode_image(png(w, h, 0, 1, bw))[2]
    assert g[0][:3] == [255.0, 0.0, 255.0]
    g16 = [bytes(v for x in range(w) for v in (grad[y][x], 0)) for y in range(h)]
    assert abs(R.decode_image(png(w, h, 0, 16, g16))[2][1][1] - grad[1][1]) < 1
    pgm = b'P5\n# c\n3 2\n255\n' + bytes([0, 128, 255, 255, 128, 0])
    assert R.decode_image(pgm)[2][0] == [0, 128, 255]
    pbm = b'P4\n3 1\n' + bytes([0b10100000])
    assert R.decode_image(pbm)[2][0] == [0, 255, 0]
    try:
        R.decode_image(b'GIF89a')
        raise AssertionError
    except R.JobError:
        pass
    print('decoder OK (gray 1/8/16-bit, RGB, RGBA, palette, all 5 filters, PGM, PBM)')


def test_render():
    img = base64.b64encode(png(40, 40, 0, 8, [bytes([(x * 6) % 256 for x in range(40)])] * 40)).decode()
    blocks = [
        {'type': 'text', 'text': 'x', 'font': 'A'}, {'type': 'text', 'text': 'x', 'size': 'both', 'bold': True, 'underline': True, 'red': True, 'align': 'right'},
        {'type': 'text', 'text': '中文 大', 'size': 'both', 'underline': True}, {'type': 'labeled', 'label': '12.', 'text': 'y' * 80},
        {'type': 'item', 'name': 'n', 'qty': '1'}, {'type': 'columns', 'left': 'a', 'right': 'b'}, {'type': 'rule', 'char': '='}, {'type': 'feed', 'lines': 2},
        {'type': 'image', 'data': img, 'dither': 'grid'}, {'type': 'image', 'data': img, 'dither': 'blue', 'fine': True, 'width': 64, 'align': 'left'},
        {'type': 'image', 'data': img, 'dither': 'threshold', 'invert': True, 'gamma': 1.5},
        {'type': 'chart', 'kind': 'bar', 'series': [{'values': [1, 2, 3]}]}, {'type': 'chart', 'kind': 'line', 'series': [{'values': [1, 3]}], 'fine': False},
        {'type': 'chart', 'kind': 'pie', 'slices': [{'name': 'a', 'value': 1}]}, {'type': 'chart', 'kind': 'hbar', 'rows': [{'label': 'r', 'value': 5}], 'over': 4},
        {'type': 'legend', 'entries': [{'label': 'shade', 'level': 0.3}, {'label': 'dashed', 'line': True, 'dash': 4}]},
        {'type': 'nv_print', 'slot': 2}, {'type': 'cut'}, ['title', 'v1 title'], ['meal', 'L', 'z'],
    ]
    for upside in (False, True):
        for paper in R.PAPER:
            out = R.build({'blocks': blocks, 'upside_down': upside, 'paper': paper, 'copies': 2})
            assert out['receipts'] == 4 and out['data'].count(p.CUT) == 4
            for line in out['preview'].split('\n'):
                if not line.startswith('[') and '~ cut ~' not in line:
                    assert R.width(line) <= R.PAPER[paper] * (1 if 'v1 title' not in line else 0.5) + 1e-9, (paper, line)
            if upside:
                assert R.ESC + b'{\x01' in out['data']
    for bad in ({'blocks': [{'type': 'nope'}]}, {'blocks': [{'type': 'chart', 'kind': 'bar', 'series': [{'values': list(range(90))}, {'values': [1]}, {'values': [1]}]}]},
                {'blocks': [], 'paper': '80'}, {'blocks': [{'type': 'text', 'text': 'x', 'align': 'middle'}]}, {'nope': 1}):
        try:
            R.build(bad)
            raise AssertionError(bad)
        except R.JobError:
            pass
    print('render OK (every block type, upside-down, 3 paper widths, copies, bad jobs refused)')


# ---------------------------------------------------------------- HTTP against a dry-run server
def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close(); return port


def test_http():
    tmp = Path(tempfile.mkdtemp())
    port = free_port()
    env = dict(os.environ, RELAY_TOKEN='sekret', RELAY_STATE=str(tmp / 'state.json'), RELAY_NV_DAILY_LIMIT='1')
    proc = subprocess.Popen([sys.executable, str(HERE / 'print_relay.py'), '--bind', '127.0.0.1', '--port', str(port), '--dry-run', str(tmp / 'jobs')],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f'http://127.0.0.1:{port}'

    def call(path, body=None, token='sekret', raw=False):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'POST')
        if token:
            req.add_header('Authorization', 'Bearer ' + token)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return r.status, (data if raw else data.decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8')
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + '/health', timeout=1); break
            except Exception:
                time.sleep(0.1)
        st, body = call('/health'); h = json.loads(body)
        assert st == 200 and h['dry_run'] and h['nv_daily_limit'] == 1
        assert 'is running' in call('/')[1]
        # page contract: GET /print?j=<base64url>, dedupe, again=1
        job = {'v': 1, 'id': 'abc123', 'label': 'Week: meals', 'blocks': [['title', 'MEAL PLAN'], ['meal', 'L', '红烧肉 x2 + 炒包菜 + 米饭'], ['cut'], ['item', 'Pork belly 五花肉', '2 lb']]}
        j = base64.urlsafe_b64encode(json.dumps(job).encode()).decode().rstrip('=')
        st, body = call('/print?j=' + j, token=None)
        assert st == 200 and 'Printing' in body and '2 receipts' in body, body
        assert 'Already printed' in call('/print?j=' + j, token=None)[1]
        assert 'Printing' in call('/print?j=' + j + '&again=1', token=None)[1]
        assert call('/print?j=not-base64!!', token=None)[0] == 400
        # POST endpoints need the token
        assert call('/print', {'blocks': [['text', 'x']]}, token=None)[0] == 401
        st, body = call('/print', {'id': 'p1', 'blocks': [{'type': 'chart', 'kind': 'pie', 'slices': [{'name': 'a', 'value': 2}, {'name': 'b', 'value': 1}]}]})
        assert st == 200 and json.loads(body)['ok']
        assert json.loads(call('/print', {'id': 'p1', 'blocks': [['text', 'x']]})[1])['duplicate']
        st, body = call('/print', {'blocks': [{'type': 'inverse'}]})
        assert st == 400 and 'white-on-black' in body
        st, txt = call('/render?format=text', {'blocks': [['head', 'Hi'], ['item', 'Garlic', '43 cloves (≈5 heads)']]})
        assert st == 200 and '(~5 heads)' in txt
        st, binary = call('/render', {'blocks': [['text', 'ok']]}, raw=True)
        assert st == 200 and binary.startswith(p.INIT) and binary.endswith(p.CUT)
        assert json.loads(call('/cancel', {})[1])['ok']
        assert json.loads(call('/id')[1]) == {'dry_run': True}
        # NV store: needs confirm, counts against the daily budget (limit 1 here)
        logo = base64.b64encode(png(64, 16, 0, 8, [bytes([0 if 8 < x < 56 else 255 for x in range(64)])] * 16)).decode()
        assert call('/nv/store', {'images': [{'data': logo}]})[0] == 400
        st, body = call('/nv/store', {'images': [{'data': logo, 'width': 60}], 'confirm': 'replace all stored images'})
        assert st == 200 and json.loads(body)['nv_writes_today'] == 1, body
        st, body = call('/nv/store', {'images': [{'data': logo}], 'confirm': 'replace all stored images'})
        assert st == 400 and 'budget' in body
        assert json.loads(call('/nv/print', {'slot': 1})[1])['ok']
        assert 'Printing' in call('/test', token=None)[1]
        assert 'Printing' in call('/demo', token=None)[1]
        assert call('/nope', token=None)[0] == 404
        jobs = sorted((tmp / 'jobs').glob('*.bin'))
        assert len(jobs) == 7, jobs   # print, again, POST print, NV store, NV print, test, demo
        print(f'http OK (status, health, id, GET /print contract + dedupe, POST print/render/cancel, token, NV guard; {len(jobs)} dry-run jobs)')
    finally:
        proc.terminate()
        proc.wait(5)


if __name__ == '__main__':
    p.selftest()
    R.selftest()
    test_decoder()
    test_render()
    test_http()
    print('ALL TESTS PASSED')
