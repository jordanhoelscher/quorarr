# Changelog

All notable changes to this project are documented here. The format is loosely
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 0.9.0 predate the public repository; they are summarised rather
than itemised, since their history lives in a private monorepo.

## [1.2.1] — 2026-08-18

### Changed

- **A filmography is ordered by vote count, not by TMDB "popularity".** The
  1.2.0 ordering looked reasonable and was not: `popularity` is a rolling
  *trending* score, so a daily talk show someone guested on once outscores
  everything they are actually known for. It left ten of Tom Hanks's fifty
  rows as films, led by The Simpsons and The Daily Show, with Forrest Gump far
  down the grid; Scarlett Johansson's page opened on three chat shows.
  `voteCount` — how many people cared enough to rate the title, which does not
  decay — puts the work back on top: the same fifty become 48 films led by
  Forrest Gump, Toy Story and The Green Mile. The acting-only filter and the
  fifty-credit cap are unchanged.

### Fixed

- `frontend/package-lock.json` carried the pre-rename `pensieve-frontend` and a
  version stuck at `0.1.0`, so the one file that pins the build disagreed with
  `package.json` about what it was building. Regenerated; no dependency
  changed.

## [1.2.0] — 2026-08-18

### Added

- **Actors are searchable, and they have a page.** Typing in Discover now
  raises a list of matches under the field — films and shows as before, and
  the people in them, which the results grid has never been able to show
  because a person is not something to request. Tapping one opens their
  filmography: acting credits only, most popular first, capped at fifty, as
  ordinary poster tiles that open the same detail sheet and file through the
  same guarded request lane as anything else. This closes the gap where
  "something with Tom Hanks in it" was a question the app could not be asked.
- **Search suggestions.** The dropdown is debounced at two characters and
  driven by arrows, Enter and Escape, with the highlight shared between the
  keyboard and the mouse so the two can never disagree about what Enter would
  take. Enter with nothing highlighted still submits the typed query rather
  than the first guess. Pressing it, tapping outside, or leaving the field
  dismisses the list; the full results grid underneath is unchanged.
- `GET /api/discover/suggest?q=` and `GET /api/discover/person/{person_id}`,
  both member-gated like their siblings. `suggest` reads the same Jellyseerr
  search as `/search`, from the same cache entry — the two calls a keystroke
  fires cost one upstream request between them — and differs only in keeping
  people and cutting the list to eight.

### Fixed

- **Multi-word search reached Jellyseerr as `tom+hanks` and failed.** The query
  travelled as a form-encoded parameter, so a space arrived as `+`; Jellyseerr
  answers that with a 400 — *"Parameter 'query' must be url encoded"* — which
  this app reported as "jellyseerr unreachable". Every search of more than one
  word had been a 502 since Discover shipped, which is most searches for a
  person and a good share of searches for a film. The query is now
  percent-encoded into the URL, `&` and `=` included, so a title carrying one
  cannot open a second parameter either.

### Notes

- Credits are de-duplicated on media type *and* TMDB id, not the id alone.
  TMDB emits one row per credit, so a second role in the same film is a second
  row; and its id space is per media type, so film 13 and show 13 are two
  different titles a bare-id key would silently merge.
- `/api/discover/search` is untouched: people are still dropped from it. The
  grid's contract is that everything in it can be requested, and folding a
  person into that list would have made every caller filter for one.

## [1.1.0] — 2026-08-15

### Added

- **The pipeline board is a staged poster wall.** Requests group under the stage
  they are actually in — Downloading, Finding a copy, Partly there, Awaiting
  approval, Ready to watch — three poster tiles across, with a stage omitted
  entirely when nothing is in it. Status is carried by the artwork: dimmed means
  waiting, a lit tile with a progress bar means transferring, a check means
  watchable now, and the single warm mark on the page flags a stall. Previously
  this view was a flat list of text cards whose one visual element, the progress
  bar, is absent whenever both download queues are idle — which is most of the
  time. Grouping rules live in `frontend/src/lib/pipelineStages.ts` and are
  tested independently of the view; a status the build does not recognise folds
  into "Finding a copy" rather than disappearing from the board.
- **Pipeline cards carry poster art and a TMDB id.** Artwork comes from whichever
  source knew the title first: the Radarr/Sonarr libraries, or a hint recorded at
  request time for media too new to be in either. Tapping a tile opens the same
  detail sheet Discover uses, with availability pinned to what the board already
  knows, so it can never offer a Request button for something already in flight.
- `title_hints.poster` (migration 2), so a request filed seconds ago has a face
  rather than blank stone.

### Fixed

- `posterUrl` no longer prefixes the TMDB base onto an already-absolute URL,
  which produced an unfetchable `.../w342/https://…`. Radarr and Sonarr return
  absolute poster URLs, so this affected every arr-sourced tile.
- Approving a parked 4K request no longer wipes a poster hint already recorded
  for that title. The 4K queue stores no artwork and the write was an
  `INSERT OR REPLACE`; it is now an upsert that leaves the poster alone.

## [1.0.0] — 2026-08-15

Initial public release. No functional change from 0.10.0.

## [0.10.0] — 2026-08-15

### Added

- **Real schema migrations.** The database now carries its own version in
  `PRAGMA user_version`, and startup applies each pending migration in order,
  every one inside a transaction that also writes the version stamp. Upgrading
  is still just "pull and restart"; what changed is that a schema change now has
  a defined, resumable path onto a database that already has data in it.
  Migration 1 is the 0.9.1 schema as a baseline, written to adopt an existing
  pre-versioning database in place — no data is rebuilt or moved.
- `docs/upgrading.md`: how the versioning works, how to back up a WAL-mode
  SQLite file consistently, and what a failed migration looks like.

### Changed

- A migration that fails is now a failed startup with the real error, instead of
  the previous `ALTER TABLE ... except OperationalError: pass`, which could not
  tell "this column already exists" from "this database is broken" and reported
  neither.
- This package's `INFO` logs are now visible under uvicorn. Nothing configured
  logging, so the root logger's last-resort handler dropped everything below
  `WARNING` — including the one line that tells an operator a migration ran.

## [0.9.1] — 2026-08-15

### Fixed

- `index.html`'s `<title>` and `apple-mobile-web-app-title` are substituted
  from `$APP_NAME` at build time. They were the one branded surface still
  hardcoded: the runtime rewrite from `/api/config` fixes them once the page
  has scripted, but the tab title is painted before any script runs, so a
  rebranded instance showed the upstream name for that beat.

## [0.9.0] — 2026-08-15

The release that made the app deployable by someone other than its author.

### Added

- `APP_NAME`, `OWNER_NAME`, and `SERVER_NAME` settings. Every user-facing string
  that named the app, its owner, or the server now reads from configuration.
  The frontend fetches them at runtime from the new public `GET /api/config`, so
  a published image can be rebranded with an environment variable rather than a
  rebuild. A build-time fallback covers the single frame before that answers.
- `GET /manifest.json` is generated from `APP_NAME`, so the PWA's Home Screen
  name follows configuration too.
- `FORWARDED_ALLOW_IPS` is now an environment variable (image default `*`,
  correct only for the documented no-published-port deployment) instead of a
  hardcoded uvicorn flag.

### Changed

- **`SESSION_SECRET` must be at least 32 characters.** A shorter one is now a
  startup error: a guessable signing key is a forgeable owner session.
- **`BASE_URL` is required and has no default.** It is interpolated into the
  plex.tv `forwardUrl`, so a wrong value does not error — it forwards a user's
  freshly minted auth token to another host.
- **`VAPID_SUBJECT` has no default, and is required when either VAPID key is
  set.** It is sent to Apple/Google/Mozilla on every push; a default would put
  the packager's address in every deployment's push traffic. Push with no keys
  at all remains a supported configuration that degrades to Discord.
- Copy that named the owner is written pronoun-free, so any `OWNER_NAME` reads
  correctly.
- Thirteen separate "could not reach the server" messages, in three different
  wordings, became one.
- `pyproject.toml` reads its version dynamically from `pensieve.__version__`.
  It had been stale at 0.1.0 for nine releases while `/health` reported the
  truth.

### Fixed

- Rate limiters are reset between tests. They are process-global singletons, so
  without that an unauthenticated test could see a 429 where it expected a 401,
  depending on collection order.

### Notes for existing deployments

Nothing needs migrating. Cookie names and signing salts are unchanged, so live
sessions survive. If your environment file omits `BASE_URL` or sets a
`SESSION_SECRET` under 32 characters, the container will refuse to start —
which is the point.

## 0.1.0 – 0.8.1 (private)

Built between 2026-08-11 and 2026-08-14:

- Plex OAuth sign-in gated on a share of a specific server, with the PIN minted
  in the user's own browser so Plex's "new device" email names their address.
- Library, storage, and pipeline views over Radarr, Sonarr, and Jellyseerr.
- Deletion as a social process: flag → 14-day veto window → owner approval, with
  a 30-day re-flag cooldown.
- Tiered quality (720p / 1080p self-serve, 4K owner-gated) against configured
  profile ids, never a client-supplied one. Denying a 4K ask still files the
  title at 1080p.
- Discover, search, and a filtered endless-scroll browse view (0.7.0).
- Access requests from accounts with no share, approved into a real plex.tv
  library share (0.5.x), plus an hourly reconcile that revokes anyone Plex no
  longer shares with.
- Plex share state surfaced on both sides: a member who has not accepted their
  invite is told so, and the owner sees who is still waiting (0.8.0).
- Web Push (VAPID) with a Discord fallback measured on delivery, not on whether
  a subscription exists.
