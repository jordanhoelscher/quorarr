# Contributing to Quorarr

Thanks for looking. Quorarr is maintained by one person, so the most useful contributions
are small, tested, and scoped to something already agreed on.

**Before writing a large change, open an issue or discussion.** A PR that lands is one whose
shape was settled first; a big unsolicited one is usually a slow disappointment for both of
us.

---

## Repository layout

```
backend/           FastAPI app
  pensieve/        Python package name, kept from the project's original name:
                   renaming it would touch every import, the Dockerfile CMD,
                   and every deployed DB path for no functional gain.
    api/           Route modules: auth, guest, member, discover, admin
    clients/       Upstream HTTP clients: plex.tv, radarr, sonarr, jellyseerr
    services/      Pure logic: deletion state machine, quality tiers, discover, access
  tests/           pytest suite, fixtures under tests/fixtures/
frontend/          Svelte 5 + Vite PWA
  src/views/       One file per screen
  src/lib/         Shared components, API client, stores
docker/Dockerfile  Multi-stage build; context is the repo root
docs/              Setup guide and design notes
```

The backend serves both the API and the built SPA from one process. `docker/Dockerfile`
builds the frontend with Node, then copies `dist/` into the Python runtime image.

---

## Development setup

### Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Provide the env vars from the configuration table in the README.
# Throwaway values are fine against a test Radarr/Sonarr/Jellyseerr;
# the app refuses to start if a required one is missing.
set -a && source ../quorarr.env && set +a

uvicorn pensieve.main:create_app --factory --reload --port 8000
```

```bash
pytest              # the whole suite
pytest -k deletion  # one area
```

Requires Python 3.12+.

### Frontend

```bash
cd frontend
npm install
npm run dev      # Vite dev server; proxies /api and /auth to localhost:8000
```

```bash
npm run check    # svelte-check + tsc — treat warnings as failures
npm test         # vitest
npm run build    # must succeed before you open a PR
```

Requires Node 22+.

---

## How changes are expected to be written

**Tests first.** Every behavioural change should arrive with a test that fails without it.
The existing suite is the model: services are pure functions over a SQLite connection and an
injected `now`, and routes are tested through `TestClient` with mocked upstreams. If a change
is genuinely untestable, say so in the PR and explain why.

**No silent failures.** This codebase is unusually deliberate about the difference between
"empty" and "broken". An unreadable upstream must never come back as an empty list — read
`services/access.py` and `clients/plex_tv.py` before touching anything that decides who has
access. If you add a fallback, make it *say* it's a fallback.

**Comment the why, not the what.** The dense comments in this codebase exist because the
non-obvious decisions (fail-closed reconciliation, salted cookies, explicit `profileId` on
every request) are exactly the ones a future reader would innocently undo.

**Keep the diff shaped like the problem.** Drive-by refactors in a bugfix PR make review
slower, not faster.

---

## Commits

Conventional Commits:

```
feat: 720p lane for the Discover season picker
fix: don't strand a denied 4K ask when Jellyseerr 409s
docs: explain the reverse-proxy requirement
test: cover re-flag cooldown boundary
chore: bump svelte to 5.44
refactor: extract profile lookup from plan_action
```

Use `feat!:` or a `BREAKING CHANGE:` footer for anything that changes an API contract, an env
var's meaning, or the database schema.

Versioning is SemVer. If your change warrants a bump, bump it **in the same commit**:
`backend/pensieve/__init__.py` (`__version__`) and `frontend/package.json` must agree —
`/health` and the UI footer read from them. `backend/pyproject.toml` needs no edit: it
declares its version dynamically from `pensieve.__version__`.

---

## Pull request checklist

- [ ] `cd backend && pytest` passes
- [ ] `cd frontend && npm run check` passes
- [ ] `cd frontend && npm test` passes
- [ ] `cd frontend && npm run build` passes
- [ ] New behaviour has a test that fails without the change
- [ ] No new env var without a row in the README table **and** `.env.example`
- [ ] No new user-facing string hardcodes the app or owner name — read `APP_NAME` /
      `OWNER_NAME` (backend: `Settings`; frontend: `lib/branding.svelte.ts`)
- [ ] No secrets, real hostnames, API keys, or tokens anywhere in the diff
- [ ] Docs updated if behaviour a user can see changed
- [ ] Version bumped in all three places if the change warrants one
- [ ] PR description says what you actually ran and verified, not just what you intended

CI runs the same four commands. A red CI is not a review request.

---

## Scope

Quorarr is **Plex-only**, and the core is deliberately narrow.

**In scope**
- Bug fixes anywhere.
- Improvements to the governance loop: flags, vetoes, approvals, quality tiers, access.
- Reverse-proxy, deployment, and documentation fixes — these are the most common source of
  a bad first hour, and the most valuable thing to improve.
- Accessibility and mobile-layout fixes.
- Making hardcoded behaviour configurable *when there's a real use case in an issue*.

**Out of scope (for the core)**
- Jellyfin/Emby support. Not hostile to it — but the identity model, the login gate, and the
  access-request flow are all plex.tv. If you want to attempt it, open a discussion first so
  we can agree on the abstraction seams; a PR that scatters `if plex:` across every module
  will not merge.
- Replacing Jellyseerr with a built-in request engine.
- A rule engine for automatic deletion. That's Maintainerr's job, and it's the opposite of
  this project's premise — deletions come from people and can be argued with.
- Multi-Plex-server support.
- Alternative databases. SQLite is the deliberate choice for a few thousand rows.
- New auth backends (LDAP, OIDC, local accounts). Plex OAuth *is* the model.

If you're unsure which side of that line something falls on, ask in an issue. "Would you
merge this?" is a completely reasonable question to lead with.

---

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
