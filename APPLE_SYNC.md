# MacBook-free monthly playlist updates

Open `/apple-music` in MYKMAN Music, choose **Connect and preview updates**, sign in to Apple, then choose **Import previewed updates**. The server takes a database snapshot and comparison export before importing. XML is an optional legacy import, not required for this workflow.

The server needs `APPLE_TEAM_ID`, `APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY` (or `APPLE_PRIVATE_KEY_PATH`) configured once using an Apple Music developer key. Your account must authorize MusicKit and the playlists must be present in your Apple Music cloud library. Developer setup and account authorization have not been verified against your live deployment in this change.

Scope: playlists named Month YYYY and their songs, with all playlist and track pages fetched. Existing ratings, comparisons and play/skip counts are retained; missing songs or playlist memberships are not deleted. This is a browser-initiated update, not an unattended background sync. It does not yet import every song outside monthly playlists, refresh lifetime play/skip counts, or bring in files that exist only locally on a computer.

Validation: three mocked JavaScript tests cover pagination for playlists/tracks and invalidating a stale preview after partial failure. Live Apple account authorization still needs an end-to-end check after deployment.

## Listening queue recovery

The Apple Music page also has a read-only **Scan full library** action. It reads
every page of `/v1/me/library/songs`, including songs outside monthly playlists,
and displays an inventory grouped by artist and album. It does not import these
tracks into rankings or infer completion from library membership, downloads,
or chronological order. Track and album completion remain unknown. A failed
page clears the preview, and nothing is saved from a partial scan.

After a complete scan, **Save inventory and open queue** saves the inventory
to separate listening tables, without adding unheard tracks to rankings.
The scan also requests recent played tracks. An unavailable history endpoint
does not prevent saving the complete library inventory; playback evidence is
shown separately and never marks a track complete.

At `/listening`, confirm individual songs, whole albums, or artists; mark
unfinished targets; queue any target; move it to the top; and save resume notes.
The newest explicit decision wins for each saved track. Queuing alone does not
reset completion. Finishing an album or artist clears queued descendants.
Newly added tracks do not inherit older album/artist completion decisions.
Missing library tracks are retained on rescans. Artist completion is a personal
confirmation, not a claim that the inventory is an exhaustive discography.

The first queued target provides the next unfinished saved track, with
in-progress tracks preferred and album track order used otherwise. You choose
which artist or album comes first; release chronology is not assumed. Completing
a song advances this suggestion within a queued album. The earlier `/listen-next`
list is retained and linked separately; no old queue entries are deleted.

Inventory and progress live in the same persistent SQLite database as the app,
in two additive tables. `/api/listening/export` downloads both as JSON for backup;
existing whole-database snapshots also include them. There is no automatic
lifetime-history recovery or polling service. Live Apple authorization and
recovery still require verification after deployment; no export from iTunes is
needed.

Apple documentation: https://developer.apple.com/documentation/applemusicapi/get-all-library-playlists
