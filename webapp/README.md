# webapp - research archive viewer

A stateless, **read-only** Flask app that serves the archive from Oracle: catalog,
`LIKE` search, per-report summary, and the PDF streamed from the BLOB. It never writes,
and never talks to the AI or SMTP. Uses `python-oracledb` thin mode (no Oracle client).

## Routes
| Path | Purpose |
|------|---------|
| `GET /` | Latest digest + recent reports (paginated), each tagged with its source |
| `GET /search?q=` | `LIKE` search over titles/authors/synopsis (results show the source) |
| `GET /reports/<id>` | Metadata (incl. source) + AI summary + embedded PDF |
| `GET /reports/<id>/pdf` | PDF bytes from the BLOB (`?download=1` to attach) |
| `GET /digest/<YYYY-MM-DD>` | A given day's digest |
| `GET /healthz` `GET /readyz` | Liveness / readiness (DB ping) |

`<id>` may be a bare id (the original source) or a namespaced `source:id` (e.g. `jpm:...`);
the route accepts both. The archive reads the `reports.source` column (part of the
`001_init.sql` schema).

## Environment
| Var | Meaning |
|-----|---------|
| `DB_USER` / `DB_PASSWORD` | **SELECT-only** DB user |
| `DB_CONNECT_STRING` | `host:port/service_name` |
| `DB_SCHEMA` | schema that owns the tables (set as CURRENT_SCHEMA) |

## Run locally on the LAN (Windows PC)
Reads DB creds from the repo-root `.env` automatically (`DB_USER/PASSWORD/CONNECT_STRING/DB_SCHEMA`).
```bat
cd webapp
pip install -r requirements.txt
:: sturdy server (recommended on Windows; gunicorn does NOT run on Windows):
waitress-serve --host=0.0.0.0 --port=8080 app:app
:: or a quick dev server:
python app.py
```
Then share the URL with the LAN:
```bat
ipconfig            :: note the IPv4 Address, e.g. 10.1.2.3
```
Colleagues open `http://10.1.2.3:8080`. `--host=0.0.0.0` binds all interfaces so the LAN can
reach it. If they can't connect, allow the port through **Windows Defender Firewall**
(first run may prompt "Allow access" -> allow on the Private network), or add an inbound rule
for TCP 8080. Populate data first by running the pipeline; an empty archive shows nothing.

## Deploy to OpenShift
```bash
# 1) build the image from this directory
oc new-build --name treasury-news-web --binary --strategy docker
oc start-build treasury-news-web --from-dir=. --follow

# 2) create the DB secret (SELECT-only user)
cp openshift/secret.example.yaml openshift/secret.yaml   # edit real values
oc apply -f openshift/secret.yaml

# 3) deploy (edit deployment.yaml image PROJECT, and route.yaml host first)
oc apply -f openshift/deployment.yaml
oc apply -f openshift/service.yaml
oc apply -f openshift/route.yaml
```
Keep the Route **internal** - licensed content must not be publicly reachable. Point the
pipeline's `WEBAPP_BASE_URL` at this Route so the digest emails link here.
