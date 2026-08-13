# Read-only research-archive viewer. python-oracledb thin mode -> no Oracle client needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

EXPOSE 8080
# OpenShift runs containers with a random UID in the root group; a numeric non-root USER
# plus a group-readable /app works with that. python-oracledb thin mode needs no client libs.
USER 1001

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--timeout", "60", "app:app"]
