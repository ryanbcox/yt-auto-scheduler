#!/usr/bin/env python3
"""Read-only readiness checks for the next Sacrament Meeting broadcast."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError

from notifications import send_problem_notification
from schedule import (
    UNLISTED,
    authenticate_youtube,
    find_upcoming_broadcast,
    format_broadcast_title,
    load_config,
    next_sunday_start,
)


def _camera_is_reachable(url: str) -> tuple[bool, str]:
    request = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= response.status < 400, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # The camera's web console normally challenges unauthenticated requests.
        if exc.code in (401, 403):
            return True, f"camera web interface reachable (HTTP {exc.code} authentication challenge)"
        return False, f"camera web interface returned HTTP {exc.code}"
    except Exception as exc:
        return False, f"camera web interface unreachable ({type(exc).__name__})"


def _plugin_is_available() -> tuple[bool, str]:
    plugin_dir = Path("/opt/ndi-relay/gst-plugins")
    try:
        inspect = subprocess.run(
            ["gst-inspect-1.0", "ndisrc"],
            env={**os.environ, "GST_PLUGIN_PATH": str(plugin_dir)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return False, f"NDI GStreamer source plugin check failed ({type(exc).__name__})"
    if inspect.returncode == 0:
        return True, "NDI GStreamer source plugin is available"
    return False, "NDI GStreamer source plugin is unavailable"


def check_readiness(config: dict, *, now: datetime | None = None, youtube=None) -> list[str]:
    """Check OAuth, broadcast setup, camera reachability, and local NDI software."""
    problems: list[str] = []
    tz = ZoneInfo(config["timezone"])
    local_now = (now or datetime.now(tz)).astimezone(tz)
    for channel in config["channels"]:
        try:
            channel_youtube = youtube if youtube is not None else authenticate_youtube(channel)
        except RefreshError:
            problems.append(f"YouTube OAuth refresh failed for {channel['channelName']}; reauthorize the scheduler before the next broadcast.")
            continue
        except Exception as exc:
            problems.append(f"YouTube authentication failed for {channel['channelName']} ({type(exc).__name__}); check scheduler authorization.")
            continue

        for ward in channel["wards"]:
            scheduled_start = next_sunday_start(local_now, config["timezone"], ward["startTime"])
            title = format_broadcast_title(ward, scheduled_start, config["timezone"])
            try:
                broadcast = find_upcoming_broadcast(channel_youtube, title, scheduled_start)
            except Exception as exc:
                problems.append(f"YouTube could not check the upcoming broadcast ({type(exc).__name__}).")
                continue
            if not broadcast:
                problems.append(f"No upcoming Sacrament Meeting broadcast is scheduled for {scheduled_start.astimezone(tz).strftime('%m/%d/%Y')}.")
                continue

            state = broadcast.get("status", {}).get("lifeCycleStatus")
            if state not in ("created", "ready"):
                problems.append(f"The {title} broadcast has unexpected status {state or 'unknown'}.")
            content = broadcast.get("contentDetails", {})
            if content.get("boundStreamId") != ward["streamID"]:
                problems.append(f"The {title} broadcast is not bound to its configured Sacrament stream.")
            if broadcast.get("status", {}).get("privacyStatus") != UNLISTED:
                problems.append(f"The {title} broadcast is not unlisted.")

            try:
                playlist = channel_youtube.playlists().list(part="status", id=ward["playlistID"], maxResults=1).execute().get("items", [])
                if not playlist:
                    problems.append(f"The configured playlist for {channel['channelName']} was not found.")
                elif playlist[0].get("status", {}).get("privacyStatus") != UNLISTED:
                    problems.append(f"The configured playlist for {channel['channelName']} is not unlisted.")
                page_token = None
                found = False
                while True:
                    response = channel_youtube.playlistItems().list(
                        part="snippet,contentDetails",
                        playlistId=ward["playlistID"],
                        maxResults=50,
                        pageToken=page_token,
                    ).execute()
                    for item in response.get("items", []):
                        video_id = item.get("contentDetails", {}).get("videoId") or item.get("snippet", {}).get("resourceId", {}).get("videoId")
                        if video_id == broadcast["id"]:
                            found = True
                            break
                    if found:
                        break
                    page_token = response.get("nextPageToken")
                    if not page_token:
                        break
                if not found:
                    problems.append(f"The {title} broadcast is missing from its Sacrament playlist.")
            except Exception as exc:
                problems.append(f"YouTube could not check the Sacrament playlist ({type(exc).__name__}).")

            try:
                streams = channel_youtube.liveStreams().list(part="id,status", id=ward["streamID"]).execute().get("items", [])
                if not streams:
                    problems.append(f"The configured Sacrament stream for {channel['channelName']} was not found.")
            except Exception as exc:
                problems.append(f"YouTube could not check the configured stream ({type(exc).__name__}).")

    camera_url = config.get("cameraHealthUrl")
    if camera_url:
        reachable, detail = _camera_is_reachable(camera_url)
        if not reachable:
            problems.append(detail)
    plugin_ok, plugin_detail = _plugin_is_available()
    if not plugin_ok:
        problems.append(plugin_detail)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only broadcast readiness check")
    parser.add_argument("--config", default="/etc/yt-scheduler/config.json")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        problems = check_readiness(config)
    except Exception as exc:
        problems = [f"Broadcast health check failed ({type(exc).__name__})."]

    if problems:
        message = "Broadcast health check found problems:\n" + "\n".join(f"• {problem}" for problem in problems)
        delivered = send_problem_notification(message)
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        print("Problem notification submitted." if delivered else "Problem notification delivery failed.", file=sys.stderr)
        return 1

    print("Broadcast readiness check passed; no stream was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
