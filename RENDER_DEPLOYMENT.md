# September 2026 queue release

## Scope and status

Prepared locally from baseline `8261b72`. No live database connection, production SQL, upload, restore, push, or deployment was performed in this task. The prepared application changes are present in the working tree. Local regression tests use synthetic databases. Live comparison counts and the actual Render dashboard configuration still require verification at release time.

The release keeps unfinished pairs, selects XML-only songs, distinguishes skip from tie, prevents repeat votes, and improves Apple pagination and error reporting. Review added two protections: queue refill reserves SQLite's writer even after an ORM read, and startup metadata repairs are disabled by default. Those old repairs can merge songs, rewrite comparison references, and delete resulting self-comparisons. They now run only with explicit `MYKMAN_RUN_STARTUP_REPAIRS=1`; leave it unset or `0` for this release.

No new database migration, data import, historical rating replay, or comparison cleanup is required. Existing initialization still checks schema and can apply older missing columns; its SQL is explained below.

## Release procedure

1. Use the existing `mykman-music` service, connected repository and `master` branch. Verify its existing persistent disk is mounted at `/var/data` and `MYKMAN_DATA_DIR=/var/data`. Keep that same disk. Verify the existing admin password and other secrets are configured without copying them into source control. Set `MYKMAN_RUN_STARTUP_REPAIRS=0` in the actual service configuration before deploying; a YAML edit alone is not proof dashboard settings changed.
2. Schedule a short pause in voting, imports, undo, and metadata maintenance while recording the baseline. Record the currently deployed commit for a code rollback. Ensure sufficient disk space for a complete additional database copy and JSON export.
3. Make `scripts/deployment_safety.py` available in the running service shell through the normal authenticated file-transfer workflow before deployment. This script imports only the Python standard library; it does not import/start the app. In that shell, run the following with a unique unused backup name:

   ```sh
   python scripts/deployment_safety.py backup /var/data/music.db /var/data/music-pre-queue-20260918.db
   ```

   It creates a consistent complete SQLite backup and `/var/data/music-pre-queue-20260918.db.comparisons.json`. Record the reported count and securely download both artifacts outside Render. The JSON contains comparison rows; the full database also preserves song metadata and every other table. Treat either as private data. Never substitute the Windows database for the live source. An error means the backup is not verified: resolve it before release, retaining any failed artifacts for inspection. Existing files are never overwritten.
4. Run local checks:

   ```sh
   python -m unittest discover -s tests -v
   node --test tests/compare-ui.test.cjs tests/apple-import.test.cjs
   python -m compileall -q app scripts/deployment_safety.py tests
   git diff --check
   ```

5. Review/stage only release files: `app/main.py`, `app/pair_selector.py`, `app/queue_selection.py`, `app/scoring.py`, the three modified templates, `tests/`, `requirements-dev.txt`, `render.yaml`, the audit/Apple sync documentation, this runbook, the safety script, and README. Do not include `data/`, `transfer/`, screenshots, browser-result JSON, credentials, or backups. Commit the reviewed source. Publishing to the linked branch may trigger Render automatically; this task prepares that step but does not perform it.
6. Deploy source to the existing service. Keep the build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. The blueprint now sets `/healthz` as health check. Do not add seed/import/restore commands. Do not put the backup in a build/pre-deploy hook: persistent disks are accessible only at runtime. A disk-backed service has a brief deployment interruption. See [Render persistent disks](https://render.com/docs/disks) and [deployment behavior](https://render.com/docs/deploys).
7. After startup, run against the same persistent database and pre-release backup:

   ```sh
   python scripts/deployment_safety.py verify /var/data/music.db /var/data/music-pre-queue-20260918.db
   ```

   Require all baseline comparisons to be preserved byte-for-value across all columns. Additional rows are allowed. A count alone is insufficient: a replacement or changed winner could leave the count unchanged. Investigate any mismatch; do not automatically restore, delete, or rerank anything.
8. Check `/healthz`, login, `/compare`, and queue refill. Refill writes queue bookkeeping only. Confirm existing unfinished cards remain. Exercise real votes only when they express a real preference; do not create fake production votes as a smoke test. Skip should leave comparison count and ratings unchanged. Full voting/duplicate/retry behavior is tested with synthetic data. Apple authorization and real-device UI still need live verification.
9. For application failure, stop making changes and roll back code while retaining the current disk. A code rollback to the old baseline reintroduces its unconditional startup repairs: prepare the old code with the new startup repair guard retained before restarting it. Never roll back the database automatically, because post-backup votes would be lost. Restore requires a separate reconciled recovery procedure.

## SQL explained

The examples below describe SQLAlchemy's generated SQL semantically; it may use aliases, bind placeholders, batches, or extra primary-key refresh reads. No listed production operation was executed during preparation.

### Read-only deployment safety operations

`mode=ro` is a SQLite connection option, not an SQL statement. The helper requires an existing source file and denies source writes. It does not enable `immutable`, so SQLite can see committed WAL contents and coordinate with active writers.

`Connection.backup(destination)` invokes SQLite's online backup API rather than an SQL file-copy statement. SQLite copies a consistent database image, including committed data represented in the WAL, into a newly created destination file. This writes only the backup, never replaces the source, and may briefly contend with active database work. The destination is exclusively created; an existing path causes failure. It is a complete database backup, not just a comparison export.

`PRAGMA integrity_check` checks SQLite's internal table/index structure and reports `ok` or errors. The helper runs it on the new backup, and on both databases during verification. It does not repair data or validate musical meaning, and it does not replace foreign-key validation. It may scan the entire database and consume I/O; run it during the planned quiet period.

`SELECT * FROM comparisons ORDER BY id` reads every column of every comparison in primary-key order. Backup export reads the copied database; verification reads the baseline and current source. Python compares each baseline row with the current row having the same ID. Missing rows and changes to winner, song IDs, timestamps, difficulty, or nostalgia fail verification. New rows are allowed. Column differences also fail for manual review. This loads comparison rows into memory; it is appropriate for the current single-user workflow but should be streamed for very large archives. The JSON export is a filesystem write, not SQL. Connection closure releases read resources; no source `INSERT`, `UPDATE`, or `DELETE` is issued.

### Queue refill

`BEGIN IMMEDIATE` starts a SQLite write transaction and obtains the writer reservation before selecting candidates. Other writers wait (the application connection timeout is 60 seconds) or fail if that timeout expires. This prevents simultaneous refill requests from each observing an empty queue and adding duplicate work. SQLite read/write blocking details depend on journal mode. SQLAlchemy's `in_transaction()` alone is insufficient with the current SQLite driver: a prior SELECT can create an ORM transaction without a SQLite transaction. The code now checks the underlying SQLite connection. It reuses a genuine existing transaction rather than starting a nested one.

`SELECT ... FROM comparison_queue_items WHERE status='active' ORDER BY id` loads unfinished work. `SELECT id FROM songs` identifies song references that still exist. The limited comparison-history read selects both song IDs where both belong to the active queue's song-ID set; Python then checks the exact unordered pair. This detects previously answered queue items without treating unrelated comparisons as answers to that pair.

`UPDATE comparison_queue_items SET status='stale', completed_at=? WHERE id=?` retires a queue item whose song is missing, whose sides are identical, or whose pair was already compared. It retains the queue row. It does not remove or modify the historical comparison that made the card stale.

When more pairs are needed, three selection reads load (1) songs with LEFT OUTER JOINs to albums/artists, (2) all comparison song-ID pairs ordered newest first, and (3) playlist/song membership IDs. Outer joins retain songs even when related metadata is absent. All historical pairs are excluded, including reversed sides; there is no 20,000-row cutoff. Recent-song preference can relax if needed, but history exclusions never relax. Selection/scoring occurs in Python and changes no stored ratings. Memory grows with songs, comparison history and playlist membership; exhaustive fallback can be quadratic.

`INSERT INTO comparison_queue_items (...) VALUES (...)` adds only missing candidate pairs with active status and creation time. SQLAlchemy `flush()` sends pending inserts/updates and assigns IDs but does not commit them. `COMMIT` durably saves queue bookkeeping and releases the writer reservation. A subsequent active-queue SELECT returns the saved queue. Payload reads load selected songs/albums/artists in one joined query using `WHERE songs.id IN (...)`; ORM refreshes can occur when committed objects expire. Existing unfinished items are kept, even if the requested visible queue size is smaller.

### Vote, tie, skip and retry

The vote endpoint uses `BEGIN IMMEDIATE` before reading either song, so duplicate checks and rating writes are serialized across SQLite writers. `SELECT ... FROM songs WHERE id=?` loads each side. Self-pairs, missing songs, invalid winners, and unsupported difficulty values are rejected. Closing the request session rolls back its uncommitted transaction on rejection/error and releases locks.

For a vote or tie, `SELECT id FROM comparisons WHERE (song_a_id=? AND song_b_id=?) OR (song_a_id=? AND song_b_id=?) LIMIT 1` checks both orientations. A match returns HTTP 409 without recording another vote. This is an application-level rule protected by the writer reservation, not a newly added unique database constraint. Historical duplicate rows are retained.

`UPDATE songs SET ... WHERE id=?` persists new Glicko rating, deviation, volatility, comparison count and any placement state for the two songs only. Calculations occur in Python. Both placement bounds use the opponents' pre-vote ratings. A tie is evidence and changes rating uncertainty/counts but does not narrow placement bounds. No archive-wide replay occurs.

`INSERT INTO comparisons (song_a_id, song_b_id, winner_id, difficulty, nostalgia, created_at) VALUES (...)` appends exactly one new comparison; a tie stores a null winner. An active-queue SELECT finds the corresponding card; `UPDATE comparison_queue_items SET status='completed', completed_at=? WHERE id=?` completes it. Both song changes, the comparison insertion and queue completion commit in the same transaction. A flush is not a separate durable save.

A skip performs the queue completion lookup and status update, ending with `status='skipped'`. It performs no comparison INSERT and no song rating UPDATE. Depending on flush timing, SQLite may see an intermediate completed-status UPDATE followed by skipped status within the same transaction. Historical null-winner rows cannot reliably be separated into old skips versus ties and are left untouched. A skipped pair may be proposed again later because it is not comparison evidence.

After the vote/skip commit, refill runs in a separate transaction using the operations above. The append-only JSON journal is a filesystem operation after the vote commit, not part of the SQL transaction. Thus journal/refill failure can produce an error after a vote was saved; a retry is rejected by the existing-pair check. Reload the queue to reconcile. The database remains the source of truth. Exact atomicity between SQL and that journal is not claimed.

### Scores and initialization

Album/artist scoring now uses `SELECT` batches for albums, songs and album tracks (`WHERE foreign_key IN (...)`) instead of repeatedly fetching each relationship. Playlist membership and artist-membership reads are also SELECTs. These change how scores are calculated/displayed, not stored comparison rows or ratings.

`Base.metadata.create_all()` checks table existence through SQLite schema introspection (`PRAGMA` and/or `sqlite_master` SELECTs) and emits `CREATE TABLE`/`CREATE INDEX` for missing model tables and their declared indexes. It does not drop, truncate or rebuild existing tables. No model definitions change in this release.

Existing `init_db()` inspects columns and conditionally executes `ALTER TABLE ... ADD COLUMN` for old schemas. Songs: placement pending/low/high, liked, track number, play/skip counts, Apple library ID. Artists: kind, images, disambiguation, start/end years, prompt resolution, internet totals/sync time, Apple catalog ID/status, and geographic fields. Albums: MusicBrainz/release-group IDs/type, cover fields, track total, excluded-from-listened flag. Playlists: Apple library ID. Notes: visibility, status, kind. Persons: MusicBrainz ID. Comparisons: difficulty and nostalgia. Types/defaults are defined in `app/models.py`; nullable columns default to NULL, numeric/boolean defaults to 0 except placement pending (1), and note defaults are public/published/essay. These are pre-existing compatibility additions, not a migration newly introduced here. Added defaults can change how older rows are interpreted, so a current-schema baseline is expected and a schema difference during verification requires review. Existing migration error swallowing remains a limitation.

Initialization also runs `UPDATE artists SET prompt_resolved=1 WHERE (prompt_resolved IS NULL OR prompt_resolved=0) AND (kind IN ('group','collab') OR (kind='solo' AND gender IN ('M','F','NB','Unknown')))`. This marks already-classified artists as resolved. It does not alter songs or comparisons. The surrounding initialization transaction commits successful schema/metadata work. The newly gated people/collaboration/alias repair routines are not run on a normal deployment; notably, their comparison-rewriting/deleting SQL is excluded from this release path.

Authentication may independently SELECT admin sessions and DELETE expired session rows; login inserts a session. Those are existing authentication operations, never comparison deletions. The existing undo, restore, importer and manual metadata-maintenance routes remain separate operations and are not invoked by this preparation or release procedure.

### Test-only SQL

Tests create temporary/in-memory schemas, INSERT synthetic artists/albums/songs/comparisons, and exercise the queue/vote operations described above. Small-library tests DELETE synthetic songs to simulate exhaustion. Safety tests enable WAL with `PRAGMA journal_mode=WAL`, create a minimal synthetic comparisons table, INSERT rows, and deliberately UPDATE a winner to prove preservation verification fails. Concurrency tests use a temporary file and separate connections to prove reversed simultaneous votes save exactly one result. None of these operations target local `data/music.db` or Render.
