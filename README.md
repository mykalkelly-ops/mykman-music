# Music Ranker

Local app that ingests an Apple Music library export and will (eventually) produce a living ranking of your favorite songs, albums, and artists via Glicko-2 pairwise comparisons.

For project memory, completed feature history, current product decisions, and new-chat handoff context, start with [`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md).

## What works right now

- Parses `Library.xml` exported from the Mac Music app
- Loads artists, albums, songs, and any playlist named `Month YYYY` (e.g. `April 2026`) into a local SQLite DB
- Web viewer showing library stats, each monthly playlist, searchable song/album/artist pages, and detail pages
- Pairwise comparison UI with Glicko-style ranking, queueing, undo, skip/tie, difficulty, nostalgia, anti-repeat logic, and practical ranking milestones
- Song, album, and artist MYK scores
- Artist/person/collab metadata, gender breakdowns, artist origin map, and discography progress
- Listen-next queue and album confirmation queue
- MYK Thoughts essays/reviews with drafts, multi-song targeting, Ko-fi subscriber access, comments, and moderation
- Admin safety tools for DB snapshots, comparison exports, comparison history export, and guarded restore
- Admin Today cockpit at `/today` for the daily work loop: protect comparisons, rank, write, clean metadata, and check listen-next

## Exporting your library on the Mac

1. Open **Music** on the Mac.
2. (One-time) Enable library XML: **Music → Settings → Advanced → check "Share Library XML with other applications"**. Apple may also auto-write it to `~/Music/Music/Library.xml`.
3. **File → Library → Export Library…** → save as `Library.xml`.
4. Get the file to your Windows PC (shared iCloud/Dropbox folder, scp, USB stick — whatever). Place it somewhere you'll remember, e.g. `C:\Users\mykal\Desktop\music-ranker\data\Library.xml`.

Re-export any time you want fresh data. Later phases will auto-watch the file.

## Running on Windows

From `C:\Users\mykal\Desktop\music-ranker`:

```bat
:: one-time: create venv + install deps (already done if you're reading this)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

:: import your library
.venv\Scripts\python -m app.importer data\Library.xml

:: start the web app
.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 in your browser.

## Admin login

Set an admin password before you expose this outside your machine:

```powershell
$env:MYKMAN_ADMIN_PASSWORD="your-password-here"
.venv\Scripts\python -m uvicorn app.main:app --reload
```

If you do not set it, the fallback password is `changeme`.

## Apple Music API setup

The current import path still uses `Library.xml`, but the Apple Developer path
is being prepared so MYKMAN Music can eventually sync Apple Music playlists
without manual MacBook exports.

After Apple Developer enrollment is approved:

1. In Apple Developer, create an Apple Music / MusicKit private key.
2. Set these environment variables locally and on Render:
   - `APPLE_TEAM_ID`
   - `APPLE_KEY_ID`
   - `APPLE_PRIVATE_KEY` or `APPLE_PRIVATE_KEY_PATH`
3. Locally, `APPLE_PRIVATE_KEY_PATH` can point to the downloaded `.p8` file.
   On Render, use `APPLE_PRIVATE_KEY` and paste the full `.p8` contents.
   Escaped `\n` newlines are supported for hosted env vars.
4. Open `/apple-music` as admin and generate a test developer token.
5. Click `Configure MusicKit`, then `Authorize Apple Music`, then `Fetch library playlists`.
6. Click `Preview Month playlist sync` and inspect the read-only diff.
7. If the preview looks right, click `Import Month playlists`.
8. Use `Preview weekly playlist` / `Create Apple Music playlist` to make a
   private Apple Music playlist from upcoming comparison candidates.

The first API sync preview is read-only: fetch Apple Music playlists and compare
them against the current `Library.xml` data before writing anything to the DB.
The guarded import creates a DB snapshot and comparison export first, then
upserts Month YYYY playlist songs without deleting old data or touching
comparison history.

The weekly comparison playlist feature chooses upcoming comparison pairs,
requires songs to have Apple Music library IDs, and creates a private Apple
Music playlist in the signed-in Apple Music account from the browser.

Artist pages also have an `Enrich totals from Apple` admin button. It searches
the Apple Music catalog for the artist, fetches catalog albums, filters obvious
singles/compilations/anniversary duplicates, collapses deluxe duplicates, and
updates internet release and track totals used by discography coverage. Live
albums count.

The `/artists` admin page can also enrich the next 10 visible artists from Apple
in one batch. By default it skips artists already synced from Apple; use the
refresh checkbox to re-run already-synced visible rows.

The `/data-issues` admin page flags import artifacts and scoring data problems:
impossible coverage counts, leading `and ...` artist names, feature-only artists
without Apple catalog totals, suspicious featured credits, and solo-looking
artists marked as collabs. Feature-only artists such as Clem Creevy or Bill
Cosmiq may have no Apple catalog artist page; keep their feature credits when
the song title actually names them instead of forcing a catalog total.

## Project layout

```
music-ranker/
  app/
    __init__.py
    db.py            # SQLite engine + session
    models.py        # SQLAlchemy schema
    importer.py      # Library.xml -> DB
    main.py          # FastAPI app
    templates/       # Jinja2 HTML
  data/              # SQLite DB + Library.xml go here (gitignored)
  requirements.txt
```

## People & Acts

Artists are now classified by `kind` (`solo` / `group` / `collab`) and decomposed into Persons via `ArtistMembership` rows. A solo artist links to one Person; a group links to multiple Persons; a collab act links to other Artists via `child_artist_id`. Stats and gender breakdowns derive from `SongCredit → ArtistMembership → Person.gender` (recursively expanding collab child acts), so a Beyoncé+Jay-Z track counts as "mixed" rather than as one artist's gender. Run the one-time backfill with `python -m app.backfill_people` (it also runs automatically on startup if the persons table is empty).

## Roadmap

1. **Protect the comparison data** — keep local, Render, snapshots, and comparison exports aligned before imports/restores/deploy-risky work.
2. **Make the daily loop irresistible** — improve `/today` and `/compare` until ranking, cleanup, and writing each have a clear next action.
3. **Deepen writing workflows** — support drafts, related essays, review prompts, and subscriber-facing structure without generating Mykal's prose.
4. **Improve metadata confidence** — better manual review for MusicBrainz misses, origins, memberships, collabs, full tracklists, and art.
5. **Tighten public storytelling** — make stats explain signal, uncertainty, listening gaps, and progress without overclaiming completeness.
6. **Future sync/mobile** — Mac-to-PC auto-sync, responsive polish, and a native phone app only if the web app stops being enough.

## Paywall & Ko-fi

Notes/essays can be marked `subscribers` in the editor. Public readers see the title, date, a short teaser, and a CTA to either enter an access code or tip on Ko-fi.

### Setup
- Set env vars before launching uvicorn:
  - `KOFI_VERIFICATION_TOKEN` — copy from your Ko-fi webhooks page
  - `KOFI_URL` — your Ko-fi page (default `https://ko-fi.com/mykman`)
- In Ko-fi → Settings → API/Webhooks, point the webhook at `https://yoursite/api/kofi-webhook`.
- Each new subscription generates a memorable code like `velvet-echo-417` and prints it to stdout. Hand it to the supporter (or check `/subscribers`).

### Manual codes
Open `/subscribers` (admin only) and click `+ Create manual code` to mint a code for friends/press. Codes default to 365 days.

### How cancellation works
Ko-fi's webhooks are renewal-based, not cancellation-based. Each successful payment bumps `expires_at` to now + 40 days. If a supporter stops paying, the code goes stale automatically about 40 days after their last successful payment — `is_subscriber()` checks `expires_at` on every request and flips the row to `expired`. Admins can also revoke instantly via the dashboard.
