# leaderboard_race.py
"""
Generates animated bar chart race videos showing user song play counts over time.
Uses the bar_chart_race library to create MP4 animations from play history data.
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

# --- Monkey-patch bar_chart_race for pandas 3.0 compatibility ---
# The bar_chart_race library (last updated 2020) uses deprecated fillna(method='ffill')
# which was removed in pandas 3.0. This patch fixes it at runtime.
import bar_chart_race._make_chart as _bcr_make_chart

_original_prepare_wide_data = _bcr_make_chart.prepare_wide_data

def _patched_prepare_wide_data(df, orientation='h', sort='desc', n_bars=None, 
                                interpolate_period=False, steps_per_period=10, 
                                compute_ranks=True):
    """Patched version that uses ffill() instead of fillna(method='ffill')"""
    if n_bars is None:
        n_bars = df.shape[1]

    df_values = df.reset_index()
    df_values.index = df_values.index * steps_per_period
    new_index = range(df_values.index[-1] + 1)
    df_values = df_values.reindex(new_index)
    
    if interpolate_period:
        if df_values.iloc[:, 0].dtype.kind == 'M':
            first, last = df_values.iloc[[0, -1], 0]
            dr = pd.date_range(first, last, periods=len(df_values))
            df_values.iloc[:, 0] = dr
        else:
            df_values.iloc[:, 0] = df_values.iloc[:, 0].interpolate()
    else:
        # FIX: Use ffill() instead of fillna(method='ffill')
        df_values.iloc[:, 0] = df_values.iloc[:, 0].ffill()
    
    df_values = df_values.set_index(df_values.columns[0])
    if compute_ranks:
        df_ranks = df_values.rank(axis=1, method='first', ascending=False).clip(upper=n_bars + 1)
        if (sort == 'desc' and orientation == 'h') or (sort == 'asc' and orientation == 'v'):
            df_ranks = n_bars + 1 - df_ranks
        df_ranks = df_ranks.interpolate()
    
    df_values = df_values.interpolate()
    if compute_ranks:
        return df_values, df_ranks
    return df_values

# Apply the patch
_bcr_make_chart.prepare_wide_data = _patched_prepare_wide_data
# --- End monkey-patch ---

import bar_chart_race as bcr

logger = logging.getLogger(__name__)

# Dark theme color constants (matching leaderboard_graph.py)
DARK_BG_COLOR = '#1a1a2e'       # Figure background
DARK_AXES_COLOR = '#16213e'     # Axes background
DARK_TEXT_COLOR = 'white'       # Title text
DARK_LABEL_COLOR = '#e0e0e0'    # Label text
DARK_TICK_COLOR = '#b0b0b0'     # Tick text
DARK_GRID_COLOR = '#4a4a6a'     # Grid and spine color


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

        # Set up dark theme for matplotlib using rcParams
        # This ensures bar_chart_race inherits our color scheme
        plt.style.use('dark_background')
        plt.rcParams.update({
            'figure.facecolor': DARK_BG_COLOR,
            'axes.facecolor': DARK_AXES_COLOR,
            'axes.edgecolor': DARK_GRID_COLOR,
            'axes.labelcolor': DARK_LABEL_COLOR,
            'axes.titlecolor': DARK_TEXT_COLOR,
            'text.color': DARK_TEXT_COLOR,
            'xtick.color': DARK_TICK_COLOR,
            'ytick.color': DARK_TICK_COLOR,
            'savefig.facecolor': DARK_BG_COLOR,
            'savefig.edgecolor': DARK_BG_COLOR,
        })

        # Create figure with axes already styled
        # bar_chart_race hardcodes ax.set_facecolor('.9') when it creates its own figure,
        # but if we pass a figure with axes already added, it uses ours instead
        # NOTE: When passing a pre-made figure, bar_chart_race skips create_figure()
        # which means it won't set the title - we must set it ourselves
        fig, ax = plt.subplots(figsize=(8, 5), dpi=144)
        fig.patch.set_facecolor(DARK_BG_COLOR)
        ax.set_facecolor(DARK_AXES_COLOR)
        ax.tick_params(colors=DARK_TICK_COLOR, labelsize=10)
        for spine in ax.spines.values():
            spine.set_color(DARK_GRID_COLOR)
        
        # Set the title ourselves (bar_chart_race won't do it for pre-made figures)
        ax.set_title(
            f'{guild_name} Song Leaderboard Race\n{start_date} - {end_date}',
            fontsize=16,
            color=DARK_TEXT_COLOR,
            fontweight='bold',
            pad=15
        )

        # Create more margin at the top/bottom and significantly more on the left for names
        plt.subplots_adjust(top=0.82, bottom=0.25, right=0.90, left=0.20)

        # Manually set x-limit to ensure bar labels have space (since bcr skips this for pre-made figures)
        max_val = cumulative_counts.max().max()
        ax.set_xlim(0, max_val * 1.2) # Give 20% extra space for labels

        # Generate the bar chart race with dark theme
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
            bar_size=0.7,
            period_label={
                'x': 0.98, 'y': -0.18, 'ha': 'right', 'va': 'top', 
                'size': 18, 'color': DARK_TEXT_COLOR
            },
            period_fmt='%b %d, %Y',
            period_summary_func=lambda v, r: {
                'x': 0.98, 'y': -0.32, 's': f'Total Plays: {v.sum():,.0f}',
                'ha': 'right', 'va': 'top', 'size': 12, 'family': 'sans-serif', 'color': DARK_LABEL_COLOR
            },
            perpendicular_bar_func=None,
            figsize=(8, 5),
            dpi=144,
            cmap='tab20',
            title_size=16,
            bar_label_size=10,
            tick_label_size=10,
            shared_fontdict={'family': 'sans-serif', 'weight': 'normal'},
            scale='linear',
            writer=None,
            fig=fig,
            bar_kwargs={'alpha': 0.9, 'ec': DARK_GRID_COLOR, 'lw': 0.5},
            filter_column_colors=False,
        )
        
        plt.close(fig)

        logger.info(f"Bar chart race video saved to: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Failed to generate bar chart race video: {e}", exc_info=True)
        return None
