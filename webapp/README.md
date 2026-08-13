# webapp - research archive viewer

A stateless, **read-only** Flask app that serves the archive from Oracle: catalog,
`LIKE` search, per-report summary, and the PDF streamed from the BLOB. It never writes,
and never talks to the AI or SMTP. Uses `python-oracledb` thin mode (no Oracle client).

## Routes
| Path | Purpose |
|------|---------|
| `GET /` | Latest digest + recent reports (paginated) |
| `GET /search?q=` | `LIKE` search over titles/authors/synopsis |
| `GET /reports/<id>` | Metadata + AI summary + embedded PDF |
| `GET /reports/<id>/pdf` | PDF bytes from the BLOB (`?download=1` to attach) |
| `GET /digest/<YYYY-MM-DD>` | A given day's digest |
| `GET /healthz` `GET /readyz` | Liveness / readiness (DB ping) |

## Environment
| Var | Meaning |
|-----|---------|
| `DB_USER` / `DB_PASSWORD` | **SELECT-only** DB user |
| `DB_CONNECT_STRING` | `host:port/service_name` |
| `DB_SCHEMA` | schema that owns the tables (set as CURRENT_SCHEMA) |

## Run locally
```bash
cd webapp
pip install -r requirements.txt
export DB_USER=... DB_PASSWORD=... DB_CONNECT_STRING=host:port/service DB_SCHEMA=...
python app.py                 # dev server on http://localhost:8080
# or production-style:
gunicorn -b 0.0.0.0:8080 app:app
```

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
