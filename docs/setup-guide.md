# Quorarr setup guide

This walks through every value in `.env.example`, then the reverse proxy, then installing
the app on a phone. Budget about 20 minutes, most of it collecting ids.

**Before you start you need:** a Plex server you own, and Jellyseerr, Radarr, and Sonarr
already running and working. Quorarr drives them; it doesn't replace them.

---

## 1. Secrets you generate

### `SESSION_SECRET`

```bash
openssl rand -hex 32
```

Signs the session, PIN, and guest cookies. Store it once and leave it alone — rotating it
signs out every user including you.

### `PLEX_CLIENT_ID`

```bash
python3 -c "import uuid; print(uuid.uuid4())"
```

Any stable UUID. It identifies the app to plex.tv, and it's what appears in each user's Plex
*Authorized Devices* list. Generate it once; changing it later invalidates logins in flight
and creates a duplicate device entry for everyone.

---

## 2. Plex values

### `PLEX_SERVER_MACHINE_ID`

Your server's `machineIdentifier`. This is the login gate — an account without a share on
*this* machine id cannot sign in, so getting it wrong produces a login that silently loops
back to the sign-in screen.

```bash
curl -s "http://YOUR_PLEX_HOST:32400/identity"
```

Returns XML containing `machineIdentifier="…"` (a 40-character hex string). To pull just the
value:

```bash
curl -s "http://YOUR_PLEX_HOST:32400/identity" \
  | grep -o 'machineIdentifier="[^"]*"' | cut -d'"' -f2
```

`/identity` normally answers without a token. If yours doesn't, append `?X-Plex-Token=YOUR_TOKEN`.

### Getting your `X-Plex-Token`

You need this for the next two values.

1. Open Plex Web, play any item, and open **⋮ → Get Info → View XML**.
2. The token is the `X-Plex-Token=` parameter in the URL of the page that opens.

(Plex's own article, "Finding an authentication token / X-Plex-Token", documents this and a
couple of alternatives.) Treat it like a password — it can share and unshare your libraries.

### `PLEX_OWNER_ACCOUNT_ID`

Your numeric plex.tv account id — the account Quorarr grants the owner role to.

```bash
curl -s -H "Accept: application/json" -H "X-Plex-Token: YOUR_TOKEN" \
  https://plex.tv/api/v2/user \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"
```

It's an integer, not your username or email.

### `PLEX_OWNER_TOKEN`

The same token from above. Optional, but without it:

- approving an access request answers **502** (Quorarr can't call the plex.tv share API), and
- the hourly reconcile that revokes people you've un-shared in Plex can't run.

Set it unless you have a reason not to, and keep the env file at mode `600`.

---

## 3. Jellyseerr, Radarr, Sonarr

### API keys

Each app: **Settings → General → API Key**. Copy them into `JELLYSEERR_API_KEY`,
`RADARR_API_KEY`, `SONARR_API_KEY`.

### URLs

Defaults assume the apps are on the same Docker network under their conventional names:

```
RADARR_URL=http://radarr:7878
SONARR_URL=http://sonarr:8989
JELLYSEERR_URL=http://jellyseerr:5055
```

If your containers are named differently, or you reach them by IP, override accordingly.
`docker compose exec quorarr python3 -c "import urllib.request;print(urllib.request.urlopen('http://radarr:7878').status)"`
is a quick way to prove the name resolves from inside the container.

### Quality profile ids

Quorarr never accepts a profile id from a browser. It maps the vocabulary `720p` / `1080p` /
`4K` onto ids you configure, so every request lands in a lane you chose. Look yours up:

```bash
# Radarr
curl -s -H "X-Api-Key: YOUR_RADARR_KEY" \
  http://radarr:7878/api/v3/qualityprofile | jq '.[] | {id, name}'

# Sonarr
curl -s -H "X-Api-Key: YOUR_SONARR_KEY" \
  http://sonarr:8989/api/v3/qualityprofile | jq '.[] | {id, name}'
```

No `jq`? Swap the last part for `| python3 -m json.tool` and read off `"id"` / `"name"`.
Running from outside the Docker network? Use the published host and port instead
(`http://192.168.1.10:7878`, etc.).

Typical output:

```json
{ "id": 1, "name": "Any" }
{ "id": 4, "name": "HD-1080p" }
{ "id": 5, "name": "Ultra-HD" }
{ "id": 6, "name": "HD-720p" }
```

Map them:

| Variable | From | Required |
| -------- | ---- | -------- |
| `RADARR_PROFILE_HD_ID` | Radarr's 1080p profile | yes, must be > 0 |
| `RADARR_PROFILE_4K_ID` | Radarr's 4K profile | yes, must be > 0 |
| `SONARR_PROFILE_HD_ID` | Sonarr's 1080p profile | yes, must be > 0 |
| `SONARR_PROFILE_4K_ID` | Sonarr's 4K profile | yes, must be > 0 |
| `SONARR_PROFILE_720_ID` | Sonarr's 720p profile | optional; `0` disables the lane |

If you don't run a separate 4K profile, point the 4K ids at your best profile — but be aware
that the 4K lane is what the owner-approval gate protects, so it should be the one that costs
you disk.

---

## 4. Web Push (optional but recommended)

Generate a VAPID keypair:

```bash
npx web-push generate-vapid-keys
```

```
Public Key:
BE...long-base64url-string...
Private Key:
kY...shorter-base64url-string...
```

Put them in `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`, and set `VAPID_SUBJECT` to your own
`mailto:` address — push services expect a contactable sender and some will reject or
throttle an unfamiliar one. Setting either key without a subject is a **startup error**: an
unset `sub` claim is rejected at send time, so the alternative would be silent
non-delivery discovered hours later.

No Node? The `py_vapid` package (a dependency of `pywebpush`, which Quorarr already uses) can
generate a pair:

```bash
pip install py-vapid
vapid --gen                     # writes private_key.pem / public_key.pem
vapid --applicationServerKey    # prints the base64url public key
```

Leave the VAPID vars empty and push is simply off: the app works, owner events go to Discord
if you configured a webhook, and members get no notifications.

**`DISCORD_WEBHOOK_URL`** (optional): Discord → Server Settings → Integrations → Webhooks →
New Webhook → Copy URL. It's used only as a fallback when a push to the owner didn't land.

---

## 5. Paths

- `MEDIA_MOUNT` (default `/media`) — the path *inside the container* whose filesystem the
  storage view measures. Mount your library there read-only. If your library spans several
  filesystems, point this at the one that actually fills up.
- `DB_PATH` (default `/data/pensieve.db`) — the SQLite file. Its directory must be a mounted
  volume, or every container recreate wipes your members, flags, and push subscriptions.
  Back this volume up. The filename keeps the project's original name on purpose: changing
  the default would silently orphan the database of every deploy that upgraded into it.

---

## 6. Start it

```bash
docker compose up -d
docker logs -f quorarr
```

A missing required variable produces a `pydantic` validation error at startup naming the
field — that's intentional, not a crash to work around.

Health check (there's no published port, so ask the container):

```bash
docker exec quorarr python3 -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health').read())"
# {"status":"ok","version":"0.9.0"}
```

---

## 7. Reverse proxy

**Required.** Session cookies are `Secure`, so sign-in doesn't work over plain HTTP, and the
container must stay unpublished (see the warning in `docker-compose.example.yml`). Whatever
proxy you use must be able to reach `quorarr:8000` — in practice, be on the same Docker
network.

### Caddy

```caddyfile
media.example.com {
    reverse_proxy quorarr:8000
}
```

Caddy sets `X-Forwarded-For` / `X-Forwarded-Proto` and gets a certificate on its own. Put the
Caddy container on the same network as Quorarr (`networks: [media-net]`).

### nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name media.example.com;

    ssl_certificate     /etc/letsencrypt/live/media.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/media.example.com/privkey.pem;

    location / {
        proxy_pass http://quorarr:8000;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Poster grids can be chatty on first paint.
        proxy_read_timeout 60s;
    }
}

server {
    listen 80;
    server_name media.example.com;
    return 301 https://$host$request_uri;
}
```

`X-Forwarded-For` matters: without it, every login looks like it came from the proxy and the
per-IP login limiter throttles all your users together.

### Traefik

Labels on the `quorarr` service in your compose file:

```yaml
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.quorarr.rule=Host(`media.example.com`)"
      - "traefik.http.routers.quorarr.entrypoints=websecure"
      - "traefik.http.routers.quorarr.tls.certresolver=letsencrypt"
      - "traefik.http.services.quorarr.loadbalancer.server.port=8000"
```

Traefik forwards the standard `X-Forwarded-*` headers by default.

### DNS and `BASE_URL`

Point `media.example.com` at your proxy, and set `BASE_URL=https://media.example.com` — no
trailing slash, and it must match exactly what users type. Plex's OAuth redirect comes back to
`<BASE_URL>/auth/callback`, so a mismatch means sign-in dead-ends after the Plex screen.

---

## 8. First run

1. Open the site and **sign in with Plex** as yourself. Because your Plex account id matches
   `PLEX_OWNER_ACCOUNT_ID`, you get the owner role: the Approvals tab appears.
2. Have someone you already share the library with sign in. They land as a member.
3. Have someone you *don't* share with try. They get an honest "you're not on this server"
   screen with a **Request access** button; approving it in your Approvals tab sends them a
   plex.tv invite to every library section and creates their member row.

Sanity checks worth doing on day one:

- Flag something for deletion as a member, then veto it as someone else.
- Ask for a 4K title as a member and confirm it lands in your queue with nothing filed
  upstream until you approve.
- Deny that 4K ask and confirm the title still arrives in Jellyseerr at 1080p.

---

## 9. Install on a phone

The app is a PWA; there's nothing in an app store.

### iOS / iPadOS (16.4+)

1. Open the site **in Safari** (not Chrome — only Safari can install).
2. **Share → Add to Home Screen → Add.**
3. Open Quorarr **from the Home Screen icon**, then turn on notifications with the bell in
   the top bar.

**Web Push on iOS only works from an installed copy.** Until you install, the app replaces
the bell with that instruction rather than offering a button that can't work. If you delete
the Home Screen icon, the subscription goes with it.

### Android

Chrome offers "Install app" / "Add to Home screen" from the ⋮ menu, or via the install prompt
in the address bar. Push works in the browser tab too, but the installed copy behaves better.

### Desktop

Chrome and Edge show an install icon in the address bar. Firefox has no install, but the site
works normally in a tab.

---

## Troubleshooting

**Sign-in loops back to the login screen.**
Almost always `PLEX_SERVER_MACHINE_ID`. Quorarr checks the signed-in account's Plex resources
for a share on that exact machine id; a stale or wrong id matches nothing, so every login is
politely refused. Re-run the `/identity` command against the server you actually share.

**Sign-in dead-ends after the Plex authorization page.**
`BASE_URL` doesn't match the origin the browser is on (http vs https, `www`, wrong host, or a
trailing slash).

**Nothing happens when I sign in — no cookie is set.**
You're on plain HTTP. Cookies are issued `Secure`; finish the TLS setup.

**Approving an access request returns 502.**
`PLEX_OWNER_TOKEN` is unset or expired. That approval is a real plex.tv share call.

**"720p lane not configured".**
`SONARR_PROFILE_720_ID` is `0`. That's a deliberate refusal — quietly filing the 1080p profile
would spend disk nobody agreed to. Set the id, or leave the lane off.

**"couldn't map your account".**
Quorarr couldn't resolve that Plex account to a Jellyseerr user, even after asking Jellyseerr
to import from Plex. Check that Jellyseerr can see your Plex server and that its user import
works; Quorarr will never file a request under the API key's owner instead.

**Container won't start, log shows a `pydantic` validation error.**
The named field is missing or invalid — most commonly a quality profile id left at `0`.

**The storage view shows the wrong disk.**
`MEDIA_MOUNT` points at a path inside the container that lives on a different filesystem than
your library. Check what you mounted where.

**Everyone gets rate-limited at once.**
Your proxy isn't passing `X-Forwarded-For`, so every login looks like one client.
