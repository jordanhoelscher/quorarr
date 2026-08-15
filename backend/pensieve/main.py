"""FastAPI app factory."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pensieve import __version__, notify
from pensieve.api.admin_routes import router as admin_router
from pensieve.api.auth_routes import router as auth_router
from pensieve.api.discover_routes import router as discover_router
from pensieve.api.guest_routes import router as guest_router
from pensieve.api.member_routes import router as member_router
from pensieve.clients import plex_tv
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings, get_settings
from pensieve.db import connect, init_db
from pensieve.services import access, deletion

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 3600

#: The Basin's ground colour, mirrored from ``frontend/src/app.css`` so the
#: splash and the OS task-switcher card paint the same black.
THEME_COLOR = "#05070b"
MANIFEST_DESCRIPTION = "What's on the server, what's coming, and what's on its way out."


async def run_sweep_once(settings: Settings, http: CachedHTTP) -> list[dict]:
    """Run a single hourly sweep tick: expire flags, notify the owner of each move.

    Opens and closes its own DB connection -- this runs off a background
    timer, not a request, so it doesn't share a connection with anything
    else. Mirrors the sweep that ``GET /api/flags`` (Task 14) already does
    opportunistically on read; this is the tick that advances the 14-day
    veto window even when nobody's looking at the member view.

    Args:
        settings: App settings, for ``db_path`` and the notification channels.
        http: Shared cached HTTP client, for the Discord fallback.

    Returns:
        The deletion-flag rows that were swept from ``flagged`` into
        ``pending_approval`` this tick (possibly empty).
    """
    now = datetime.now(timezone.utc)
    conn = connect(settings.db_path)
    try:
        moved = deletion.sweep_expired(conn, now)
        # The connection stays open across the notifications: owner_event
        # reads the owner's row and push subscriptions from it.
        for row in moved:
            await notify.owner_event(
                http,
                conn,
                settings,
                title="🗑️ Deletion approval needed",
                body=f"{row['title']} — flagged by {row['flagged_by_name']} "
                     "14 days ago, no vetoes",
            )
    finally:
        conn.close()

    return moved


async def reconcile_shares_once(settings: Settings, http: CachedHTTP) -> list[dict]:
    """Revoke members Plex no longer shares with, and tell the owner.

    Runs on the same hourly tick as the deletion sweep. This is what makes
    "remove their Plex share" also mean "remove their dashboard access" — the
    0.5.2 login gate deliberately stopped asking plex.tv on every login, so
    something has to keep the ``users`` table in step with reality.

    **Fails closed on revocation.** If either plex.tv list can't be read, the
    tick is skipped entirely rather than proceeding with a partial or empty
    picture: an empty share list is indistinguishable from "revoke everyone",
    and getting that wrong locks the whole household out of the dashboard.
    Skipping costs at most an hour's delay on a revocation that was going to
    happen anyway.

    Returns:
        The rows revoked on this tick (possibly empty).
    """
    try:
        entitled = await plex_tv.list_shared_account_ids(http, settings)
        # Union, not intersection: an approved member who hasn't accepted the
        # invite email has no share yet and appears only in this second list.
        entitled |= await plex_tv.list_pending_invite_account_ids(http, settings)
    except UpstreamError:
        logger.warning("share reconciliation skipped: plex.tv unreadable", exc_info=True)
        return []

    now = datetime.now(timezone.utc)
    conn = connect(settings.db_path)
    try:
        revoked = access.revoke_unshared(conn, entitled, settings.plex_owner_account_id, now)
        for user in revoked:
            await notify.owner_event(
                http,
                conn,
                settings,
                title=f"🔒 {settings.app_name} access revoked",
                body=f"{user['name']} — no longer shared on Plex",
            )
    finally:
        conn.close()

    return revoked


async def _sweep_loop(app: FastAPI, settings: Settings) -> None:
    """Background task: run ``run_sweep_once`` every ``SWEEP_INTERVAL_SECONDS`` forever.

    Each tick is wrapped in its own try/except -- a transient DB or upstream
    failure on one tick must not kill the loop for the rest of the app's
    lifetime. ``app.state.http`` is read fresh each iteration (rather than
    captured once) so it stays correct even if it's swapped after startup
    (as tests do).
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        # Independent try/except per job: a deletion-sweep failure must not
        # cost that tick's revocations, or vice versa.
        try:
            await run_sweep_once(settings, app.state.http)
        except Exception:
            logger.exception("hourly sweep tick failed")
        try:
            await reconcile_shares_once(settings, app.state.http)
        except Exception:
            logger.exception("share reconciliation tick failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; settings override for tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Wire the shared HTTP client, init the DB, and start the hourly sweep task."""
        http_client = httpx.AsyncClient(timeout=15)
        app.state.http = CachedHTTP(http_client)

        conn = connect(settings.db_path)
        try:
            init_db(conn)
        finally:
            conn.close()

        sweep_task = asyncio.create_task(_sweep_loop(app, settings))

        yield

        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass

        await http_client.aclose()

    # docs/redoc/openapi are off: this app is public-facing and fronts an API
    # that deletes files from disk. A machine-readable map of every admin
    # route is a free blueprint, and these are framework routes that the
    # per-router session dependency does not cover.
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict:
        """Everything the SPA needs before anyone is signed in.

        Unauthenticated by necessity — the login screen renders from it — so
        the payload is a fixed allowlist of values that are already public:
        the instance's name, who it says approvals go to, the Plex client
        identifier (visible in every hosted auth URL), and the version the
        footer prints. Serving branding from here rather than baking it into
        the bundle is what lets one published image be rebranded by an
        operator without a rebuild.
        """
        return {
            "app_name": settings.app_name,
            "owner_name": settings.owner_name,
            "server_name": settings.server_name,
            "client_id": settings.plex_client_id,
            "version": __version__,
        }

    @app.get("/manifest.json")
    def manifest() -> dict:
        """The PWA manifest, generated so the home-screen name follows config.

        Registered before the static mount, which therefore never gets to
        serve the build's own ``manifest.json`` (that copy survives only for
        the dev server, which has no backend in front of it).
        """
        return {
            "name": settings.app_name,
            "short_name": settings.app_name,
            "description": MANIFEST_DESCRIPTION,
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": THEME_COLOR,
            "theme_color": THEME_COLOR,
            "icons": [
                {
                    "src": f"/icons/icon-{size}.png",
                    "sizes": f"{size}x{size}",
                    "type": "image/png",
                    "purpose": "any maskable",
                }
                for size in (192, 512)
            ],
        }

    app.include_router(auth_router)
    app.include_router(guest_router)
    app.include_router(member_router)
    app.include_router(discover_router)
    app.include_router(admin_router)

    if settings.static_dir:
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")

    return app
