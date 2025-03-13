import configparser
import math
import os
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

# Read configuration
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), "config.ini")
config.read(config_path)

# Get configuration values
PLEX_URL = config["plex"]["url"]
PLEX_TOKEN = config["plex"]["token"]
MUSIC_LIBRARY_ID = config["plex"]["music_library_id"]


# Function to get all albums from Plex
def get_all_albums() -> List[Dict[str, str]]:
    url = f"{PLEX_URL}/library/sections/{MUSIC_LIBRARY_ID}/albums"
    headers = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json", "X-Plex-Container-Size": "2000"}
    response = requests.get(url, headers=headers)
    albums = []

    if response.status_code == 200:
        data = response.json()
        if "MediaContainer" in data and "Metadata" in data["MediaContainer"]:
            for album in data["MediaContainer"]["Metadata"]:
                albums.append({"key": album["ratingKey"], "title": album["title"], "artist": album["parentTitle"], "userRating": album.get("userRating")})

    return albums


# Function to get tracks for an album
def get_album_tracks(album_key: str) -> List[Dict[str, Any]]:
    url = f"{PLEX_URL}/library/metadata/{album_key}/children"
    headers = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json", "X-Plex-Container-Size": "2000"}
    response = requests.get(url, headers=headers)
    tracks = []

    if response.status_code == 200:
        data = response.json()
        if "MediaContainer" in data and "Metadata" in data["MediaContainer"]:
            for track in data["MediaContainer"]["Metadata"]:
                user_rating = track.get("userRating", None)
                duration = track.get("duration", 0)
                if duration:
                    duration = int(duration) // 1000  # Convert from milliseconds to seconds
                tracks.append({"title": track["title"], "rating": user_rating, "duration": duration})

    return tracks


# Function to calculate album rating based on track ratings
def calculate_album_rating(tracks: List[Dict[str, Any]]) -> tuple[Optional[int], Optional[str]]:
    # Get basic stats before any filtering
    ratings = [t["rating"] for t in tracks if t["rating"] is not None]
    if not ratings:
        return None, "No rated tracks"

    min_rating = min(ratings)
    avg_rating = sum(ratings) / len(ratings)

    # Skip if any track is unrated
    unrated_count = sum(1 for track in tracks if track.get("rating") is None or track.get("rating") == 0)
    if unrated_count > 0:
        return None, f"Has {unrated_count} unrated tracks (min: {min_rating}, avg: {avg_rating:.1f})"

    # Filter out very short tracks with low ratings
    filtered_tracks = [track for track in tracks if not (track.get("duration", 0) < 60 and track.get("rating", 0) < 3) and not track.get("duration", 0) < 30]

    # Skip if too few tracks remain after filtering
    if len(filtered_tracks) < 3:
        removed_tracks = len(tracks) - len(filtered_tracks)
        filter_info = f" ({removed_tracks} tracks filtered)" if removed_tracks > 0 else ""
        return None, f"Too few tracks {len(filtered_tracks)} tracks{filter_info}: (min: {min_rating}, avg: {avg_rating:.1f})"

    ratings = [track["rating"] for track in filtered_tracks]
    min_rating = min(ratings)
    avg_rating = sum(ratings) / len(ratings)

    # Calculate percentages for each tier
    excellent = sum(1 for r in ratings if r >= 9) / len(ratings)
    very_good = sum(1 for r in ratings if 7 <= r < 9) / len(ratings)
    # average = sum(1 for r in ratings if r == 6) / len(ratings)
    below_average = sum(1 for r in ratings if 4 <= r < 6) / len(ratings)
    poor = sum(1 for r in ratings if r < 4) / len(ratings)

    # Best track bonus
    max_rating = max(ratings) if ratings else 0
    best_track_bonus = 0
    if max_rating >= 9:
        best_track_bonus = 0.7
    elif max_rating >= 7:
        best_track_bonus = 0.3

    # Bad track penalty (increased)
    min_rating = min(ratings) if ratings else 0
    bad_track_penalty = 0
    if min_rating < 4:
        bad_track_penalty = 1.5  # Increased from 1.2
    elif min_rating < 6:
        bad_track_penalty = 0.8  # Increased from 0.6

    # No bad tracks bonus
    no_bad_tracks_bonus = 0.4 if min_rating >= 6 else 0

    # Calculate raw adjustment
    adjustment = (excellent * 1.2) + (very_good * 0.5) + best_track_bonus + no_bad_tracks_bonus - (below_average * 0.8) - (poor * 1.8) - bad_track_penalty

    # Cap total adjustment to ±1 point
    adjustment = max(-1.0, min(1.0, adjustment))

    # Apply adjustment and round, ensuring result is not lower than minimum track rating
    final_rating = max(min_rating, avg_rating + adjustment)
    return round_half_up(final_rating), None


# Function to update album rating
def update_album_rating(album_key: str, rating: int) -> bool:
    # Ensure rating is within Plex's 0-10 scale
    plex_rating = max(0, min(10, rating))
    url = f"{PLEX_URL}/library/sections/{MUSIC_LIBRARY_ID}/all"
    headers = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"}
    params = {
        "type": 9,  # Type 9 is for albums
        "id": album_key,
        "userRating.value": plex_rating,
    }
    response = requests.put(url, headers=headers, params=params)
    return response.status_code == 200


def get_filtered_tracks(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Get tracks filtered by duration and rating criteria."""
    return [track for track in tracks if not (track.get("duration", 0) < 60 and track.get("rating", 0) < 3) and not track.get("duration", 0) < 30]


def get_track_stats(tracks: List[Dict[str, Any]]) -> tuple[List[int], Optional[float], Optional[int], Optional[int]]:
    """Calculate track rating statistics. Returns (ratings, avg, min, max)."""
    ratings = [t["rating"] for t in tracks if t["rating"] is not None]
    if not ratings:
        return ratings, None, None, None
    return ratings, sum(ratings) / len(ratings), min(ratings), max(ratings)


def create_result_dict(album: Dict[str, str], rating: Optional[int], skip_reason: Optional[str] = None) -> Dict[str, Any]:
    """Create the base result dictionary."""
    result = {
        "Artist": album["artist"],
        "Album": album["title"],
        "Rating": rating,
        "Status": "Skipped" if rating is None else "Preview",
        "Rating Adjustment": None,
        "Avg Rating": None,
        "Lowest Track": None,
        "Highest Track": None,
    }
    if skip_reason:
        result["Reason"] = skip_reason
    return result


def update_progress(current: int, total: int, stats: Dict[str, int]) -> None:
    """Update progress display in-place."""
    processed = stats.get("Success", 0) + stats.get("Preview", 0)
    skipped = stats.get("Skipped", 0)
    failed = stats.get("Failed", 0)

    print(f"\rProcessed: {current}/{total} | Updated: {processed} | Skipped: {skipped} | Failed: {failed}", end="")


def round_half_up(n: float, decimals: int = 0) -> float:
    """Round a number using "half up" strategy instead of banker's rounding."""
    multiplier = 10**decimals
    return math.floor(n * multiplier + 0.5) / multiplier


def get_mode() -> str:
    """Get execution mode from user input."""
    while True:
        mode = input("Select mode ([p]review/[u]pdate): ").lower().strip()
        if mode in ["preview", "update", "p", "u"]:
            return "preview" if mode in ["preview", "p"] else "update"
        print("Invalid mode. Please enter 'preview' (p) or 'update' (u)")


def process_album(album: Dict[str, Any], mode: str, stats: Dict[str, int]) -> Dict[str, Any]:
    """Process a single album and return its result."""
    if album.get("userRating") is not None:
        result = create_result_dict(album, None, "Album already rated")
        stats["Skipped"] = stats.get("Skipped", 0) + 1
        return result

    tracks = get_album_tracks(album["key"])
    rating, skip_reason = calculate_album_rating(tracks)
    result = create_result_dict(album, rating, skip_reason)

    if rating is not None:
        filtered_tracks = get_filtered_tracks(tracks)
        filtered_ratings, filtered_avg, lowest, highest = get_track_stats(filtered_tracks)

        if filtered_avg is not None:
            result.update(
                {
                    "Rating Adjustment": round_half_up(abs(rating - filtered_avg), 2),
                    "Avg Rating": round_half_up(filtered_avg, 2),
                    "Lowest Track": lowest,
                    "Highest Track": highest,
                }
            )

        if mode == "update":
            success = update_album_rating(album["key"], rating)
            result["Status"] = "Success" if success else "Failed"

    stats[result["Status"]] = stats.get(result["Status"], 0) + 1
    return result


def save_results(results: List[Dict[str, Any]], mode: str) -> str:
    """Save results to CSV and return output filename."""
    results_df = pd.DataFrame(results)
    output_file = f"plex_album_ratings_{mode}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    results_df.to_csv(output_file, index=False)
    return output_file


def print_summary(results: List[Dict[str, Any]], mode: str) -> None:
    """Print processing summary."""
    total = len(results)
    if mode == "update":
        updated = sum(1 for r in results if r["Status"] == "Success")
        skipped = sum(1 for r in results if r["Status"] == "Skipped")
        failed = sum(1 for r in results if r["Status"] == "Failed")

        print("\nSummary:")
        print(f"Total albums processed: {total}")
        print(f"Successfully updated: {updated}")
        print(f"Skipped: {skipped}")
        print(f"Failed: {failed}")
    else:
        processed = sum(1 for r in results if r["Status"] == "Preview")
        skipped = sum(1 for r in results if r["Status"] == "Skipped")

        print("\nSummary:")
        print(f"Total albums processed: {total}")
        print(f"Ratings calculated: {processed}")
        print(f"Skipped: {skipped}")


def main() -> None:
    mode = get_mode()
    print(f"\nRunning in {mode.upper()} mode")

    print("Fetching albums from Plex...")
    albums = get_all_albums()
    print(f"Found {len(albums)} albums")

    results = []
    stats = {"Preview": 0, "Success": 0, "Failed": 0, "Skipped": 0}

    print("Processing albums...")
    for i, album in enumerate(albums, 1):
        result = process_album(album, mode, stats)
        results.append(result)
        update_progress(i, len(albums), stats)
        time.sleep(0.1)

    print("\nDone!")

    output_file = save_results(results, mode)
    print_summary(results, mode)
    print(f"Results saved to {output_file}")


if __name__ == "__main__":
    main()
