# leaderboard_graph.py
"""
Generates static line graph images showing cumulative user song plays over time.
Uses matplotlib to create PNG images from play history data.
"""

# IMPORTANT: Set matplotlib backend BEFORE any matplotlib imports
# This prevents Tkinter threading conflicts in async environments like Discord bots
import matplotlib
matplotlib.use('Agg')  # Use headless backend (no GUI, thread-safe)

import logging
import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

logger = logging.getLogger(__name__)


def generate_cumulative_graph(
    play_data: List[Dict[str, Any]],
    guild,
    output_dir: Optional[str] = None,
    top_n: int = 10,
) -> Optional[str]:
    """
    Generates a static line graph showing cumulative plays over time per user.

    Args:
        play_data: List of dicts with 'request_timestamp', 'user_id', and 'user_name' keys.
        guild: Discord Guild object for resolving user display names.
        output_dir: Directory for output file. Uses temp dir if None.
        top_n: Maximum number of users to show in the graph (default 10).

    Returns:
        Path to the generated PNG file, or None if generation failed.
    """
    if not play_data:
        logger.warning("No play data provided for cumulative graph generation.")
        return None

    logger.info(f"Generating cumulative line graph from {len(play_data)} play records...")

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

        # Ensure index is datetime for proper plotting
        cumulative_counts.index = pd.to_datetime(cumulative_counts.index)

        # Filter to top N users by final count
        final_counts = cumulative_counts.iloc[-1].sort_values(ascending=False)
        top_users = final_counts.head(top_n).index.tolist()
        cumulative_counts = cumulative_counts[top_users]

        if cumulative_counts.empty or len(cumulative_counts.columns) == 0:
            logger.warning("No data available after processing for cumulative graph.")
            return None

        # Generate output file path
        if output_dir is None:
            output_dir = tempfile.gettempdir()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(output_dir, f"cumulative_plays_{timestamp}.png")

        # Determine date range for title
        start_date = cumulative_counts.index.min().strftime('%b %Y')
        end_date = cumulative_counts.index.max().strftime('%b %Y')
        guild_name = guild.name if guild else "Server"

        logger.info(f"Creating cumulative line graph with {len(top_users)} users...")

        # Set up the plot with a modern dark theme
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12, 7), dpi=150)
        
        # Set background colors
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#16213e')

        # Use a vibrant color palette
        colors = plt.cm.tab20(range(len(top_users)))

        # Plot each user's cumulative line
        for idx, user in enumerate(top_users):
            ax.plot(
                cumulative_counts.index,
                cumulative_counts[user],
                label=user,
                color=colors[idx],
                linewidth=2.5,
                marker='o',
                markersize=3,
                alpha=0.9
            )

        # Style the plot
        ax.set_title(
            f'{guild_name}\nCumulative Song Plays ({start_date} - {end_date})',
            fontsize=16,
            fontweight='bold',
            color='white',
            pad=20
        )
        ax.set_xlabel('Date', fontsize=12, color='#e0e0e0')
        ax.set_ylabel('Total Songs Played', fontsize=12, color='#e0e0e0')

        # Format x-axis dates
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.xticks(rotation=45, ha='right', fontsize=10, color='#b0b0b0')
        plt.yticks(fontsize=10, color='#b0b0b0')

        # Add grid for readability
        ax.grid(True, linestyle='--', alpha=0.3, color='#4a4a6a')

        # Add legend
        legend = ax.legend(
            loc='upper left',
            fontsize=10,
            framealpha=0.8,
            facecolor='#1a1a2e',
            edgecolor='#4a4a6a',
            labelcolor='white'
        )

        # Style the spines
        for spine in ax.spines.values():
            spine.set_color('#4a4a6a')
            spine.set_linewidth(1)

        # Add final counts as annotations on the right side
        for idx, user in enumerate(top_users):
            final_value = cumulative_counts[user].iloc[-1]
            final_date = cumulative_counts.index[-1]
            ax.annotate(
                f'{int(final_value)}',
                xy=(final_date, final_value),
                xytext=(10, 0),
                textcoords='offset points',
                fontsize=9,
                color=colors[idx],
                fontweight='bold',
                va='center'
            )

        plt.tight_layout()

        # Save the figure
        plt.savefig(
            output_path,
            facecolor=fig.get_facecolor(),
            edgecolor='none',
            bbox_inches='tight',
            dpi=150
        )
        plt.close(fig)

        logger.info(f"Cumulative line graph saved to: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Failed to generate cumulative line graph: {e}", exc_info=True)
        return None
