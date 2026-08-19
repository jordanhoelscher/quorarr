/**
 * Response shapes for the backend.
 *
 * These mirror the routes in `pensieve/api/member_routes.py` exactly, including
 * the fields that are nullable upstream — a movie with no file on disk has no
 * `quality`, a pipeline card with nothing in the download queue has no `title`.
 * Optional-vs-nullable is meaningful here: `stale_seconds` is *absent* on a live
 * response and present only on a stale-cache fallback.
 */

export type MediaType = 'movie' | 'series';

/* ------------------------------------------------------------------ storage */

export interface StorageSummary {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  movies_bytes: number;
  tv_bytes: number;
  movie_count: number;
  series_count: number;
  stale_seconds?: number;
}

/* ----------------------------------------------------------------- pipeline */

export type PipelineStatus =
  | 'requested'
  | 'processing'
  | 'partially_available'
  | 'available'
  | 'downloading'
  | 'unknown';

export interface PipelineCard {
  /** Null when nothing in the arr queue matched — jellyseerr carries no title. */
  title: string | null;
  media_type: string;
  /** Opens the Discover detail sheet for this tile. Null on a malformed request. */
  tmdb_id: number | null;
  /**
   * Artwork, from whichever source knew the title first: an absolute URL from
   * the arrs, or a TMDB-relative path from a Discover hint. `posterUrl` takes
   * either. Null falls through to `Poster`'s carved stone.
   */
  poster: string | null;
  requested_by: string | null;
  created_at: string | null;
  status: PipelineStatus;
  /** Percent complete, downloading cards only. */
  pct: number | null;
  /** Raw arr duration, "HH:MM:SS" or "D.HH:MM:SS". */
  timeleft: string | null;
  /** A queue status worth flagging ("stalled", "failed", "warning"). */
  warning: string | null;
  /** Number of active queue rows behind this card ("3 episodes"). */
  count: number | null;
}

export interface PipelineBoard {
  cards: PipelineCard[];
  stale_seconds?: number;
}

/* ------------------------------------------------------------------ library */

export interface MovieRow {
  arr_id: number;
  title: string;
  year: number | null;
  tmdb_id: number | null;
  size_bytes: number;
  quality: string | null;
  /** Vertical resolution of the file on disk, e.g. 1080. Null with no file. */
  resolution: number | null;
  poster: string | null;
  added: string | null;
  has_file: boolean;
  media_type: 'movie';
}

export interface SeasonRow {
  season_number: number;
  size_bytes: number;
  episode_file_count: number;
  monitored: boolean;
  /** Quality-name → file count. Detail endpoint only. */
  qualities?: Record<string, number>;
}

export interface SeriesRow {
  arr_id: number;
  title: string;
  year: number | null;
  tvdb_id: number | null;
  size_bytes: number;
  episode_count: number;
  poster: string | null;
  added: string | null;
  seasons: SeasonRow[];
  media_type: 'series';
}

export interface LibraryList<T> {
  items: T[];
}

/* -------------------------------------------------------------------- flags */

export type FlagState =
  | 'flagged'
  | 'vetoed'
  | 'pending_approval'
  | 'approved'
  | 'denied'
  | 'executed';

export interface Flag {
  id: number;
  media_type: MediaType;
  arr_id: number;
  season_number: number | null;
  title: string;
  size_bytes: number;
  reason: string | null;
  state: FlagState;
  flagged_by_name: string;
  flagged_at: string;
  vetoed_by_name: string | null;
  resolved_at: string | null;
  note: string | null;
}

export interface FlagBoard {
  active: Flag[];
  recent: Flag[];
}

/* ---------------------------------------------------------- quality requests */

export interface QualityRequestResult {
  state: 'auto_triggered' | 'pending_approval' | 'error';
  id?: number;
}

export type QualityRequestState =
  | 'auto_triggered'
  | 'pending_approval'
  | 'approved'
  | 'denied'
  | 'error';

/**
 * A member-facing row from `GET /api/quality-requests`.
 *
 * Deliberately has no `error` field: the backend enumerates its columns to
 * keep the raw upstream exception text owner-only. `state === 'error'` is all
 * a member learns, which is all they need to see a failed badge.
 */
export interface QualityRequest {
  id: number;
  media_type: MediaType;
  arr_id: number;
  season_number: number | null;
  title: string;
  current_quality: string | null;
  requested_quality: string;
  state: QualityRequestState;
  requested_by: number;
  requested_by_name: string;
  created_at: string;
  resolved_at: string | null;
  /** The owner's reason, on a denied request. */
  note: string | null;
}

export interface QualityRequestList {
  items: QualityRequest[];
}

/* ----------------------------------------------------------- admin queue */

/**
 * Shapes for `GET /api/admin/queue` — owner-only, so these carry the raw
 * `error` column that the member-facing routes deliberately strip. The text
 * is an upstream exception, not a friendly message; it is only ever rendered
 * behind the owner gate.
 */

export interface AdminFlag extends Flag {
  /** Full failure text from an execution attempt that didn't land. */
  error: string | null;
}

export interface AdminQualityRequest {
  id: number;
  media_type: string;
  arr_id: number;
  season_number: number | null;
  title: string;
  current_quality: string | null;
  requested_quality: string;
  state: QualityRequestState;
  requested_by_name: string;
  created_at: string;
  note: string | null;
  error: string | null;
}

/**
 * A request from a Plex account that is *not* shared on the server yet.
 *
 * The only queue row whose subject is a person rather than a file, and the
 * only one whose approval reaches outside the homelab — approving calls
 * plex.tv to share the libraries, which cannot be undone from this screen.
 */
export interface AccessRequest {
  id: number;
  plex_account_id: number;
  name: string;
  email: string;
  state: 'pending' | 'approved' | 'denied';
  created_at: string;
  resolved_at: string | null;
  note: string | null;
}

/**
 * A friend's 4K ask from Discover, parked until the owner decides.
 *
 * Nothing has been filed with Jellyseerr when this row exists — that is the
 * whole point of the queue. Both outcomes still file *something*: approving
 * files the 4K profile, denying files the same title at 1080p.
 */
export interface Discover4kRequest {
  id: number;
  media_type: MediaType;
  tmdb_id: number;
  title: string;
  /** JSON array of season numbers, TV only; null for a film or a whole series. */
  seasons_json: string | null;
  requested_by: number;
  requested_by_name: string;
  state: 'pending' | 'approved' | 'denied';
  created_at: string;
  resolved_at: string | null;
  note: string | null;
}

export interface AdminQueue {
  deletions: AdminFlag[];
  quality: AdminQualityRequest[];
  access: AccessRequest[];
  discover_4k: Discover4kRequest[];
  waiting_on_plex: WaitingOnPlex[];
}

/* ---------------------------------------------------------- guest access */

/**
 * Where a denied account's request stands, from
 * `GET /api/guest/access-requests/me`. `none` means they have not asked yet.
 */
export type AccessRequestState = 'none' | 'pending' | 'approved' | 'denied';

export interface AccessRequestStatus {
  state: AccessRequestState;
}

/* ------------------------------------------------------------- action sheet */

/**
 * What the shared action sheet is acting on.
 *
 * A season and a whole series are both `media_type: "series"` to the backend;
 * the distinction is the `seasonNumber` field, so the sheet keeps them as
 * separate `kind`s only to phrase the copy correctly.
 */
export interface ActionTarget {
  kind: 'movie' | 'series' | 'season';
  arrId: number;
  /** Sent verbatim to the backend and shown in the sheet header. */
  title: string;
  sizeBytes: number;
  seasonNumber?: number;
  /** Movies only — hides the 1080p option when the file is already ≥1080. */
  resolution?: number | null;
  /** Movies only — recorded alongside a quality request for the owner's context. */
  quality?: string | null;
}

/* ----------------------------------------------------------------- discover */

/**
 * How a title stands with the server, as `clients/jellyseerr.availability_of`
 * decides it. `requestable` is the only one that gets a Request button, and
 * the only one that shows no badge — absence of a mark means "free to ask".
 */
export type Availability = 'available' | 'partial' | 'requested' | 'requestable';

/** One card on a Discover shelf or in a search result grid. */
export interface DiscoverCard {
  tmdb_id: number;
  title: string;
  year: number | null;
  /** Jellyseerr's own vocabulary: `tv`, not the library views' `series`. */
  media_type: 'movie' | 'tv';
  /** TMDB-relative (`/abc.jpg`); run it through `posterUrl` before use. */
  poster_path: string | null;
  overview: string;
  rating: number | null;
  /** Raw Jellyseerr media status. `availability` is the derived form to render. */
  status: number | null;
  availability: Availability;
}

export interface DiscoverSeason {
  season_number: number;
  name: string;
  episode_count: number;
  air_date: string | null;
  availability: Availability;
  /** False when the season is already on the server or already requested. */
  requestable: boolean;
}

export interface DiscoverDetailCard extends DiscoverCard {
  runtime: number | null;
  /** Null for a movie — distinct from a series that reports no seasons. */
  seasons: DiscoverSeason[] | null;
}

export interface DiscoverShelf {
  id: string;
  title: string;
  items: DiscoverCard[];
  /** Set when this one shelf failed; the rest of the page is still good. */
  error: string | null;
}

export interface DiscoverShelves {
  shelves: DiscoverShelf[];
}

export interface DiscoverResults {
  items: DiscoverCard[];
}

/**
 * A person row from `GET /api/discover/suggest`.
 *
 * Shares no id field with `DiscoverCard` on purpose: `person_id` and `tmdb_id`
 * address different endpoints, so a row carrying the wrong one would fail as a
 * confusing 404 rather than as a type error. `media_type` is the discriminant.
 */
export interface PersonSuggestion {
  person_id: number;
  name: string;
  /** TMDB-relative headshot path; run it through `posterUrl` before use. */
  profile_path: string | null;
  media_type: 'person';
}

/** One row of the search dropdown: something to watch, or someone who was in it. */
export type Suggestion = DiscoverCard | PersonSuggestion;

export interface DiscoverSuggestions {
  items: Suggestion[];
}

/** The answer to `GET /api/discover/person/{person_id}`. */
export interface PersonFilmography {
  person_id: number;
  name: string;
  profile_path: string | null;
  /** Acting credits only, most popular first, capped server-side at 50. */
  items: DiscoverCard[];
}

/**
 * The answer to `POST /api/discover/request`.
 *
 * `state` is only present on the 202 a friend gets when they ask for 4K:
 * nothing was filed, it is waiting on the owner. Its absence means the request
 * went straight to Jellyseerr.
 */
export interface RequestResult {
  ok: boolean;
  request_id?: number | null;
  title: string;
  state?: 'pending_approval';
  id?: number;
}

/** The quality lanes a friend can pick in Discover. 720p is TV-only. */
export type DiscoverQuality = '1080p' | '720p' | '4K';

/* -------------------------------------------------------------- browse */

/** Orderings the backend will honour. A closed vocabulary, mirrored server-side. */
export type BrowseSort = 'trending' | 'popular' | 'newest' | 'upcoming' | 'top_rated';

export type BrowseMedia = 'movie' | 'tv';

export type BrowseDecade = '2020s' | '2010s' | '2000s' | '1990s' | 'older';

/** The browse page's whole state, minus pagination. */
export interface BrowseFilters {
  sort: BrowseSort;
  media: BrowseMedia;
  /** TMDB genre id, only meaningful alongside `media`. */
  genre: number | null;
  decade: BrowseDecade | null;
  minRating: 7 | 8 | null;
}

/** One page from `GET /api/discover/browse`. */
export interface BrowseResults {
  items: DiscoverCard[];
  page: number;
  total_pages: number;
  has_more: boolean;
}

/** One entry from `GET /api/discover/genres/{media_type}`. */
export interface Genre {
  id: number;
  name: string;
}

/* ------------------------------------------------------- plex share state */

/**
 * An approved member whose Plex invite is still unaccepted.
 *
 * Derived live from plex.tv on every queue read, not stored — the row simply
 * stops appearing once they accept.
 */
export interface WaitingOnPlex {
  plex_account_id: number;
  name: string;
  email: string;
  /** Unix epoch *seconds*, straight off the Plex invite. */
  invited_at: number;
}

/** Where a member stands with the Plex server, from `GET /api/me/share`. */
export type ShareState = 'active' | 'pending' | 'none' | 'unknown';

export interface ShareStatus {
  state: ShareState;
}
