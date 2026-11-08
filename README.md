# print-relay

HTTP relay for an Epson TM-U220 receipt printer (9-pin impact, ESC/POS over raw TCP 9100). Web pages can't open a raw TCP socket, so they open a link to the relay; scripts POST JSON to it. Python 3.8+, standard library only.

```
app/                      the relay (print_relay.py), its tests, tmu220.py (printer library)
skill/printing-on-epson-tm-u220/
                          Claude skill for writing code that prints to this printer
                          (scripts/tmu220.py is a symlink to app/tmu220.py)
compose.yaml              runs the relay in python:3.12-slim on port 8765
```

## Skill

`skill/printing-on-epson-tm-u220/` is a Claude skill for writing code that prints to the TM-U220: images and charts as `ESC *` bands, double size, red, Chinese and accented text, and fixes for garbage output. It also has the steps to deploy this relay. `reference.md` has the measurements and sources; `scripts/full_demo.py` prints every feature.

Install it by linking it into Claude's skills folder:

```sh
ln -s "$PWD/skill/printing-on-epson-tm-u220" ~/.claude/skills/
```

To share it as a zip, use `cd skill && zip -r printing-on-epson-tm-u220.zip printing-on-epson-tm-u220` (no `-y`), so the zip holds the real `tmu220.py` and not the symlink.

## Run

```sh
echo "RELAY_TOKEN=$(openssl rand -hex 16)" > .env   # required; protects the POST endpoints
docker compose up -d
curl http://localhost:8765/health
```

Without Docker: `python3 app/print_relay.py` (see `--help`). Settings come from `compose.yaml` / the environment: `PRINTER_IP`, `RELAY_PORT`, `RELAY_BIND`, `RELAY_TOKEN`, `RELAY_STATE`, `RELAY_NV_DAILY_LIMIT`, `RELAY_DRY_RUN`. `./app` is mounted, so after changing code run `docker compose restart`.

Keep it on the home network: the GET endpoints are open.

## Endpoints

| | |
|---|---|
| `GET /` , `/health`, `/id` | status page, JSON health, printer model |
| `GET /test`, `/demo` | print a test receipt / every feature |
| `GET /print?j=<base64url JSON job>` | print from a link (repeat links are deduped; add `&again=1`) |
| `POST /print` | print a JSON job |
| `POST /render?format=bin\|text` | lay out a job without printing |
| `POST /cancel` | stop the running job |
| `POST /nv/store`, `/nv/print` | store / print a logo in printer memory (10 writes/day, budget kept in `./data`) |

POSTs need `Authorization: Bearer $RELAY_TOKEN` (or `?t=`). A job is `{"blocks": [...]}`, e.g.:

```sh
curl -H "Authorization: Bearer $RELAY_TOKEN" -d '{"blocks": [["title", "HELLO"], ["rule"], ["text", "printed via relay"]]}' http://localhost:8765/print
```

Block types: see `build()` in `app/print_relay.py` and `TEST_JOB` for examples.

## Test

```sh
python3 app/tmu220.py selftest       # printer library, no printer needed
python3 app/test_relay.py            # relay rendering + HTTP against a dry-run server
python3 app/tmu220.py id <printer-ip> # confirm the printer model on the network
```
