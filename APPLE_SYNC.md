# MacBook-free monthly playlist updates

Open `/apple-music` in MYKMAN Music, choose **Connect and preview updates**, sign in to Apple, then choose **Import previewed updates**. The server takes a database snapshot and comparison export before importing. XML is an optional legacy import, not required for this workflow.

The server needs `APPLE_TEAM_ID`, `APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY` (or `APPLE_PRIVATE_KEY_PATH`) configured once using an Apple Music developer key. Your account must authorize MusicKit and the playlists must be present in your Apple Music cloud library. Developer setup and account authorization have not been verified against your live deployment in this change.

Scope: playlists named Month YYYY and their songs, with all playlist and track pages fetched. Existing ratings, comparisons and play/skip counts are retained; missing songs or playlist memberships are not deleted. This is a browser-initiated update, not an unattended background sync. It does not yet import every song outside monthly playlists, refresh lifetime play/skip counts, or bring in files that exist only locally on a computer.

Validation: three mocked JavaScript tests cover pagination for playlists/tracks and invalidating a stale preview after partial failure. Live Apple account authorization still needs an end-to-end check after deployment.

Apple documentation: https://developer.apple.com/documentation/applemusicapi/get-all-library-playlists
