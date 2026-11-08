---
name: "printing-on-epson-tm-u220"
description: "Use when writing code that prints to an Epson TM-U220 or similar 9-pin impact (dot matrix) ESC/POS receipt printer, including images, logos, charts, large or red text, or when its output shows garbage or Chinese characters instead of an image, jittered image strips, gaps between strips, or text size commands that do nothing; also when deploying, configuring or sending jobs to its print relay (tm-u220-relay)."
---

# Printing on the Epson TM-U220

## Overview

The TM-U220 is a 9-pin impact printer: 200 dots per line on 76 mm paper, about 80 dpi, black and (with an ERC-38(B/R) ribbon) red. Thermal-printer ESC/POS code mostly fails on it. Images and charts must be drawn on the computer and sent as 8-dot `ESC *` bands.

`scripts/tmu220.py` (Python 3, stdlib only) implements everything below and was checked on a real TM-U220. Import it rather than rewriting it:

```python
import tmu220 as p
PRINTER_IP = '<printer-ip>'                       # the printer's address on your network
c = p.Canvas(p.WIDTH, 96)
c.fill(lambda x, y: x / 199, 'blue')              # level 0 white .. 1 black
p.send(PRINTER_IP, p.INIT + p.size('REPORT', True, True) + c.bit_image() + p.CUT)
```

Run `python scripts/tmu220.py selftest` after changing it, and `python scripts/tmu220.py id <ip>` to confirm the model first: other printers on the network also listen on port 9100.

`python scripts/tmu220.py demo <ip>` prints a short sample. `scripts/full_demo.py` prints every feature: copy its receipt layout, legends, and bar, line and pie charts.

## Scripts and this printer

- The scripts (`scripts/tmu220.py`, `scripts/full_demo.py`) and `reference.md` live in https://github.com/StevenGuo42/tm-u220-relay (private) under `skills/printing-on-epson-tm-u220/`. If they are not next to this file, get them with `gh repo clone StevenGuo42/tm-u220-relay`.
- Steven's printer: `192.168.50.79`, port 9100, 76 mm paper, ERC-38(B/R) black + red ribbon, Chinese GB18030 model (prints GB18030 Chinese text directly; wrap Chinese lines in `ESC U 1` / `ESC U 0` because the 16-dot font prints in two passes).
- The printer is on the home network: a cloud workspace can't reach it. Send jobs from a machine on that network, for example the print relay (`app/print_relay.py` in the tm-u220-relay repo, see below).

## Deploying the relay

The relay takes HTTP (GET links from web pages, JSON POSTs from scripts) and prints. From the tm-u220-relay repo root, on a machine on the home network:

0. Needs Docker with Compose, and Python 3 for the checks. Get the code: `gh repo clone StevenGuo42/tm-u220-relay && cd tm-u220-relay` (private repo: run `gh auth login` first).
1. Check the code with no printer: `python3 app/tmu220.py selftest` and `python3 app/print_relay.py --selftest`.
2. Confirm the printer: `python3 app/tmu220.py id 192.168.50.79`. If its address changed, update `PRINTER_IP` in `compose.yaml`.
3. Put a token in `.env` (git-ignored): `echo "RELAY_TOKEN=$(openssl rand -hex 16)" > .env`. Compose refuses to start without it. POSTs need `Authorization: Bearer <token>` (or `?t=<token>`); GET `/print`, `/test`, `/health` stay open, so keep the port off the internet.
4. `docker compose up -d`, then `curl http://<host>:8765/health`.
5. Give clients the same token: on each machine that runs Claude, add `"env": {"RELAY_TOKEN": "<token>"}` to `~/.claude/settings.json`.

Settings in `compose.yaml`: `PRINTER_IP`, `RELAY_PORT`, `RELAY_STATE` (NV logo write budget, kept in `./data`), and optionally `RELAY_NV_DAILY_LIMIT`, `RELAY_DRY_RUN`. `./app` is mounted, not built into an image: after changing code, `docker compose restart`. To try jobs without printing, run `python3 app/print_relay.py --dry-run jobs` (saves `.bin` + `.txt` per job). Logs: `docker compose logs -f`.

To send a job: `curl -H "Authorization: Bearer $RELAY_TOKEN" -d '{"blocks": [...]}' http://192.168.50.31:8765/print` (`/render?format=text` previews without printing). If `RELAY_TOKEN` is unset, ask the user for it; never write the token into files or commit it.

## Quick reference

| Need | Use | Not |
|---|---|---|
| Image | `ESC * 0` bands, 200 dots wide (69.5 mm: 180, 57.5 mm: 150) | `GS v 0`, `GS ( L` (printed as garbage text) |
| Bands that touch | `ESC 3 16` (16/144") during the image, `ESC 2` after | other spacings leave gaps or overlap |
| No sideways jitter | `ESC U 1` during images, `ESC U 0` after | default two-way printing |
| Smoother image, half speed | `bit_image(fine=True)`: two passes per band, offset by `ESC J 1` | |
| Double height/width | `ESC !` bits 0x10 / 0x20 (keep bit 0x01 for Font B); double height needs `ESC U 1` too (`size()` does this) | `GS !` (ignored) |
| Bold, underline, align | `ESC E`, `ESC -`, `ESC a` | `ESC G` (no visible effect) |
| Red | `ESC r 1` ... `ESC r 0` | red as a middle tone (looks worse) |
| Cut | `GS V 66 0` after feeding | `ESC i` |
| Stop a running job | `10 14 08 01 03 14 01 06 02 08` on a new connection; data still in the network card prints anyway | |
| Inverse text | not supported | `GS B` (prints normal) |
| Page reads upside down (printer mounted vertically) | `upside_down(blocks)`: `ESC {` plus lines sent last-first; images as `Canvas.flipped_image()` | `ESC {` alone (lines stay in the wrong order) |

## Shading (verified side by side on the printer)

| Content | Method |
|---|---|
| Charts, bars, pies, flat fills | `'grid'` (4×4 Bayer) plus 1-dot outlines (`Canvas.regions`) |
| Photos, gradients, spheres, fractals | `'blue'` (blue noise, void-and-cluster) |

Avoid error diffusion (Floyd-Steinberg, Atkinson): pale edges get eaten. `ESC * 1` (400 positions) looks no sharper, because one pin can't fire at two neighbouring positions. A dot row is ~11% taller than a dot column is wide: sample any picture, photo or circle at `(x, y * p.ASPECT)`, or it prints stretched.

Normal mode needs no tone curve: pass levels straight through. Printed gradients stayed even and only shifted darker or lighter.

Fine mode doubles the rows, so a canvas needs twice the height, `y * p.ASPECT_FINE` and blue noise, shaded with `fill(level, 'blue', fine=True)`. Grid shading looked bad in fine mode. Fine mode looks a lot better than normal on curved shading but does not match its darkness exactly, so don't mix the two where they will be compared.

## Permanent (NV) memory has a usage limit

Writing to NV memory (`FS q` stored images, and the other NV write commands) wears it out. Epson says no more than 10 writes per day, shared across those commands, and no command reports the writes left. Store a logo once at setup, never per job or in a loop, and don't write without the user's OK. Each `FS q` replaces all stored images, so print the current slots with `FS p` first. For anything that changes, send it as an `ESC *` image instead.

```python
p.send(ip, p.nv_store(logo), wait=15)     # once, in its own job; canvas width a multiple of 8
p.send(ip, p.INIT + p.nv_print(1) + p.CUT)  # in any later job
```

Print with `FS p n 1` (`nv_print`): `m=0` comes out half as wide, probably because stored columns are half-dot positions. `m=2` and `m=3` gave no double height.

## Upside-down pages

`ESC {` rotates each line in place, but the first line printed is still the first out of the printer, so the job has to be sent last line first. Build it as a list in reading order, one line per block. Each block must restore any alignment or colour it sets:

```python
blocks = [p.CENTER + p.size('REPORT', True, True) + p.LEFT, logo.flipped_image(), p.text('last line')]
p.send(ip, p.INIT + p.upside_down(blocks) + p.CUT)
```

Text styles, red and `inline()` swatches all flip with the mode. Use a full-width canvas with `flipped_image()`.

## Common mistakes

- **Chinese characters instead of an image**: the printer didn't understand the image command (usually `GS v 0`) and printed the bytes as text in its GB18030 font.
- **Accented text prints as Chinese**: Chinese models start in Kanji mode. A byte above 0x7F swallows the next byte as well (`caf\x82 ok` printed `cafok`). Send `FS .` (`1C 2E`) first.
- **"Black" prints red, "red" barely prints**: the ribbon cassette isn't pushed fully in.
- **First connection times out**: the printer is still starting up. `send()` retries for ~10 s.
- **Data cut short**: closing the socket immediately after `sendall`. `send()` waits 2 s.
- **Text-only charts**: real bars, pies and lines print well as images. Labels line up with 25-dot slots (5 Font B characters).

Details, measurements and sources: `reference.md`.