# MYKMAN Music Project Handoff

Last updated: 2026-04-20

This file is the quick context dump for starting a new Codex/Claude/chat session in this repo. Read this before making changes.

## Core Vision

MYKMAN Music is a living memoir through music and data.

The public data side shows Mykal's music taste as a living ranking system: songs, albums, artists, genres, decades, gender/act breakdowns, geography, discography progress, listen-next queues, and comparison progress.

The writing side is the important, intimate part. Essays and reviews are behind a Ko-fi/paywall system. They may be long personal essays that only slightly reference a song, using music as a doorway into memoir, memory, temporality, vulnerability, identity, and emotional pattern recognition.

Important writing boundary: never write essay sentences for Mykal. Help structure ideas, clarify what he is really getting at, pressure-test blind spots, and suggest where hyperlinks/drafts should contain tangents.

## Current Stack

- Python + FastAPI + Jinja templates
- SQLite via SQLAlchemy
- Apple Music `Library.xml` importer from Mac Music app
- MusicBrainz enrichment for artist/album metadata and art
- Render deployment from GitHub master branch
- Persistent Render disk mounted at `/var/data`
- Local repo: `C:\Users\mykal\Desktop\music-ranker`
- GitHub repo: `https://github.com/mykalkelly-ops/mykman-music.git`
- Live site: `https://mykman-music.onrender.com`

## Non-Negotiable Data Safety

Do not wipe comparisons. Comparisons are the core labor of this project.

Do not upload local `data/music.db` over the live Render DB unless explicitly directed and after checking comparison counts. The live Render DB has historically had more real comparisons than local.

Safety features already exist:

- `/safety` admin page
- Manual DB snapshots
- Manual comparison exports
- Latest comparison export download
- Restore guard refuses to restore a backup with fewer comparisons than the current DB
- CLI export: `python -m app.export_comparisons`
- Importer creates pre-import backups through `backup_before_import()`

When touching importer, restore, deploy, or database code, think "protect comparisons first."

## Completed Major Features

- Apple Music XML importer for `Month YYYY` playlists.
- SQLite schema for artists, albums, songs, playlists, playlist songs, comparisons, notes, subscribers, people/acts, song credits, album tracks, song links, artist releases, and listen-next queue.
- Basic library viewer with home, songs, playlists, albums, artists, stats, and detail pages.
- Glicko-style pairwise comparison system.
- Comparison UI with multiple queued pairs, queue sizes, sticky queue persistence, keyboard/tap-friendly selection, skip/tie, undo, nostalgia checkbox, and easy/normal/hard difficulty.
- Rating updates account for choice difficulty and nostalgia.
- Anti-repeat comparison fixes:
  - Recent shown songs/pairs are tracked.
  - Exact previously compared pairs are blocked.
  - Placement-mode fallback respects pair exclusions.
  - Compare page cache key was bumped to clear old cursed queues.
- Ranking progress now uses practical milestones instead of impossible full pairwise convergence.
- Song, album, and artist MYK scores with half-MYK support.
- MYK image asset support for ratings.
- Album/artist score derivation from songs, including unliked/listened tracks where known.
- Artist scores shrink toward uncertainty and get low-coverage penalties when discography coverage is low.
- Singles and very short releases are excluded from album rankings.
- EPs are separated from albums.
- Albums can have total track count manually set when internet lookup is insufficient.
- Full album tracklists can be fetched/stored and displayed.
- Album detail pages show full tracklists, highlighting liked songs in green.
- Tracklist totals can count as album totals when a full listed tracklist exists.
- Canonical song linking exists for duplicate releases, while live/demo versions should remain separate for song rankings.
- Artist ranking can still consider alternate versions/duplicates appropriately.
- People/acts model:
  - `Artist.kind`: solo/group/collab
  - `Person`
  - `ArtistMembership`
  - `SongCredit`
  - Collab acts can link to child artists.
  - Groups can link to member people.
  - Gender breakdown recursively expands memberships/collabs.
- Importer and repair tooling now split obvious collaboration artist strings like `Sexyy Red & Chief Keef` into real credits.
- Protected real band names like `Captain Beefheart & His Magic Band` and `Earth, Wind & Fire` should not be split.
- `Various Artists` is treated as a container, not a real artist, and is skipped during bulk enrichment.
- Artist merge tool exists.
- Case/duplicate import resilience exists.
- `Ye` should be merged into `Kanye West` if it appears separately.
- Artist prompts for solo/group/collab and gender were tightened so resolved artists should not be re-asked repeatedly.
- Artist detail admin panel supports editing kind, members, collabs, origin, and metadata.
- MusicBrainz enrichment:
  - Artist metadata, country/origin, members when available, release totals, track totals, album art, artist art.
  - Bulk enrich skips `Various Artists`.
  - Some MusicBrainz data can be incomplete/wrong; manual correction remains necessary.
- Cached art stored under `data/art/`.
- Listen-next queue:
  - Add albums/artists from compare cards, album pages, artist pages, albums list, artists list.
  - Used when Mykal realizes he has not finished an album/artist.
- Discography progress:
  - Artist pages/lists show heard/total releases and listened/total tracks.
  - Progress is capped/fixed so it should not exceed 100%.
  - Completed-discography artists should be visually highlighted/understood as complete.
- Stats page:
  - Clickable genres, decades, genders, and playlists.
  - Charts/visual data insights.
  - Interactive Leaflet world map.
  - City-level artist origin support with US region grouping when origin city/state is known.
  - Fallback to country-level markers when city data is missing.
- Genre normalization:
  - Alternative rap, hip hop/rap, underground rap, rap, hip hop, and hip-hop/rap should merge under `Hip-Hop/Rap`.
- MYK Thoughts:
  - Notes/essays/reviews can target songs, albums, artists, or general posts.
  - Multi-song essay targeting exists.
  - Drafts and published statuses exist.
  - Hyperlinking infrastructure exists.
  - Admin can link to drafts; public users should see an unfinished/sorry page if the linked essay is not published.
  - Search UI in note editor was lightened to avoid dark-purple-on-black readability issues.
- Ko-fi/paywall:
  - Notes can be public or subscribers-only.
  - `/unlock` accepts access codes and sets subscriber cookie.
  - `/subscribers` admin dashboard can create manual codes, revoke/reactivate, and track subscribers.
  - Ko-fi webhook endpoint exists.
  - Codes expire after renewals stop.
  - Public data remains free; writings are behind Ko-fi.
- Comments/moderation and public read-only mode exist.
- Homepage:
  - Bio/project space added.
  - Link to unwritten "why I am doing this project" essay.
  - Retro 80s/90s furniture-store-closing style promo ads:
    - Subscribe for $5/month to see inside my mind.
    - Donate $50 to add an album to the top of my queue.
    - Donate $100 to add an artist to the top of my queue.
- Design direction:
  - Preserve the MYKMAN Music logo/font unless explicitly asked.
  - Current vibe: childlike, hopeful, emo with love, nihilistic edge, hearts, scrapbook/sticker energy.
  - Avoid dark purple text over black backgrounds.

## Deployment Notes

Render service:

- Type: Web Service
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Persistent disk: `/var/data`
- Important env vars:
  - `MYKMAN_DATA_DIR=/var/data`
  - `MYKMAN_ADMIN_PASSWORD=...`
  - `KOFI_URL=...`
  - `KOFI_VERIFICATION_TOKEN=...`

Render deploys from `master`. After pushing to GitHub, wait for Render auto-deploy.

Free/Starter memory can be tight during heavy enrichment or expensive pages. Prefer lightweight queries, pagination, and background work.

## Important Behavioral Decisions

- Universe of rankable songs comes from selected monthly playlists and imported/listened albums.
- If a song from an album is in a playlist, assume the album was listened to.
- If an album was listened to but songs were not liked, those tracks should count toward listened totals when known.
- There is no perfect list of every song Mykal ever heard; the site should explain that some data gets lost to the void when an album had no liked songs and was not otherwise recorded.
- `Songs liked` and `Songs listened` must not be assumed equal.
- Full internet-backed artist discography totals should come from enrichment, not just local library rows.
- MusicBrainz is useful but imperfect. Discogs may later help with collaborative/member metadata, but is not currently the primary source.
- Song rankings should keep demo/live versions separate.
- Artist ranking may consider related/duplicate versions where appropriate.
- Collaboration display artists can exist for album context, but ranking/scoring/gender stats should credit the real artists, not the fake combined string.
- Public visitors should not see raw comparison count as the headline metric; show progress toward meaningful milestones/accuracy instead.

## Known Pain Points / Watchlist

- MusicBrainz sometimes fetches the wrong release, especially EP vs album with the same name.
- Artist membership/collab data can be incomplete.
- Some artist origin city/state data must be manually filled to make the map region-specific.
- Bulk enrich can be memory/time heavy on Render.
- Apple Music XML does not provide everything needed for full discography completeness.
- Apple API/MusicKit may later help with automatic new-release/listen-next workflows, but currently no Apple Developer account is used.
- Browser localStorage can preserve stale compare queues; cache key bumps are used when queue logic changes.
- `transfer/` is untracked and should not be committed unless there is a deliberate reason.

## Recent Completed Commits Worth Knowing

- `c1ec337` Stop repeated comparison pairs
- `2c675c9` Split collaboration artists into real credits
- `cd6c32c` Add city-level artist origin map
- `add11b6` Add interactive artist country map to stats
- `fd26c61` Replace theoretical ranking target with milestones
- `6e1e85e` Strengthen comparison history safeguards
- `536dda9` Prevent artist discography progress over 100 percent
- `107f21e` Confidence adjust artist scores by discography coverage
- `b88d1ae` Skip Various Artists during bulk enrichment
- `aec84cc` Add half MYK scores for songs albums and artists
- `b372f36` Fix artist credits and add artist merge tool
- `5fca869` Make stats page interactive and visual
- `36872aa` Improve artist prompts and split EP rankings
- `5d59de7` Support multi-song essays and lighten note UI
- `bf4938b` Add listen-next queue from comparisons
- `0138c28` Show full album tracklists with liked highlights
- `fb92f70` Add album total editor and skip singles

## Good First Context For A New Chat

Tell the new agent:

> Read `PROJECT_HANDOFF.md` first. Protect comparison data. Do not overwrite the live Render DB with local DB. The project is MYKMAN Music, a music-ranking/data site plus paywalled living memoir through music. Public data is free; writings are Ko-fi gated. Never write essay prose for me; help structure and clarify.

