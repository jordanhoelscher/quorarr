# Security Policy

Quorarr is internet-facing by design (your friends sign in from anywhere) and it fronts an
API that can delete files from your disk. Security reports are taken seriously.

## Supported versions

| Version | Supported |
| ------- | --------- |
| Latest release | ✅ |
| Anything older | ❌ |

There is one maintainer and no LTS branch. Fixes land on `main` and go out in the next
release; the remedy for an older version is to upgrade. If you are running a tagged image,
pin to a version you can update promptly rather than one you intend to freeze.

## Reporting a vulnerability

**Please do not open a public issue, discussion, or PR for a security problem.**

Use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. **Report a vulnerability** → fill in the advisory form.

That opens a private advisory only you and the maintainer can see, and it's the channel that
gets checked. If private reporting is unavailable to you for some reason, open a public issue
saying only *"I'd like to report a security issue privately, please enable a channel"* —
with no details of the issue itself.

### What helps

- Affected version (the footer in the UI, or `/health`).
- How you deploy it: image tag or local build, reverse proxy, whether the container port is
  published.
- The exact request/response or steps to reproduce.
- What an attacker gets out of it, and what access they need first (unauthenticated? a guest
  cookie? a member session? owner?). The privilege boundary matters more than the payload.
- A proof of concept if you have one.

### What to expect

- **Acknowledgement: within a few days.** This is a side project maintained in evenings —
  days, not hours. If you've heard nothing in a week, a nudge is fair and welcome.
- Triage and a severity assessment after that, in the advisory thread.
- A fix released as soon as it's ready and verified. High-severity issues jump every queue.
- Credit in the advisory and the release notes, unless you'd rather not be named.
- If a report turns out not to be a vulnerability, you'll get a straight explanation of why
  rather than silence.

### Scope

**In scope**
- Authentication and session handling: the Plex OAuth PIN handshake, cookie signing, the
  guest-cookie flow, session fixation or forgery.
- Authorization: any path where a member reaches an owner-only action (approving deletions,
  4K, access requests, revoking users), or a revoked/guest user reaches a member action.
- Anything that deletes or modifies media without the two-key path (flag → veto window →
  owner approval).
- Injection into upstream calls (Radarr/Sonarr/Jellyseerr/plex.tv), SSRF, XXE.
- Leaking another user's Plex token, session, push subscription, or email.
- Rate-limit bypass on unauthenticated endpoints.

**Out of scope**
- Deployments that publish the container port directly to a network you don't control. The
  app is documented as requiring a reverse proxy and no published port; the image's default
  `FORWARDED_ALLOW_IPS=*` is safe only under that assumption, and it is an environment
  variable precisely so a different topology can narrow it. Reports that rely on breaking
  that assumption are configuration issues, not vulnerabilities.
- Vulnerabilities in Plex, Jellyseerr, Radarr, Sonarr, or the reverse proxy itself. Report
  those upstream.
- Findings that require you to already be the owner. The owner can delete media by design.
- Missing hardening headers, or scanner output with no demonstrated impact.
- Denial of service by simply sending a lot of traffic.

## Operational notes worth knowing

- **`SESSION_SECRET` is not a revocation tool.** Rotating it signs out everyone including
  you. To cut off one person, revoke them in the owner's user list — role and revocation are
  re-read from the database on every request, so it takes effect on their next call.
- **`PLEX_OWNER_TOKEN` is a full-power Plex credential.** It can share and unshare your
  libraries. Keep the env file at mode `600`, never commit it, and rotate it in Plex if it
  is ever exposed.
- **Back up the `/data` volume.** It holds the entire local state: members, flags, quality
  requests, access requests, push subscriptions, and the audit log.
