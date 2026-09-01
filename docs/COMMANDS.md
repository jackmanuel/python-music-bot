# Command Reference

The bot uses `!` as its command prefix.

## Playback

*   `!join`: Joins your current voice channel.
*   `!leave`: Disconnects from the current voice channel and clears active playback state.
*   `!play <song name or URL>`: Plays a song from a YouTube search query, YouTube URL, or SoundCloud URL. Playback confirmations include the current server's first queue date/requester and completed lifetime play count. If something is already playing, the confirmation also includes the new song's queue position and estimated wait.
*   `!search <song name>`: Shows up to five YouTube results and lets you pick one by reacting to the message.
*   `!skip`: Skips the current song.

## Queue

*   `!queue` or `!q`: Shows the currently playing song and the next queued songs.
*   `!nowplaying` or `!np`: Shows the current song and playback progress.
*   `!remove <position>`: Removes a queued song by its queue number. If that request created a new cache download, the file is removed when no longer in use.
*   `!clear`: Clears the waiting queue and marks those requests as skipped without interrupting the current song.

## Stats

*   `!songinfo <YouTube URL>`: Shows locally stored song metadata, cache status, completed/skipped counts, first request details, and the server's top five requesters for that song. This command does not play or queue the song and makes no `yt-dlp` requests.
*   `!stats [@user]`: Shows total request count for you or another server member.
*   `!statslong [@user]`: Shows detailed stats including today, this week, this month, this year, all time, longest streak, and top repeated requests.
*   `!leaderboard` or `!lb`: Shows the top five song requesters in the server.
*   `!cumulativegraph` or `!cg`: Generates a static cumulative song-play graph from the SQLite play history.
*   `!leaderboardrace` or `!lbrace`: Generates an animated leaderboard race video from the SQLite play history.

## Cache

*   `!cache`: Shows the number of cached songs, approximate cache size, and active cache mode.
*   `!clearcache`: Clears cached `.opus` files. Requires administrator permissions and a typed confirmation.

New cache downloads are also removed automatically when their song is skipped within the first 30 seconds of playback. Previously cached songs are not affected.
