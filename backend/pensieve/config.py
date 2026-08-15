"""Environment-driven settings."""
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    #: Signs the session, PIN, and guest cookies. A short secret is a
    #: forgeable session: the floor is enforced here rather than documented,
    #: because the failure mode of a weak one ("anybody can mint an owner
    #: cookie") is silent until it is catastrophic. 32 characters is the
    #: length ``secrets.token_urlsafe(32)`` produces at minimum.
    session_secret: str = Field(min_length=32)
    #: Product name shown in the UI, the PWA manifest, the FastAPI title,
    #: and — most visibly to a stranger — the entry this app gets in every
    #: user's Plex *Authorized Devices* list. Configurable so one codebase
    #: can be deployed under whatever the operator calls their instance.
    app_name: str = "Quorarr"
    #: How the app refers to whoever approves things ("Sent to {owner_name}
    #: for approval"). Substituted into copy that is deliberately written
    #: pronoun-free, so any name — or a role word like the default — reads
    #: correctly.
    owner_name: str = "the owner"
    #: Optional subtitle under the wordmark on the login screen — what the
    #: operator calls the *server*, as distinct from the app. Empty (the
    #: default) simply omits the line rather than printing a placeholder.
    server_name: str = ""
    #: Public origin this deployment is reached on. Required, and
    #: deliberately without a default: it is interpolated into the plex.tv
    #: ``forwardUrl``, so a wrong value does not error, it sends a friend's
    #: freshly minted auth token to somebody else's host.
    base_url: str
    plex_client_id: str
    plex_server_machine_id: str
    plex_owner_account_id: int
    plex_owner_token: str = ""
    radarr_url: str = "http://radarr:7878"
    radarr_api_key: str
    sonarr_url: str = "http://sonarr:8989"
    sonarr_api_key: str
    jellyseerr_url: str = "http://jellyseerr:5055"
    jellyseerr_api_key: str
    #: The four audited lanes every Discover request is filed against.
    #: ``gt=0`` because 0 is not a profile id, it is an unset environment
    #: variable -- and ``services.discover.profile_for`` only guards the
    #: optional 720p lane against that, so an unset required id would sail
    #: through as ``profileId: 0`` and file against whatever the arr made of
    #: it. Refusing to boot is the honest answer to a half-configured deploy.
    radarr_profile_hd_id: int = Field(gt=0)
    radarr_profile_4k_id: int = Field(gt=0)
    sonarr_profile_hd_id: int = Field(gt=0)
    sonarr_profile_4k_id: int = Field(gt=0)
    #: Sonarr's HD-720p profile -- the "space-saver" lane a friend can pick
    #: for sitcoms and background TV. Unset (0) means the lane does not
    #: exist on this deploy: ``services.discover.profile_for`` returns None
    #: and the request route answers 502 rather than quietly filing the
    #: show at 1080p, which would be a worse lie than an error.
    sonarr_profile_720_id: int = 0
    discord_webhook_url: str = ""
    #: VAPID keypair for Web Push. Empty (the default) disables push
    #: entirely -- ``push.send_to_user`` returns 0 and every owner-facing
    #: notification falls back to Discord, so an unconfigured deploy
    #: degrades rather than erroring.
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    #: VAPID ``sub`` claim — a ``mailto:`` or ``https:`` contact the push
    #: services (Apple, Google, Mozilla) use to reach the operator about a
    #: misbehaving deployment. Empty by default *on purpose*: it is sent to
    #: third parties on every push, so shipping anyone's real address as a
    #: default would put the packager's inbox in every stranger's push
    #: traffic. Required only when push is enabled (see the validator).
    vapid_subject: str = ""
    media_mount: str = "/media"
    db_path: str = "/data/pensieve.db"
    static_dir: str = ""

    @model_validator(mode="after")
    def _push_config_is_complete(self) -> "Settings":
        """Refuse a half-configured push setup rather than degrading oddly.

        Push is opt-in: with no keys at all, ``push.send_to_user`` returns 0
        and every owner notification falls back to Discord. That is a
        supported deployment. What is *not* supported is keys without a
        subject — pywebpush would sign a VAPID header with no ``sub`` claim
        and the push services reject it at send time, which surfaces as
        silent non-delivery hours later instead of a boot failure now.
        """
        if (self.vapid_private_key or self.vapid_public_key) and not self.vapid_subject:
            raise ValueError(
                "VAPID_SUBJECT is required when VAPID keys are set "
                "(e.g. mailto:you@example.com)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Singleton settings from environment."""
    return Settings()
