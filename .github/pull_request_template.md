<!--
Thanks for the PR. If this is a large change that wasn't discussed in an issue first,
please say so up front — it may need a design conversation before review.
-->

## What this changes

<!-- One or two sentences. If it fixes an issue: "Fixes #123". -->

## Why

<!-- The problem, not the patch. What was broken or missing? -->

## How it works

<!-- The approach, and anything a reviewer would otherwise have to reverse-engineer.
     Call out anything deliberate that looks odd. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change (API contract, env var meaning, or database schema)
- [ ] Documentation only
- [ ] Chore / dependency / tooling

## Verification

<!-- What you actually ran, and what you saw. "Should work" is not verification. -->

- [ ] `cd backend && pytest` passes
- [ ] `cd frontend && npm run check` passes
- [ ] `cd frontend && npm test` passes
- [ ] `cd frontend && npm run build` passes
- [ ] New behaviour is covered by a test that fails without this change

Tested against (if applicable):

- Plex:
- Jellyseerr:
- Radarr / Sonarr:

## Checklist

- [ ] Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, …)
- [ ] No new env var without a row in the README table **and** `.env.example`
- [ ] No secrets, tokens, API keys, or real hostnames in the diff
- [ ] Docs updated if user-visible behaviour changed
- [ ] Version bumped in `backend/pensieve/__init__.py` and
      `frontend/package.json` if this warrants a release
- [ ] Change stays within the scope described in CONTRIBUTING.md

## Screenshots

<!-- For UI changes. Phone-width captures preferred — that's how the app is mostly used. -->

## Anything you're unsure about

<!-- Genuinely useful. Name the part you'd most like a second opinion on. -->
