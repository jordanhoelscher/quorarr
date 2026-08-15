# Upgrading Quorarr

**Back up the database file before you upgrade. Then pull and restart — the schema
migrates itself.**

```bash
# 1. Back up (see "Backing up" below for the live-container variant)
docker compose stop quorarr
cp /path/to/data/pensieve.db /path/to/data/pensieve.db.bak-$(date +%F)

# 2. Upgrade
docker compose pull quorarr
docker compose up -d quorarr

# 3. Confirm the new version is serving
docker exec quorarr python3 -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').read())"
```

---

## How the schema is versioned

Quorarr stamps the schema version into the SQLite file itself, in the header field
`PRAGMA user_version`. On every startup it reads that number, applies each newer migration
in order, and stamps the new version — so the database says what shape it is in, and no
migrations table, migration tool, or manual step is involved.

```bash
docker exec quorarr python3 -c \
  "import sqlite3,os;print(sqlite3.connect(os.environ.get('DB_PATH','/data/pensieve.db')).execute('PRAGMA user_version').fetchone()[0])"
```

A database created by a release older than 0.10.0 reports `0`. The first 0.10.0+ startup
adopts it in place: the baseline migration only creates what is missing and only adds
columns it can see are absent, so existing members, flags, requests, and push
subscriptions are left exactly as they are.

**Each migration runs in one transaction** that also writes the version stamp, so a step
either lands whole or not at all. An upgrade interrupted by a power cut resumes from the
last version that actually committed.

**A failed migration is a failed startup.** Quorarr raises and the container exits rather
than serving against a schema it could not apply — a half-migrated database that quietly
answers queries is how one bad upgrade becomes a week of strange bugs. If that happens,
restore your backup, pin the previous image tag, and open an issue with the traceback from
`docker compose logs quorarr`.

---

## Backing up

The database is the only stateful thing Quorarr owns; everything else is rebuildable from
your env file. It runs in WAL mode, so a plain `cp` of a *running* database can miss the
write-ahead log. Either stop the container first (as above), or take a consistent online
copy:

```bash
docker exec quorarr python3 -c "
import os, sqlite3
src = sqlite3.connect(os.environ.get('DB_PATH', '/data/pensieve.db'))
dst = sqlite3.connect('/data/pensieve.backup.db')
src.backup(dst); dst.close(); src.close()"
```

Copy the resulting file somewhere off the host. Restoring is the reverse: stop the
container, put the file back at `DB_PATH`, delete any stale `-wal` / `-shm` siblings, start
it again.

---

## Downgrading

Rolling back to an older image is safe for the *database* — an older build sees a
`user_version` higher than it knows about, leaves the file alone, and logs a warning — but
it will not understand tables or columns added since. Treat a downgrade as "restore the
backup you took", not as "run the old image against the new database".
