#!/usr/bin/env python3
"""Create the next weekly YouTube live broadcast for each configured ward."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import google.auth.transport.requests
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from notifications import send_problem_notification

SCOPES = ["https://www.googleapis.com/auth/youtube"]
DEFAULT_CONFIG = os.environ.get("YT_SCHEDULER_CONFIG", "config.json")
WEB_CONFIG = os.environ.get("YT_SCHEDULER_CONFIG", "/etc/yt-scheduler/config.json")
CATEGORY_PEOPLE_AND_BLOGS = "22"
UNLISTED = "unlisted"
STREAM_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


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

    target_path = config.get("relayTargetEnvPath", "/var/lib/yt-scheduler/selected-stream.env")
    if not isinstance(target_path, str) or not Path(target_path).is_absolute():
        raise ValueError("relayTargetEnvPath must be an absolute path")
    camera_url = config.get("cameraHealthUrl")
    if camera_url is not None and (not isinstance(camera_url, str) or not camera_url.startswith(("http://", "https://"))):
        raise ValueError("cameraHealthUrl must be an HTTP or HTTPS URL")


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

    if credentials and credentials.valid:
        return build("youtube", "v3", credentials=credentials)

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(google.auth.transport.requests.Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            token_path.chmod(0o600)
        except RefreshError:
            if not interactive:
                raise
            credentials = None

    if not credentials or not credentials.valid:
        if not interactive:
            raise RuntimeError(
                f"OAuth token missing or invalid for {channel['channelName']}; "
                "run the interactive auth command before enabling the timer"
            )
        if not secret_path.exists():
            raise FileNotFoundError(f"OAuth client file not found: {secret_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        credentials = flow.run_local_server(port=8080, open_browser=False)
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
            broadcastType="event",
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


def delete_previous_sacrament_recording(youtube, config: dict, *, now: datetime | None = None) -> dict:
    """Delete the most recent completed Sunday Sacrament recording, if found."""
    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    tz = ZoneInfo(config["timezone"])
    local_now = (now or datetime.now(tz)).astimezone(tz)
    days_since_sunday = (local_now.weekday() + 1) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7
    target_date = local_now.date() - timedelta(days=days_since_sunday)
    local_start = datetime.combine(target_date, time.fromisoformat(ward["startTime"]), tzinfo=tz)
    expected_title = format_broadcast_title(ward, local_start, config["timezone"])

    matches = []
    page_token = None
    while True:
        response = youtube.liveBroadcasts().list(
            part="id,snippet,status",
            broadcastStatus="completed",
            broadcastType="event",
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for broadcast in response.get("items", []):
            snippet = broadcast.get("snippet", {})
            scheduled_start = snippet.get("scheduledStartTime")
            if snippet.get("title") != expected_title or not scheduled_start:
                continue
            start_local_date = parse_api_time(scheduled_start).astimezone(tz).date()
            if start_local_date == target_date and snippet.get("actualEndTime"):
                matches.append(broadcast)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if not matches:
        return {"deleted": False, "reason": "no_completed_recording", "title": expected_title}
    if len(matches) != 1:
        raise RuntimeError(f"found {len(matches)} completed broadcasts matching {expected_title!r}; refusing deletion")

    broadcast = matches[0]
    video_id = broadcast.get("id")
    if not video_id:
        raise RuntimeError("the completed broadcast did not include a video ID")
    if broadcast.get("status", {}).get("lifeCycleStatus") != "complete":
        raise RuntimeError("the matching Sacrament broadcast is not complete; refusing deletion")

    video_response = youtube.videos().list(part="id,snippet", id=video_id).execute()
    videos = video_response.get("items", [])
    if videos and videos[0].get("snippet", {}).get("title") != expected_title:
        raise RuntimeError("the video title did not match the completed Sacrament broadcast; refusing deletion")

    # Remove the playlist membership first, so a failure deleting the video
    # can be retried without leaving a stale playlist entry behind.
    playlist_id = ward["playlistID"]
    playlist_items_to_remove = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="id,snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            item_video_id = item.get("contentDetails", {}).get("videoId")
            if not item_video_id:
                item_video_id = item.get("snippet", {}).get("resourceId", {}).get("videoId")
            if item_video_id == video_id:
                playlist_items_to_remove.append(item["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    for playlist_item_id in playlist_items_to_remove:
        youtube.playlistItems().delete(id=playlist_item_id).execute()

    if videos:
        youtube.videos().delete(id=video_id).execute()
        result_reason = "deleted"
    else:
        result_reason = "already_deleted"
    return {"deleted": True, "reason": result_reason, "id": video_id, "title": expected_title}


def delete_previous_sacrament_config(config: dict, *, now: datetime, dry_run: bool = False) -> list[dict]:
    """Delete each configured ward's most recent completed Sunday recording."""
    validate_config(config, allow_placeholders=dry_run)
    results = []
    for channel in config["channels"]:
        youtube = None if dry_run else authenticate_youtube(channel)
        for ward in channel["wards"]:
            local_now = now.astimezone(ZoneInfo(config["timezone"]))
            days_since_sunday = (local_now.weekday() + 1) % 7 or 7
            previous_sunday = local_now.date() - timedelta(days=days_since_sunday)
            local_start = datetime.combine(previous_sunday, time.fromisoformat(ward["startTime"]), tzinfo=ZoneInfo(config["timezone"]))
            title = format_broadcast_title(ward, local_start, config["timezone"])
            if dry_run:
                print(f"Would delete the completed recording {title}")
                results.append({"deleted": False, "reason": "dry_run", "title": title})
            else:
                one_ward_config = dict(config, channels=[dict(channel, wards=[ward])])
                result = delete_previous_sacrament_recording(youtube, one_ward_config, now=now)
                print(f"{result['reason']}: {result['title']}")
                results.append(result)
    return results


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


def _local_start(date_value: str, time_value: str, tz_name: str) -> datetime:
    try:
        local_date = date.fromisoformat(date_value)
        wall_time = time.fromisoformat(time_value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD and time must be HH:MM") from exc
    if wall_time.tzinfo is not None:
        raise ValueError("time must be local wall time without a timezone")
    local = datetime.combine(local_date, wall_time, tzinfo=ZoneInfo(tz_name))
    round_trip = local.astimezone(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    if round_trip != local.replace(tzinfo=None):
        raise ValueError("the selected local time does not exist because of a daylight-saving transition")
    return local.astimezone(timezone.utc)


def _ward_for_channel(channel: dict) -> dict:
    if len(channel["wards"]) != 1:
        raise ValueError("special-event scheduling currently requires exactly one ward per channel")
    return channel["wards"][0]


def _is_sacrament(title: str, ward: dict) -> bool:
    return title.startswith(f"{ward['wardName']}: {ward['eventName']} - ")


def _list_all_upcoming(youtube) -> list[dict]:
    items = []
    page_token = None
    while True:
        response = youtube.liveBroadcasts().list(
            part="id,snippet,status,contentDetails",
            broadcastStatus="upcoming",
            broadcastType="event",
            maxResults=50,
            pageToken=page_token,
        ).execute()
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def list_upcoming_events(youtube, config: dict, *, now: datetime | None = None) -> dict:
    """List upcoming events without exposing stream keys and compute the dashboard default."""
    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    sacrament_stream_ids = {item["streamID"] for item in channel["wards"]}
    local_now = (now or datetime.now(ZoneInfo(config["timezone"]))).astimezone(ZoneInfo(config["timezone"]))
    next_sacrament = next_sunday_start(local_now, config["timezone"], ward["startTime"])

    events = []
    for item in _list_all_upcoming(youtube):
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        title = snippet.get("title", "")
        start = snippet.get("scheduledStartTime")
        if not start:
            continue
        start_utc = parse_api_time(start)
        bound_stream = content.get("boundStreamId")
        is_sacrament = _is_sacrament(title, ward)
        events.append({
            "id": item["id"],
            "title": title,
            "startTime": to_api_time(start_utc),
            "isSacrament": is_sacrament,
            "streamId": bound_stream,
            "needsDedicatedStream": bool(not is_sacrament and bound_stream in sacrament_stream_ids),
            "shareUrl": f"https://youtu.be/{item['id']}",
            "studioUrl": f"https://studio.youtube.com/video/{item['id']}/edit",
        })
    events.sort(key=lambda event: event["startTime"])

    sacrament_events = [event for event in events if event["isSacrament"]]
    next_sacrament_event = next(
        (event for event in sacrament_events if parse_api_time(event["startTime"]) >= local_now.astimezone(timezone.utc)),
        None,
    )
    if local_now.weekday() == 6:
        default_event = next_sacrament_event
    else:
        cutoff = next_sacrament
        if next_sacrament_event:
            event_start = parse_api_time(next_sacrament_event["startTime"])
            if event_start <= cutoff:
                cutoff = event_start
        default_event = next((
            event for event in events
            if not event["isSacrament"]
            and not event["needsDedicatedStream"]
            and local_now.astimezone(timezone.utc) <= parse_api_time(event["startTime"]) < cutoff
        ), None) or next_sacrament_event

    return {
        "events": events,
        "defaultBroadcastId": default_event["id"] if default_event else None,
        "playlistUrl": f"https://www.youtube.com/playlist?list={ward['playlistID']}",
        "timezone": config["timezone"],
    }


def _find_reusable_stream(youtube, stream_title: str) -> dict | None:
    page_token = None
    while True:
        response = youtube.liveStreams().list(
            part="id,snippet,cdn,contentDetails",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for stream in response.get("items", []):
            if stream.get("snippet", {}).get("title") == stream_title:
                return stream
        page_token = response.get("nextPageToken")
        if not page_token:
            return None


def ensure_dedicated_stream(youtube, broadcast: dict) -> dict:
    """Create/reuse a distinct stream resource for a special broadcast and bind it."""
    broadcast_id = broadcast["id"]
    stream_title = f"YT Scheduler special event {broadcast_id}"
    stream = _find_reusable_stream(youtube, stream_title)
    if not stream:
        stream = youtube.liveStreams().insert(
            part="snippet,cdn,contentDetails",
            body={
                "snippet": {"title": stream_title},
                "cdn": {
                    "ingestionType": "rtmp",
                    "resolution": "variable",
                    "frameRate": "variable",
                },
                "contentDetails": {"isReusable": True},
            },
        ).execute()
    stream_id = stream.get("id")
    if not stream_id:
        raise RuntimeError("YouTube did not return an ID for the dedicated event stream")
    current_stream_id = broadcast.get("contentDetails", {}).get("boundStreamId")
    if current_stream_id != stream_id:
        updated = youtube.liveBroadcasts().bind(
            part="id,contentDetails",
            id=broadcast_id,
            streamId=stream_id,
        ).execute()
        broadcast.setdefault("contentDetails", {})["boundStreamId"] = (
            updated.get("contentDetails", {}).get("boundStreamId") or stream_id
        )
    return stream


def create_special_event(youtube, config: dict, request: dict, *, now: datetime | None = None) -> dict:
    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    title = request.get("title")
    description = request.get("description", "")
    if not isinstance(title, str) or not 3 <= len(title.strip()) <= 100:
        raise ValueError("title must be between 3 and 100 characters")
    if not isinstance(description, str) or len(description) > 5000:
        raise ValueError("description must be at most 5000 characters")
    title = title.strip()
    if _is_sacrament(title, ward):
        raise ValueError("special-event titles cannot use the Sacrament Meeting title pattern")
    start = _local_start(request.get("date", ""), request.get("time", ""), config["timezone"])
    local_now = (now or datetime.now(ZoneInfo(config["timezone"]))).astimezone(timezone.utc)
    if start <= local_now:
        raise ValueError("special-event start time must be in the future")

    broadcast = find_upcoming_broadcast(youtube, title, start)
    if not broadcast:
        event = dict(ward)
        event["description"] = description.strip()
        broadcast = youtube.liveBroadcasts().insert(
            part="snippet,contentDetails,status",
            body=broadcast_body(event, title, start),
        ).execute()
        if not broadcast.get("id"):
            raise RuntimeError("YouTube did not return a broadcast ID")

    stream = ensure_dedicated_stream(youtube, broadcast)
    return {
        "id": broadcast["id"],
        "title": title,
        "startTime": to_api_time(start),
        "streamId": stream["id"],
        "shareUrl": f"https://youtu.be/{broadcast['id']}",
        "studioUrl": f"https://studio.youtube.com/video/{broadcast['id']}/edit",
    }


def assign_dedicated_stream(youtube, config: dict, broadcast_id: str) -> dict:
    response = youtube.liveBroadcasts().list(
        part="id,snippet,status,contentDetails", id=broadcast_id
    ).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError("broadcast was not found or is not accessible")
    broadcast = items[0]
    if broadcast.get("status", {}).get("lifeCycleStatus") not in ("created", "ready"):
        raise ValueError("only upcoming broadcasts can be assigned a dedicated stream")
    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    title = broadcast.get("snippet", {}).get("title", "")
    if _is_sacrament(title, ward):
        raise ValueError("the Sacrament Meeting broadcast must keep its configured stream")
    sacrament_streams = {item["streamID"] for item in channel["wards"]}
    if broadcast.get("contentDetails", {}).get("boundStreamId") not in sacrament_streams:
        raise ValueError("this event already has a separate stream")
    stream = ensure_dedicated_stream(youtube, broadcast)
    return {
        "id": broadcast_id,
        "title": broadcast.get("snippet", {}).get("title", ""),
        "startTime": broadcast.get("snippet", {}).get("scheduledStartTime", ""),
        "streamId": stream["id"],
        "shareUrl": f"https://youtu.be/{broadcast_id}",
        "studioUrl": f"https://studio.youtube.com/video/{broadcast_id}/edit",
    }


def delete_special_event(youtube, config: dict, broadcast_id: str) -> dict:
    """Delete an upcoming non-Sacrament broadcast without touching recordings."""
    if not isinstance(broadcast_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", broadcast_id):
        raise ValueError("invalid broadcast ID")
    response = youtube.liveBroadcasts().list(
        part="id,snippet,status,contentDetails", id=broadcast_id
    ).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError("broadcast was not found or is not accessible")
    broadcast = items[0]
    if broadcast.get("status", {}).get("lifeCycleStatus") not in ("created", "ready"):
        raise ValueError("only upcoming special events can be deleted")

    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    title = broadcast.get("snippet", {}).get("title", "")
    if _is_sacrament(title, ward):
        raise ValueError("the Sacrament Meeting broadcast cannot be deleted here")
    stream_id = broadcast.get("contentDetails", {}).get("boundStreamId")
    if not stream_id:
        raise ValueError("the broadcast has no dedicated stream")
    if any(stream_id == configured_ward["streamID"] for configured_ward in channel["wards"]):
        raise ValueError("assign a dedicated stream before deleting this special event")

    youtube.liveBroadcasts().delete(id=broadcast_id).execute()
    return {"id": broadcast_id, "title": title, "deleted": True}


def select_relay_target(youtube, config: dict, broadcast_id: str) -> str:
    if not isinstance(broadcast_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", broadcast_id):
        raise ValueError("invalid broadcast ID")
    response = youtube.liveBroadcasts().list(
        part="id,snippet,status,contentDetails", id=broadcast_id
    ).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError("broadcast was not found or is not accessible")
    broadcast = items[0]
    if broadcast.get("status", {}).get("lifeCycleStatus") not in ("created", "ready"):
        raise ValueError("only upcoming broadcasts can be selected")
    stream_id = broadcast.get("contentDetails", {}).get("boundStreamId")
    if not stream_id:
        raise ValueError("broadcast has no bound stream")

    channel = config["channels"][0]
    ward = _ward_for_channel(channel)
    title = broadcast.get("snippet", {}).get("title", "")
    if not _is_sacrament(title, ward) and any(
        stream_id == configured_ward["streamID"] for configured_ward in channel["wards"]
    ):
        raise ValueError("assign this special event a dedicated stream before selecting it")

    response = youtube.liveStreams().list(part="id,cdn", id=stream_id).execute()
    streams = response.get("items", [])
    if not streams:
        raise ValueError("the broadcast's stream was not found")
    stream_key = streams[0].get("cdn", {}).get("ingestionInfo", {}).get("streamName", "")
    if not STREAM_KEY_RE.fullmatch(stream_key):
        raise ValueError("YouTube returned an invalid stream key")

    target_path = Path(config.get("relayTargetEnvPath", "/var/lib/yt-scheduler/selected-stream.env"))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target_file:
            target_file.write(f"YOUTUBE_STREAM_KEY={stream_key}\n")
            target_file.flush()
            os.fsync(target_file.fileno())
        os.replace(temp_path, target_path)
        target_path.chmod(0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return title


def _web_response(action: str) -> dict:
    config = load_config(WEB_CONFIG)
    channel = config["channels"][0]
    youtube = authenticate_youtube(channel)
    if action == "web-upcoming":
        return list_upcoming_events(youtube, config)
    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON request") from exc
    if not isinstance(request, dict):
        raise ValueError("request body must be a JSON object")
    if action == "web-create-special":
        return create_special_event(youtube, config, request)
    if action == "web-assign-stream":
        return assign_dedicated_stream(youtube, config, str(request.get("broadcastId", "")))
    if action == "web-delete-special":
        return delete_special_event(youtube, config, str(request.get("broadcastId", "")))
    if action == "web-select-target":
        title = select_relay_target(youtube, config, str(request.get("broadcastId", "")))
        return {"selected": True, "title": title}
    raise ValueError("unknown web action")


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

    cleanup = commands.add_parser("delete-previous-sacrament", help="Delete the previous Sunday Sacrament recording(s)")
    cleanup.add_argument("--config", default=DEFAULT_CONFIG)
    cleanup.add_argument("--dry-run", action="store_true", help="Show the target recording without deleting videos")

    auth = commands.add_parser("auth", help="Run one-time interactive OAuth setup")
    auth.add_argument("--config", default=DEFAULT_CONFIG)
    auth.add_argument("--channel", help="Channel name; if omitted, authenticate all configured channels")

    streams = commands.add_parser("list-streams", help="List stream IDs without revealing RTMP stream keys")
    streams.add_argument("--config", default=DEFAULT_CONFIG)
    commands.add_parser("web-upcoming", help=argparse.SUPPRESS)
    commands.add_parser("web-create-special", help=argparse.SUPPRESS)
    commands.add_parser("web-assign-stream", help=argparse.SUPPRESS)
    commands.add_parser("web-select-target", help=argparse.SUPPRESS)
    commands.add_parser("web-delete-special", help=argparse.SUPPRESS)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command.startswith("web-"):
            print(json.dumps(_web_response(args.command)))
            return 0
        config = load_config(args.config, allow_placeholders=True)
        if args.command == "schedule":
            schedule_config(config, now=datetime.now(ZoneInfo(config["timezone"])), dry_run=args.dry_run)
        elif args.command == "delete-previous-sacrament":
            try:
                results = delete_previous_sacrament_config(
                    config, now=datetime.now(ZoneInfo(config["timezone"])), dry_run=args.dry_run
                )
            except Exception as exc:
                if not args.dry_run:
                    if isinstance(exc, RefreshError):
                        message = "Monday Sacrament cleanup could not authenticate with YouTube. Reauthorize the scheduler before the next cleanup."
                    else:
                        message = f"Monday Sacrament cleanup failed ({type(exc).__name__}). Check the scheduler journal."
                    send_problem_notification(message)
                raise
            missing = [result for result in results if result.get("reason") == "no_completed_recording"]
            if missing and not args.dry_run:
                titles = "; ".join(result["title"] for result in missing)
                send_problem_notification(
                    f"Monday Sacrament cleanup found no completed recording to delete: {titles}. Check whether the meeting streamed and finished."
                )
                return 1
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
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
