# Command Reference

The bot uses `!` as its command prefix.

## Playback

*   `!join`: Joins your current voice channel.
*   `!leave`: Disconnects from the current voice channel and clears active playback state.
*   `!play <song name or URL>`: Plays a song from a YouTube search query, YouTube URL, or SoundCloud URL. If something is already playing, the song is added to the queue.
*   `!search <song name>`: Shows up to five YouTube results and lets you pick one by reacting to the message.
*   `!skip`: Skips the current song.

## Queue

*   `!queue` or `!q`: Shows the currently playing song and the next queued songs.
*   `!nowplaying` or `!np`: Shows the current song and playback progress.
*   `!remove <position>`: Removes a queued song by its queue number.
*   `!clear`: Clears the current queue.

## Stats

*   `!stats [@user]`: Shows total request count for you or another server member.
*   `!statslong [@user]`: Shows detailed stats including today, this week, this month, this year, all time, longest streak, and top repeated requests.
*   `!leaderboard` or `!lb`: Shows the top five song requesters in the server.
*   `!cumulativegraph` or `!cg`: Generates a static cumulative song-play graph from the SQLite play history.
*   `!leaderboardrace` or `!lbrace`: Generates an animated leaderboard race video from the SQLite play history.

## Cache

*   `!cache`: Shows the number of cached songs, approximate cache size, and active cache mode.
*   `!clearcache`: Clears cached `.opus` files. Requires administrator permissions and a typed confirmation.
