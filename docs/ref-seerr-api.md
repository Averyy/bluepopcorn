# Seerr API Reference

Seerr (v3.1) -- fork of Overseerr (discontinued) and Jellyseerr. API is backwards-compatible with Overseerr.

Base URL from `SEERR_URL` env var. All endpoints prefixed with `/api/v1`. 157 total endpoints.

**IMPORTANT: Seerr 3.x rejects `+` for spaces in query params -- must use `%20` encoding.** httpx defaults to `+` encoding which causes 400 errors.

---

## Status Enums

**MediaStatus** (on `mediaInfo.status` in search results, media items -- from source `server/constants/media.ts`):
- 1 = UNKNOWN
- 2 = PENDING
- 3 = PROCESSING
- 4 = PARTIALLY_AVAILABLE
- 5 = AVAILABLE
- 6 = BLOCKLISTED (Seerr 3.0+, not in OpenAPI spec yet)
- 7 = DELETED (Seerr 3.0+, was 6 in Overseerr spec)
- absent = not tracked (never requested)

Note: The OpenAPI spec still says 6=DELETED (inherited from Overseerr). The actual source code has BLOCKLISTED=6, DELETED=7. Our code uses `MediaStatus` enum that should match source.

**MediaRequestStatus** (on `request.status` -- from source `server/constants/media.ts`):
- 1 = PENDING APPROVAL
- 2 = APPROVED
- 3 = DECLINED
- 4 = FAILED (Seerr 3.0+, not in OpenAPI spec)
- 5 = COMPLETED (Seerr 3.0+, not in OpenAPI spec)

---

## Authentication

**Our bot uses `X-Api-Key` header auth** — set `SEERR_API_KEY` in `.env`, passed as a request header on every call. No session cookies needed. The endpoints below are reference for other auth methods.

- `POST /api/v1/auth/local` (public) -- sign in with email/password
  - Body: `{email, password}`
  - Returns `User` object + `connect.sid` session cookie
  - 401 on expired session -- re-authenticate and retry

- `POST /api/v1/auth/plex` (public) -- sign in with Plex token
  - Body: `{authToken}`

- `POST /api/v1/auth/jellyfin` (public) -- sign in with Jellyfin credentials
  - Body: `{username, password, hostname, email, serverType}`

- `GET /api/v1/auth/me` -- get current authenticated user

- `POST /api/v1/auth/logout` -- sign out, clear session

- `POST /api/v1/auth/reset-password` (public) -- send reset email
  - Body: `{email}`

- `POST /api/v1/auth/reset-password/{guid}` (public) -- complete reset
  - Body: `{password}`

## Search

- `GET /api/v1/search` -- search movies, TV shows, or people
  - Params: `query` (required), `page`, `language`
  - Returns `{page, totalPages, totalResults, results: [MovieResult|TvResult|PersonResult]}`
  - MovieResult: `{id, mediaType:"movie", title, releaseDate, overview, voteAverage, posterPath, originalLanguage, mediaInfo?}`
  - TvResult: `{id, mediaType:"tv", name, firstAirDate, overview, voteAverage, posterPath, originalLanguage, mediaInfo?}`
  - `mediaInfo.status` tells availability (see enum above)

- `GET /api/v1/search/keyword` -- search TMDB keywords
  - Params: `query`, `page`

- `GET /api/v1/search/company` -- search production companies
  - Params: `query`, `page`

## Requests

- `POST /api/v1/request` -- create request
  - Body: `{mediaType: "movie"|"tv", mediaId: <tmdb_id>}`
  - Optional: `seasons` (array of ints or `"all"`), `is4k`, `serverId`, `profileId`, `rootFolder`, `languageProfileId`, `userId`, `tvdbId`
  - Omit `seasons` for TV -- defaults to all seasons
  - Returns `MediaRequest` object (201)

- `GET /api/v1/request` -- list requests
  - Params: `take`, `skip`, `filter`, `sort`, `sortDirection`, `requestedBy`, `mediaType`
  - `filter`: all, approved, available, pending, processing, unavailable, failed, deleted, completed
  - `sort`: added (default), modified
  - `sortDirection`: asc, desc
  - `mediaType`: all, movie, tv
  - Returns `{pageInfo, results: [MediaRequest]}`
  - MediaRequest: `{id, status, media: MediaInfo, createdAt, updatedAt, requestedBy, is4k, type, seasons, seasonCount, tags, ...}`

- `GET /api/v1/request/count` -- request counts
  - Returns `{total, movie, tv, pending, approved, declined, processing, available, completed}`

- `GET /api/v1/request/{requestId}` -- get single request

- `PUT /api/v1/request/{requestId}` -- modify request
  - Body: `{mediaType, seasons, is4k, serverId, profileId, rootFolder, languageProfileId, userId}`

- `DELETE /api/v1/request/{requestId}` -- cancel/delete request

- `POST /api/v1/request/{requestId}/{status}` -- approve or decline
  - `status`: approve, decline

- `POST /api/v1/request/{requestId}/retry` -- retry failed request

## Media

- `GET /api/v1/media` -- list media items
  - Params: `take`, `skip`, `filter`, `sort`
  - `filter`: all, available, partial, allavailable, processing, pending, deleted
  - `sort`: added (default), modified, mediaAdded
  - Returns `{pageInfo, results: [MediaInfo]}`
  - MediaInfo: `{id, tmdbId, tvdbId, status, status4k, mediaType, mediaAddedAt, createdAt, updatedAt, externalServiceSlug, downloadStatus, serviceUrl, ...}`
  - Needs title resolution via detail endpoint (no title in MediaInfo)

- `DELETE /api/v1/media/{mediaId}` -- delete media item (requires MANAGE_REQUESTS)

- `DELETE /api/v1/media/{mediaId}/file` -- delete media file from disk
  - Params: `is4k`

- `GET /api/v1/media/{mediaId}/watch_data` -- get watch data for media item

- `POST /api/v1/media/{mediaId}/{status}` -- update media status
  - `status`: available, partial, processing, pending, unknown, deleted
  - Body: `{is4k}` (optional)

## Movie Details

- `GET /api/v1/movie/{tmdbId}` -- full movie details
  - Params: `language`
  - Returns: `{id, title, overview, releaseDate, voteAverage, voteCount, posterPath, backdropPath, runtime, genres, relatedVideos, mediaInfo, credits, externalIds, collection, ...}`

- `GET /api/v1/movie/{tmdbId}/ratings` -- basic ratings (RT only)

- `GET /api/v1/movie/{tmdbId}/ratingscombined` -- RT + IMDB ratings
  - Returns `{rt: {title, year, url, criticsScore, criticsRating, audienceScore, audienceRating}, imdb: {title, year, url, criticsScore, criticsScoreCount}}`
  - `rt.criticsRating`: "Rotten", "Fresh", or "Certified Fresh"
  - `rt.audienceRating`: "Spilled" or "Upright"
  - `imdb.criticsScore`: float (e.g. 8.4), `imdb.criticsScoreCount`: vote count

- `GET /api/v1/movie/{tmdbId}/recommendations` -- recommended movies
  - Params: `page`, `language`
  - Returns `{page, totalPages, totalResults, results: [MovieResult]}`

- `GET /api/v1/movie/{tmdbId}/similar` -- similar movies
  - Params: `page`, `language`

## TV Details

- `GET /api/v1/tv/{tvId}` -- full TV details
  - Params: `language`
  - Returns: `{id, name, overview, firstAirDate, voteAverage, voteCount, posterPath, seasons, numberOfEpisodes, numberOfSeason, genres, relatedVideos, mediaInfo, credits, externalIds, ...}`

- `GET /api/v1/tv/{tvId}/season/{seasonNumber}` -- season details with episode list
  - Params: `language`

- `GET /api/v1/tv/{tvId}/ratings` -- basic ratings (RT only)

- `GET /api/v1/tv/{tvId}/recommendations` -- recommended TV series
  - Params: `page`, `language`

- `GET /api/v1/tv/{tvId}/similar` -- similar TV series
  - Params: `page`, `language`

### Trailer Videos

In `relatedVideos[]` on movie/TV detail responses:
- Filter `type` for "Trailer" or "Teaser" (other types: Clip, Featurette, Opening Credits, Behind the Scenes, Bloopers)
- `site` is always "YouTube"
- Each video has both `url` (full YouTube URL) and `key` (video ID)
- Build short URL: `https://youtu.be/{key}`

## Discovery

- `GET /api/v1/discover/trending` -- trending movies and TV (mixed)
  - Params: `page`, `language`

- `GET /api/v1/discover/movies` -- discover movies with filters
  - Params: `page`, `language`, `genre` (TMDB genre ID), `studio`, `keywords` (comma-separated IDs), `excludeKeywords`, `sortBy` (e.g. `popularity.desc`), `primaryReleaseDateGte`, `primaryReleaseDateLte`, `withRuntimeGte`, `withRuntimeLte`, `voteAverageGte`, `voteAverageLte`, `voteCountGte`, `voteCountLte`, `watchRegion`, `watchProviders` (pipe-separated), `certification`, `certificationGte`, `certificationLte`, `certificationCountry`, `certificationMode` (exact|range)

- `GET /api/v1/discover/tv` -- discover TV shows with filters
  - Params: `page`, `language`, `genre`, `network`, `keywords`, `excludeKeywords`, `sortBy`, `firstAirDateGte`, `firstAirDateLte`, `withRuntimeGte`, `withRuntimeLte`, `voteAverageGte`, `voteAverageLte`, `voteCountGte`, `voteCountLte`, `watchRegion`, `watchProviders`, `status`, `certification`, `certificationGte`, `certificationLte`, `certificationCountry`, `certificationMode` (exact|range)

- `GET /api/v1/discover/movies/upcoming` -- upcoming movies
  - Params: `page`, `language`

- `GET /api/v1/discover/tv/upcoming` -- upcoming TV shows
  - Params: `page`, `language`

- `GET /api/v1/discover/movies/genre/{genreId}` -- movies by genre
  - Params: `page`, `language`

- `GET /api/v1/discover/tv/genre/{genreId}` -- TV by genre
  - Params: `page`, `language`

- `GET /api/v1/discover/movies/studio/{studioId}` -- movies by studio
  - Params: `page`, `language`

- `GET /api/v1/discover/tv/network/{networkId}` -- TV by network
  - Params: `page`, `language`

- `GET /api/v1/discover/movies/language/{language}` -- movies by original language
  - Params: `page`, `language`

- `GET /api/v1/discover/tv/language/{language}` -- TV by original language
  - Params: `page`, `language`

- `GET /api/v1/discover/keyword/{keywordId}/movies` -- movies by keyword
  - Params: `page`, `language`

- `GET /api/v1/discover/watchlist` -- Plex watchlist
  - Params: `page`

- `GET /api/v1/discover/genreslider/movie` -- genre slider data for movies
  - Params: `language`

- `GET /api/v1/discover/genreslider/tv` -- genre slider data for TV
  - Params: `language`

## People

- `GET /api/v1/person/{personId}` -- actor/director details
  - Params: `language`
  - Returns: `{id, name, biography, birthday, deathday, knownForDepartment, profilePath, imdbId, ...}`

- `GET /api/v1/person/{personId}/combined_credits` -- filmography
  - Params: `language`

## Collections

- `GET /api/v1/collection/{collectionId}` -- movie collection details (e.g. all Fast & Furious)
  - Params: `language`

## Genres

- `GET /api/v1/genres/movie` -- list all TMDB movie genre IDs/names (19 genres)
  - Params: `language`
  - Returns `[{id, name}, ...]`

- `GET /api/v1/genres/tv` -- list all TMDB TV genre IDs/names (16 genres)
  - Params: `language`
  - Returns `[{id, name}, ...]`

## Certifications

- `GET /api/v1/certifications/movie` -- movie ratings by country
- `GET /api/v1/certifications/tv` -- TV ratings by country
  - Returns `{certifications: {US: [{certification, meaning, order}, ...], ...}}`
  - US movie: NR, G, PG, PG-13, R, NC-17
  - US TV: NR, TV-Y, TV-Y7, TV-G, TV-PG, TV-14, TV-MA

## Blocklist

- `GET /api/v1/blocklist` -- list blocklisted items
  - Params: `take`, `skip`, `search`, `filter` (all|manual|blocklistedTags)

- `GET /api/v1/blocklist/{tmdbId}` -- check if media is blocklisted

- `POST /api/v1/blocklist` -- add to blocklist

- `DELETE /api/v1/blocklist/{tmdbId}` -- remove from blocklist

Note: `/api/v1/blacklist/*` endpoints are deprecated aliases for the above.

## Watchlist

- `POST /api/v1/watchlist` -- add media to watchlist

- `DELETE /api/v1/watchlist/{tmdbId}` -- remove from watchlist

## Service Status

- `GET /api/v1/status` (public) -- Seerr health/version check
  - Returns `{version, commitTag, updateAvailable, commitsBehind, restartRequired}`

- `GET /api/v1/status/appdata` (public) -- app data directory status

- `GET /api/v1/service/radarr` -- list Radarr instances (non-sensitive)

- `GET /api/v1/service/radarr/{radarrId}` -- Radarr quality profiles and root folders

- `GET /api/v1/service/sonarr` -- list Sonarr instances (non-sensitive)

- `GET /api/v1/service/sonarr/{sonarrId}` -- Sonarr quality profiles and root folders

- `GET /api/v1/service/sonarr/lookup/{tmdbId}` -- Sonarr series data (seasons, episode counts, monitored status)

**NOTE: Live download status is not reliably available through Seerr in our setup.**
- `downloadStatus` on media items is almost always `[]` -- only populated during an active transfer, empty otherwise.
- Sonarr lookup returns series metadata (episode counts, monitored) but NOT queue/download progress.
- There is no `/service/radarr/lookup/{tmdbId}` equivalent (404).
- To get real download progress (% complete, ETA, queue position), we'd need to hit Radarr (`http://192.168.2.138:7878`) and Sonarr directly, bypassing Seerr. Needs further investigation.

## TMDB Reference Data

- `GET /api/v1/languages` -- all languages supported by TMDB
- `GET /api/v1/regions` -- all regions supported by TMDB
- `GET /api/v1/studio/{studioId}` -- movie studio details
- `GET /api/v1/network/{networkId}` -- TV network details
- `GET /api/v1/keyword/{keywordId}` -- keyword details
- `GET /api/v1/watchproviders/regions` -- streaming provider regions
- `GET /api/v1/watchproviders/movies` -- movie streaming providers (params: `watchRegion`)
- `GET /api/v1/watchproviders/tv` -- TV streaming providers (params: `watchRegion`)
- `GET /api/v1/backdrops` (public) -- random trending backdrops (UI use)

## Issues

- `GET /api/v1/issue` -- list all issues
  - Params: `take`, `skip`, `sort` (added|modified), `filter` (all|open|resolved), `requestedBy`

- `GET /api/v1/issue/count` -- issue counts

- `GET /api/v1/issue/{issueId}` -- get issue

- `POST /api/v1/issue` -- create issue
  - Body: `{issueType, message, mediaId}`

- `DELETE /api/v1/issue/{issueId}` -- delete issue

- `POST /api/v1/issue/{issueId}/{status}` -- update issue status (open/resolved)

- `POST /api/v1/issue/{issueId}/comment` -- add comment
  - Body: `{message}`

- `GET /api/v1/issueComment/{commentId}` -- get comment
- `PUT /api/v1/issueComment/{commentId}` -- update comment
- `DELETE /api/v1/issueComment/{commentId}` -- delete comment

## Override Rules

- `GET /api/v1/overrideRule` -- list override rules
- `POST /api/v1/overrideRule` -- create override rule
- `PUT /api/v1/overrideRule/{ruleId}` -- update rule
- `DELETE /api/v1/overrideRule/{ruleId}` -- delete rule

---

## User Management

- `GET /api/v1/user` -- list all users
  - Params: `take`, `skip`, `sort` (created|updated|requests|displayname), `q` (search), `includeIds`

- `POST /api/v1/user` -- create user
  - Body: `{email, username, permissions}`

- `PUT /api/v1/user` -- batch update users
  - Body: `{ids, permissions}`

- `GET /api/v1/user/{userId}` -- get user

- `PUT /api/v1/user/{userId}` -- update user

- `DELETE /api/v1/user/{userId}` -- delete user

- `POST /api/v1/user/import-from-plex` -- import Plex users
  - Body: `{plexIds}`

- `POST /api/v1/user/import-from-jellyfin` -- import Jellyfin users
  - Body: `{jellyfinUserIds}`

- `GET /api/v1/user/{userId}/requests` -- user's requests (params: `take`, `skip`)
- `GET /api/v1/user/{userId}/quota` -- user's request quota
- `GET /api/v1/user/{userId}/watchlist` -- user's Plex watchlist (params: `page`)
- `GET /api/v1/user/{userId}/watch_data` -- user's watch history

### User Settings

- `GET /api/v1/user/{userId}/settings/main` -- general settings
- `POST /api/v1/user/{userId}/settings/main` -- update general settings
- `GET /api/v1/user/{userId}/settings/password` -- password page info
- `POST /api/v1/user/{userId}/settings/password` -- change password
  - Body: `{currentPassword, newPassword}`
- `GET /api/v1/user/{userId}/settings/permissions` -- permission settings
- `POST /api/v1/user/{userId}/settings/permissions` -- update permissions
  - Body: `{permissions}`
- `GET /api/v1/user/{userId}/settings/notifications` -- notification settings
- `POST /api/v1/user/{userId}/settings/notifications` -- update notification settings
- `POST /api/v1/user/{userId}/settings/linked-accounts/plex` -- link Plex account
  - Body: `{authToken}`
- `DELETE /api/v1/user/{userId}/settings/linked-accounts/plex` -- unlink Plex
- `POST /api/v1/user/{userId}/settings/linked-accounts/jellyfin` -- link Jellyfin account
  - Body: `{username, password}`
- `DELETE /api/v1/user/{userId}/settings/linked-accounts/jellyfin` -- unlink Jellyfin

### Push Subscriptions

- `POST /api/v1/user/registerPushSubscription` -- register web push
  - Body: `{endpoint, auth, p256dh, userAgent}`
- `GET /api/v1/user/{userId}/pushSubscriptions` -- list push subscriptions
- `GET /api/v1/user/{userId}/pushSubscription/{endpoint}` -- get subscription
- `DELETE /api/v1/user/{userId}/pushSubscription/{endpoint}` -- delete subscription

---

## Settings (Admin)

### Main

- `GET /api/v1/settings/main` -- main settings
- `POST /api/v1/settings/main` -- update main settings
- `POST /api/v1/settings/main/regenerate` -- regenerate API key
- `GET /api/v1/settings/public` (public) -- public/non-sensitive settings
- `POST /api/v1/settings/initialize` -- first-run setup
- `GET /api/v1/settings/about` -- server stats/version

### Network

- `GET /api/v1/settings/network` -- network settings
- `POST /api/v1/settings/network` -- update network settings

### Metadata

- `GET /api/v1/settings/metadatas` -- metadata settings
- `PUT /api/v1/settings/metadatas` -- update metadata settings
- `POST /api/v1/settings/metadatas/test` -- test metadata provider
  - Body: `{tmdb, tvdb}`

### Plex

- `GET /api/v1/settings/plex` -- Plex settings
- `POST /api/v1/settings/plex` -- update Plex settings
- `GET /api/v1/settings/plex/library` -- Plex libraries (params: `sync`, `enable`)
- `GET /api/v1/settings/plex/sync` -- Plex sync status
- `POST /api/v1/settings/plex/sync` -- start/cancel Plex sync
  - Body: `{cancel, start}`
- `GET /api/v1/settings/plex/devices/servers` -- available Plex servers
- `GET /api/v1/settings/plex/users` -- Plex users

### Jellyfin

- `GET /api/v1/settings/jellyfin` -- Jellyfin settings
- `POST /api/v1/settings/jellyfin` -- update Jellyfin settings
- `GET /api/v1/settings/jellyfin/library` -- Jellyfin libraries (params: `sync`, `enable`)
- `GET /api/v1/settings/jellyfin/sync` -- sync status
- `POST /api/v1/settings/jellyfin/sync` -- start/cancel sync
  - Body: `{cancel, start}`
- `GET /api/v1/settings/jellyfin/users` -- Jellyfin users

### Radarr

- `GET /api/v1/settings/radarr` -- list Radarr instances
- `POST /api/v1/settings/radarr` -- create instance
- `PUT /api/v1/settings/radarr/{radarrId}` -- update instance
- `DELETE /api/v1/settings/radarr/{radarrId}` -- delete instance
- `POST /api/v1/settings/radarr/test` -- test connection
  - Body: `{hostname, port, apiKey, useSsl, baseUrl}`
- `GET /api/v1/settings/radarr/{radarrId}/profiles` -- quality profiles

### Sonarr

- `GET /api/v1/settings/sonarr` -- list Sonarr instances
- `POST /api/v1/settings/sonarr` -- create instance
- `PUT /api/v1/settings/sonarr/{sonarrId}` -- update instance
- `DELETE /api/v1/settings/sonarr/{sonarrId}` -- delete instance
- `POST /api/v1/settings/sonarr/test` -- test connection
  - Body: `{hostname, port, apiKey, useSsl, baseUrl}`

### Tautulli

- `GET /api/v1/settings/tautulli` -- Tautulli settings
- `POST /api/v1/settings/tautulli` -- update Tautulli settings

### Jobs & Cache

- `GET /api/v1/settings/jobs` -- list scheduled jobs
- `POST /api/v1/settings/jobs/{jobId}/run` -- manually run job
- `POST /api/v1/settings/jobs/{jobId}/cancel` -- cancel running job
- `POST /api/v1/settings/jobs/{jobId}/schedule` -- update job schedule
  - Body: `{schedule}`
- `GET /api/v1/settings/cache` -- list active caches
- `POST /api/v1/settings/cache/{cacheId}/flush` -- flush specific cache
- `POST /api/v1/settings/cache/dns/{dnsEntry}/flush` -- flush DNS cache entry

### Logs

- `GET /api/v1/settings/logs` -- application logs
  - Params: `take`, `skip`, `filter` (debug|info|warn|error), `search`

### Discovery Sliders

- `GET /api/v1/settings/discover` -- list custom sliders
- `POST /api/v1/settings/discover` -- batch update sliders
- `POST /api/v1/settings/discover/add` -- add slider
  - Body: `{title, type, data}`
- `PUT /api/v1/settings/discover/{sliderId}` -- update slider
- `DELETE /api/v1/settings/discover/{sliderId}` -- delete slider
- `GET /api/v1/settings/discover/reset` -- reset to defaults

### Notifications

For each agent: `GET` to read, `POST` to update, `POST .../test` to send test.

- `/api/v1/settings/notifications/email`
- `/api/v1/settings/notifications/discord`
- `/api/v1/settings/notifications/slack`
- `/api/v1/settings/notifications/telegram`
- `/api/v1/settings/notifications/pushbullet`
- `/api/v1/settings/notifications/pushover`
  - Extra: `GET .../pushover/sounds` (params: `token`)
- `/api/v1/settings/notifications/gotify`
- `/api/v1/settings/notifications/ntfy`
- `/api/v1/settings/notifications/webpush`
- `/api/v1/settings/notifications/webhook`
