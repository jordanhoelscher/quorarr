# Changelog

All notable changes to this project are documented here. The format is loosely
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 0.9.0 predate the public repository; they are summarised rather
than itemised, since their history lives in a private monorepo.

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
