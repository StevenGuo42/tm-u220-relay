# TM-U220 reference

"Verified" means it was printed on a TM-U220 (firmware 3.13 ESC/POS, GB18030 font, 76 mm paper,
ERC-38(B/R) ribbon) over raw TCP 9100 on 2026-11-03 and checked by eye. Everything else is from
documentation, or listed under "Not tried yet".

## Hardware

| | |
|---|---|
| Head | 9-pin serial impact, bidirectional by default (logic seeking) |
| Print width | 76 mm paper: 63.4 mm, max 200 dots / 400 half-dot positions. 69.5 mm: 180 / 360. 57.5 mm: 150 / 300 |
| Dot pitch | Horizontal ~0.317 mm (63.4 mm / 200). Vertical 2/144" = 0.353 mm. Ratio ~1.11 |
| Vertical motion unit | 1/144" (half a dot), used by `ESC 3`, `ESC J`, `ESC K` |
| Fonts | Font B 7×9, 40 per line (default). Font A 9×9, 35 per line. Chinese 16×16 |
| Ribbons | ERC-38(B) black, ERC-38(P) purple, ERC-38(B/R) black + red |
| ID | `GS I 0x42` maker, `0x43` model name, `0x41` firmware, `0x45` font. Replies start with `_`, end with NUL |

## Commands

| Command | Bytes | Status |
|---|---|---|
| Initialise | `1B 40`: resets print modes to the power-on defaults | Verified: also resets red to black |
| Bit image, 8-dot single density | `1B 2A 00 nL nH d...`, one byte per column, top dot = MSB | Verified, 200 dots |
| Bit image, 8-dot double density | `1B 2A 01 nL nH d...` | Verified to print; no visible gain over m=0 |
| Raster image | `1D 76 30 ...` | **Not supported**: data printed as text |
| Line spacing | `1B 33 10` for touching image bands; `1B 32` resets to 1/6" | Verified |
| Unidirectional | `1B 55 01` on, `1B 55 00` off | Verified: removes band-to-band jitter in images and double-height text. Epson recommends it for multi-line images |
| Print mode | `1B 21 n`: 0x01 Font B, 0x08 bold, 0x10 double height, 0x20 double width, 0x80 underline | Verified (size bits). Double height jitters between its two passes without `ESC U 1` |
| Character size | `1D 21 n` | **Ignored** |
| Bold | `1B 45 01` / `00` | Verified |
| Underline | `1B 2D 01` / `00` | Verified |
| Font | `1B 4D 00` Font A, `01` Font B | Verified |
| Align | `1B 61 00/01/02` left, centre, right | Verified |
| Colour | `1B 72 00` black, `01` red | Verified with ERC-38(B/R) |
| Reverse (white on black) | `1D 42 01` | **Not supported**: prints normal |
| Feed and cut | 4 × `0A`, then `1D 56 42 00` | Verified: the last line clears the cutter |
| Double-strike | `1B 47 01` / `00` | Verified: **no visible effect**, alone or with bold |
| Feed n/144" | `1B 4A n` | Verified: `ESC J 1` offsets a second pass by half a dot (fine mode) |
| Cancel Kanji mode | `1C 2E` | Verified: `caf\x82 ok` printed `café ok` with it, `cafok` without. Needed for single-byte text above 0x7F on Chinese models |
| Clear buffer (real time) | `10 14 08 01 03 14 01 06 02 08` | Verified on a second connection: stopped a running job early and discarded the waiting lines. The cut at the end of the job still ran, probably because it was still in the network card |
| Upside-down mode | `1B 7B 01` on, `1B 7B 00` off | Verified 2026-11-05 with lines sent last-first: bold, underline, red, double size and all three alignments read correctly when rotated (`ESC a` left stays on the reader's left). `ESC *` bands also rotate if sent last-first, including inline swatches. A full-width image with pixels rotated on the computer and the mode off looks the same. The whole full demo also printed correctly this way, including fine-mode images, Font A, accented and GB18030 text |
| Store NV image | `1C 71 n` then per image `xL xH yL yH d...`: (x × 8) columns, (y × 8) rows, column order, top dot = MSB. **Writes NV memory, replaces all stored images** | Verified with 1 image, 120 × 48, in its own job, then a 15 s wait |
| Print NV image | `1C 70 n m` | Verified: `m=1` is the same size as `ESC *`, `m=0` half as wide. `m=2` and `m=3` gave no double height. Follows `ESC a` alignment; text after it prints normally. An empty slot prints nothing |

## Image quality findings (all verified on paper)

- **Shading:** a 4×4 Bayer grid looks best on charts and flat fills (even texture). Blue noise
  looks best on photos and complex shapes (no grid texture or moiré). Error diffusion at 400
  positions left a bite out of the pale edge of a shaded sphere: it only places a dot once
  enough darkness builds up, which moves pale outlines inward.
- **Outlines:** a 1-dot border around every shaded region keeps pale regions readable.
- **Double density (`ESC * 1`):** curves looked the same as at 200 dots. Impact pins can't fire
  at two neighbouring positions in one pass (the printer drops such dots), and dots overlap anyway.
- **Inline images:** an 8-dot band followed by text on the same line works for legend swatches
  and labelled bars, also in red.
- **Full demo:** `scripts/full_demo.py` printed and looked great overall, including GB18030 Chinese
  text, a fine-mode line chart and a pie chart. It was judged as a whole, not item by item.
- **Tone curve, normal mode:** continuous ramps at `level ** gamma` stayed even at every gamma
  tried (grid 0.8, 1.0, 1.25; blue 1.25, 1.5, 1.75). Gamma only shifted the shade. The library
  applies none. On a coarser 8-step strip, the eye picked 1.0 for grid and 1.5 for blue. Blue at
  1.0 was on that strip but not on the continuous ramps.
- **Fine mode:** two passes per band, the second half a dot lower, gives 2× vertical rows at half
  the speed. A shaded sphere and curves look a lot smoother. Rows are half as far apart but dots
  are the same size, so uncorrected fine mode printed dark grey as black. `fill(fine=True)` uses
  coverage `1 - sqrt(1 - level ** 1.5)`: the best of the blue-noise ramps tried (no correction,
  this one, and gamma 2.0). It still doesn't match normal mode's darkness exactly. Grid shading
  looked bad in fine mode at all three settings.
- **Red as a middle tone:** a gradient of black and red dots looked worse than black alone, and
  not black at the dark end of a sphere.

## NV memory

The guide lists 128 KB of NV bit image memory and 8 KB of user NV memory. Epson's `FS q` page says
not to use the NV write commands, in any combination, more than 10 times per day. No total write
rating was found, and no command known to us reports the writes left. Each `FS q` replaces all
stored images, so changing one logo means sending the whole set again. `FS q` is marked obsolete
by Epson but works on this printer.

## Not tried yet

- `FS q` with more than one image.
- Whether a shorter wait than 15 s after `FS q` is safe, and whether NV images need `ESC U 1`.
- Reading a stored logo back after a power cycle.
- Upside-down pages with `flipped_image()` on canvases narrower than full width, or NV images.
- Printing with the printer standing vertically. The guide says to install it horizontally, or on a wall with the WH-10 bracket (Type B or D), which also involves moving the roll-paper detector (Appendix B).

## Sources

- [TM-U220 Technical Reference Guide, Rev. H](https://files.support.epson.com/pdf/pos/bulk/tm-u220_trg_en_std_revh.pdf) (print width, fonts, ribbons)
- [Epson ESC/POS: TM-U220 supported commands](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/tmu220.html)
- [Epson ESC/POS: FS q](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/fs_lq.html) (10 NV writes per day; each definition replaces all images)
- [Epson ESC/POS: ESC U](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/esc_cu.html), [ESC *](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/esc_asterisk.html), [ESC J](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/esc_cj.html)
- [Odoo PR #131310](https://github.com/odoo/odoo/pull/131310) (`ESC *` for TM-U models; `GS v 0` prints garbage)
- [RISC OS Budget DTP, ch. 10](http://www.riscos.com/support/users/budgetdtp/chap10.htm) and [US Patent 5,871,287](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/5871287) (no adjacent dots from one pin)
- [Near letter-quality printing](https://en.wikipedia.org/wiki/Near_letter-quality_printing) (multi-pass with fractional offsets)
- [3nstar RPT001 manual](https://3nstar.com/wp-content/uploads/2023/07/RPT001-Programming-Manual.pdf) (`FS .` and Kanji mode at power-on)
- Ulichney, "The void-and-cluster method for dither array generation", SPIE 1993 (blue noise)
