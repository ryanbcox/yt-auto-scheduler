#!/usr/bin/env python3
"""Create the next weekly YouTube live broadcast for each configured ward."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube"]
DEFAULT_CONFIG = os.environ.get("YT_SCHEDULER_CONFIG", "config.json")
CATEGORY_PEOPLE_AND_BLOGS = "22"
UNLISTED = "unlisted"


def load_config(path: str | Path, *, allow_placeholders: bool = False) -> dict:
    with open(path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    validate_config(config, allow_placeholders=allow_placeholders)
    return config


def validate_config(config: dict, *, allow_placeholders: bool = False) -> None:
    if not isinstance(config.get("timezone"), str):
        raise ValueError("config must define a timezone")
    try:
        ZoneInfo(config["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {config['timezone']}") from exc

    channels = config.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("config.channels must be a non-empty list")

    for channel in channels:
        for key in ("channelName", "secretPath", "tokenPath"):
            if not isinstance(channel.get(key), str) or not channel[key].strip():
                raise ValueError(f"each channel must define {key}")
        wards = channel.get("wards")
        if not isinstance(wards, list) or not wards:
            raise ValueError(f"{channel['channelName']}: wards must be a non-empty list")
        for ward in wards:
            for key in ("wardName", "eventName", "startTime", "streamID", "playlistID"):
                if not isinstance(ward.get(key), str) or not ward[key].strip():
                    raise ValueError(f"{channel['channelName']}: each ward must define {key}")
            try:
                parsed_time = time.fromisoformat(ward["startTime"])
            except ValueError as exc:
                raise ValueError(
                    f"{channel['channelName']}/{ward['wardName']}: startTime must be HH:MM or HH:MM:SS"
                ) from exc
            if parsed_time.tzinfo is not None:
                raise ValueError("ward startTime must be a local wall-clock time without a timezone")
            if not allow_placeholders:
                for key in ("streamID", "playlistID"):
                    if ward[key].startswith("FILL_IN_"):
                        raise ValueError(f"{channel['channelName']}/{ward['wardName']}: replace {key} placeholder")


def next_sunday_start(now: datetime, tz_name: str, start_time: str) -> datetime:
    """Return the next future Sunday start as an aware UTC datetime."""
    tz = ZoneInfo(tz_name)
    local_now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    wall_time = time.fromisoformat(start_time)
    if wall_time.tzinfo is not None:
        raise ValueError("start_time must not include a timezone")

    days_until_sunday = (6 - local_now.weekday()) % 7
    target_date = local_now.date() + timedelta(days=days_until_sunday)
    candidate = datetime.combine(target_date, wall_time, tzinfo=tz)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def format_broadcast_title(ward: dict, scheduled_start: datetime, tz_name: str) -> str:
    local_date = scheduled_start.astimezone(ZoneInfo(tz_name)).strftime("%m/%d/%Y")
    return f"{ward['wardName']}: {ward['eventName']} - {local_date}"


def to_api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_api_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def broadcast_body(ward: dict, title: str, scheduled_start: datetime) -> dict:
    return {
        "snippet": {
            "title": title,
            "description": ward.get("description", ""),
            "scheduledStartTime": to_api_time(scheduled_start),
            "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
        },
        "status": {
            "privacyStatus": UNLISTED,
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": True,
            "enableAutoStop": True,
            "enableDvr": True,
            "enableEmbed": False,
            "recordFromStart": True,
        },
    }


def authenticate_youtube(channel: dict, *, interactive: bool = False):
    token_path = Path(channel["tokenPath"])
    secret_path = Path(channel["secretPath"])
    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(google.auth.transport.requests.Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    elif not credentials or not credentials.valid:
        if not interactive:
            raise RuntimeError(
                f"OAuth token missing or invalid for {channel['channelName']}; "
                "run the interactive auth command before enabling the timer"
            )
        if not secret_path.exists():
            raise FileNotFoundError(f"OAuth client file not found: {secret_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        credentials = flow.run_local_server(port=8080)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        token_path.chmod(0o600)

    return build("youtube", "v3", credentials=credentials)


def ensure_playlist_unlisted(youtube, playlist_id: str) -> None:
    response = youtube.playlists().list(
        part="snippet,status", id=playlist_id, maxResults=1
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(f"playlist {playlist_id} was not found or is not accessible")

    playlist = items[0]
    if playlist.get("status", {}).get("privacyStatus") == UNLISTED:
        return

    current_snippet = playlist.get("snippet", {})
    snippet = {key: current_snippet[key] for key in (
        "title", "description", "tags", "defaultLanguage"
    ) if key in current_snippet}
    if "title" not in snippet:
        raise RuntimeError(f"playlist {playlist_id} response did not include its title")

    status = {"privacyStatus": UNLISTED}
    current_status = playlist.get("status", {})
    if "podcastStatus" in current_status:
        status["podcastStatus"] = current_status["podcastStatus"]

    update_body = {"id": playlist_id, "snippet": snippet, "status": status}
    if "localizations" in playlist:
        update_body["localizations"] = playlist["localizations"]
    youtube.playlists().update(part="snippet,status", body=update_body).execute()


def find_upcoming_broadcast(youtube, title: str, scheduled_start: datetime):
    page_token = None
    while True:
        response = youtube.liveBroadcasts().list(
            part="id,snippet,status,contentDetails",
            broadcastStatus="upcoming",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for broadcast in response.get("items", []):
            snippet = broadcast.get("snippet", {})
            existing_start = snippet.get("scheduledStartTime")
            if snippet.get("title") == title and existing_start:
                if parse_api_time(existing_start) == scheduled_start.astimezone(timezone.utc):
                    return broadcast
        page_token = response.get("nextPageToken")
        if not page_token:
            return None


def ensure_playlist_contains_video(youtube, playlist_id: str, video_id: str) -> None:
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            item_video_id = item.get("contentDetails", {}).get("videoId")
            if not item_video_id:
                item_video_id = item.get("snippet", {}).get("resourceId", {}).get("videoId")
            if item_video_id == video_id:
                return
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def schedule_ward(
    youtube, channel: dict, ward: dict, scheduled_start: datetime, tz_name: str
) -> dict:
    title = format_broadcast_title(ward, scheduled_start, tz_name)
    playlist_id = ward["playlistID"]
    stream_id = ward["streamID"]

    ensure_playlist_unlisted(youtube, playlist_id)
    broadcast = find_upcoming_broadcast(youtube, title, scheduled_start)
    if broadcast:
        print(f"Reusing existing broadcast {broadcast['id']} for {channel['channelName']}/{ward['wardName']}")
    else:
        broadcast = youtube.liveBroadcasts().insert(
            part="snippet,contentDetails,status",
            body=broadcast_body(ward, title, scheduled_start),
        ).execute()
        if not broadcast.get("id"):
            raise RuntimeError(f"YouTube did not return a broadcast ID for {title}")
        print(f"Created broadcast {broadcast['id']}: {title}")

    bound_stream_id = broadcast.get("contentDetails", {}).get("boundStreamId")
    if bound_stream_id != stream_id:
        youtube.liveBroadcasts().bind(
            part="id,contentDetails",
            id=broadcast["id"],
            streamId=stream_id,
        ).execute()
        print(f"Bound broadcast {broadcast['id']} to stream {stream_id}")

    ensure_playlist_contains_video(youtube, playlist_id, broadcast["id"])
    return broadcast


def schedule_config(config: dict, *, now: datetime, dry_run: bool = False) -> list[dict]:
    validate_config(config, allow_placeholders=dry_run)
    results = []
    for channel in config["channels"]:
        youtube = None if dry_run else authenticate_youtube(channel)
        for ward in channel["wards"]:
            scheduled_start = next_sunday_start(now, config["timezone"], ward["startTime"])
            title = format_broadcast_title(ward, scheduled_start, config["timezone"])
            if dry_run:
                print(f"Would schedule {title} at {to_api_time(scheduled_start)}")
                results.append({"title": title, "scheduledStartTime": to_api_time(scheduled_start)})
            else:
                results.append(schedule_ward(youtube, channel, ward, scheduled_start, config["timezone"]))
    return results


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Schedule recurring YouTube live broadcasts")
    commands = root.add_subparsers(dest="command", required=True)

    schedule = commands.add_parser("schedule", help="Schedule the next Sunday broadcast(s)")
    schedule.add_argument("--config", default=DEFAULT_CONFIG)
    schedule.add_argument("--dry-run", action="store_true", help="Show planned broadcasts without YouTube API calls")

    auth = commands.add_parser("auth", help="Run one-time interactive OAuth setup")
    auth.add_argument("--config", default=DEFAULT_CONFIG)
    auth.add_argument("--channel", help="Channel name; if omitted, authenticate all configured channels")

    streams = commands.add_parser("list-streams", help="List stream IDs without revealing RTMP stream keys")
    streams.add_argument("--config", default=DEFAULT_CONFIG)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config, allow_placeholders=True)
        if args.command == "schedule":
            schedule_config(config, now=datetime.now(ZoneInfo(config["timezone"])), dry_run=args.dry_run)
        elif args.command == "auth":
            channels = config["channels"]
            if args.channel:
                channels = [channel for channel in channels if channel["channelName"] == args.channel]
                if not channels:
                    raise ValueError(f"no configured channel named {args.channel!r}")
            for channel in channels:
                authenticate_youtube(channel, interactive=True)
                print(f"OAuth token saved for {channel['channelName']}")
        elif args.command == "list-streams":
            for channel in config["channels"]:
                youtube = authenticate_youtube(channel)
                page_token = None
                while True:
                    response = youtube.liveStreams().list(
                        part="id,snippet", mine=True, maxResults=50, pageToken=page_token
                    ).execute()
                    for stream in response.get("items", []):
                        print(f"{channel['channelName']}\t{stream['snippet']['title']}\t{stream['id']}")
                    page_token = response.get("nextPageToken")
                    if not page_token:
                        break
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
