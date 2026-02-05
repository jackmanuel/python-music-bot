# leaderboard_race.py
"""
Generates animated bar chart race videos showing user song play counts over time.
Uses the bar_chart_race library to create MP4 animations from play history data.
"""

import logging
import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd
import bar_chart_race as bcr

logger = logging.getLogger(__name__)


def generate_race_video(
    play_data: List[Dict[str, Any]],
    guild,
    output_dir: Optional[str] = None,
    top_n: int = 10,
    period_length: int = 500,
    steps_per_period: int = 10,
) -> Optional[str]:
    """
    Generates an animated bar chart race video from play history data.

    Args:
        play_data: List of dicts with 'request_timestamp', 'user_id', and 'user_name' keys.
        guild: Discord Guild object for resolving user display names.
        output_dir: Directory for output file. Uses temp dir if None.
        top_n: Maximum number of users to show in the race (default 10).
        period_length: Milliseconds per period in animation (default 500).
        steps_per_period: Frames between each period for smoothness (default 10).

    Returns:
        Path to the generated MP4 file, or None if generation failed.
    """
    if not play_data:
        logger.warning("No play data provided for race video generation.")
        return None

    logger.info(f"Generating bar chart race from {len(play_data)} play records...")

    try:
        # Convert to DataFrame
        df = pd.DataFrame(play_data)
        df['request_timestamp'] = pd.to_datetime(df['request_timestamp'])

        # Create a mapping of user_id to display name
        user_display_names = {}
        for record in play_data:
            user_id = record['user_id']
            if user_id not in user_display_names:
                # Try to get current display name from guild
                member = guild.get_member(user_id) if guild else None
                if member:
                    user_display_names[user_id] = member.display_name
                else:
                    # Fall back to stored user_name (strip discriminator if present)
                    stored_name = record.get('user_name', f'User {user_id}')
                    # Handle old-style "username#0000" format
                    if '#' in stored_name:
                        stored_name = stored_name.split('#')[0]
                    user_display_names[user_id] = stored_name

        # Map user_id to display name in the DataFrame
        df['display_name'] = df['user_id'].map(user_display_names)

        # Resample to daily counts per user
        df['date'] = df['request_timestamp'].dt.date
        daily_counts = df.groupby(['date', 'display_name']).size().unstack(fill_value=0)

        # Calculate cumulative sum over time
        cumulative_counts = daily_counts.cumsum()

        # Ensure index is datetime for bar_chart_race
        cumulative_counts.index = pd.to_datetime(cumulative_counts.index)

        # Filter to top N users by final count
        final_counts = cumulative_counts.iloc[-1].sort_values(ascending=False)
        top_users = final_counts.head(top_n).index.tolist()
        cumulative_counts = cumulative_counts[top_users]

        if cumulative_counts.empty or len(cumulative_counts.columns) == 0:
            logger.warning("No data available after processing for bar chart race.")
            return None

        # Generate output file path
        if output_dir is None:
            output_dir = tempfile.gettempdir()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(output_dir, f"leaderboard_race_{timestamp}.mp4")

        # Determine date range for title
        start_date = cumulative_counts.index.min().strftime('%b %Y')
        end_date = cumulative_counts.index.max().strftime('%b %Y')
        guild_name = guild.name if guild else "Server"

        logger.info(f"Creating bar chart race animation with {len(top_users)} users...")

        # Generate the bar chart race
        bcr.bar_chart_race(
            df=cumulative_counts,
            filename=output_path,
            orientation='h',
            sort='desc',
            n_bars=top_n,
            fixed_order=False,
            fixed_max=True,
            steps_per_period=steps_per_period,
            period_length=period_length,
            interpolate_period=False,
            label_bars=True,
            bar_size=0.95,
            period_label={'x': 0.99, 'y': 0.25, 'ha': 'right', 'va': 'center', 'size': 24},
            period_fmt='%b %d, %Y',
            period_summary_func=lambda v, r: {
                'x': 0.99, 'y': 0.18, 's': f'Total Plays: {v.sum():,.0f}',
                'ha': 'right', 'size': 14, 'family': 'sans-serif'
            },
            perpendicular_bar_func=None,
            figsize=(8, 5),
            dpi=144,
            cmap='tab20',
            title=f'🏆 {guild_name} Song Leaderboard Race\n{start_date} - {end_date}',
            title_size=16,
            bar_label_size=10,
            tick_label_size=10,
            shared_fontdict={'family': 'sans-serif', 'weight': 'normal'},
            scale='linear',
            writer=None,
            fig=None,
            bar_kwargs={'alpha': 0.85, 'ec': 'black', 'lw': 0.5},
            filter_column_colors=False,
        )

        logger.info(f"Bar chart race video saved to: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Failed to generate bar chart race video: {e}", exc_info=True)
        return None
