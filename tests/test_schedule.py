import copy
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from google.auth.exceptions import RefreshError

from schedule import (
    authenticate_youtube,
    broadcast_body,
    assign_dedicated_stream,
    create_special_event,
    delete_previous_sacrament_recording,
    delete_special_event,
    ensure_playlist_unlisted,
    find_upcoming_broadcast,
    format_broadcast_title,
    list_upcoming_events,
    next_sunday_start,
    _local_start,
    select_relay_target,
    schedule_config,
    schedule_ward,
)


WARD = {
    "wardName": "Hobble Creek 7th Ward",
    "eventName": "Sacrament Meeting",
    "description": "",
    "startTime": "09:00:00",
    "streamID": "stream-hc7",
    "playlistID": "playlist-hc7",
}
CHANNEL = {
    "channelName": "Hobble Creek 7th Ward",
    "secretPath": "/unused/client.json",
    "tokenPath": "/unused/token.json",
    "wards": [WARD],
}


def unlisted_playlist_response():
    return {
        "items": [{
            "id": "playlist-hc7",
            "snippet": {"title": "Hobble Creek 7th Ward Sacrament Meetings", "description": ""},
            "status": {"privacyStatus": "unlisted"},
        }]
    }


class ScheduleTimeTests(unittest.TestCase):
    def test_title_uses_scheduled_local_date(self):
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(
            format_broadcast_title(WARD, start, "America/Denver"),
            "Hobble Creek 7th Ward: Sacrament Meeting - 10/25/2026",
        )

    def test_next_sunday_across_spring_dst_transition(self):
        now = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Denver"))
        self.assertEqual(
            next_sunday_start(now, "America/Denver", "09:00:00"),
            datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
        )

    def test_next_sunday_across_fall_dst_transition(self):
        now = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("America/Denver"))
        self.assertEqual(
            next_sunday_start(now, "America/Denver", "09:00:00"),
            datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc),
        )

    def test_sunday_before_start_uses_today_and_at_start_moves_a_week(self):
        zone = ZoneInfo("America/Denver")
        before = datetime(2026, 10, 25, 8, 59, tzinfo=zone)
        at_start = datetime(2026, 10, 25, 9, 0, tzinfo=zone)
        self.assertEqual(next_sunday_start(before, "America/Denver", "09:00:00"),
                         datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(next_sunday_start(at_start, "America/Denver", "09:00:00"),
                         datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc))

    def test_broadcast_body_matches_selected_settings(self):
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        body = broadcast_body(WARD, "title", start)
        self.assertEqual(body["snippet"]["categoryId"], "22")
        self.assertEqual(body["snippet"]["scheduledStartTime"], "2026-10-25T15:00:00Z")
        self.assertEqual(body["status"], {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        })
        self.assertEqual(body["contentDetails"], {
            "enableAutoStart": True,
            "enableAutoStop": True,
            "enableDvr": True,
            "enableEmbed": False,
            "recordFromStart": True,
        })


class YouTubeApiTests(unittest.TestCase):
    def make_youtube(self, playlist_response=None, broadcast_response=None, items_response=None):
        youtube = Mock()
        youtube.playlists.return_value.list.return_value.execute.return_value = (
            playlist_response or unlisted_playlist_response()
        )
        youtube.playlists.return_value.update.return_value.execute.return_value = {}
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = (
            broadcast_response or {"items": []}
        )
        youtube.liveBroadcasts.return_value.insert.return_value.execute.return_value = {
            "id": "broadcast-new",
            "contentDetails": {},
        }
        youtube.liveBroadcasts.return_value.bind.return_value.execute.return_value = {}
        youtube.playlistItems.return_value.list.return_value.execute.return_value = (
            items_response or {"items": []}
        )
        youtube.playlistItems.return_value.insert.return_value.execute.return_value = {}
        return youtube

    def test_interactive_auth_falls_back_to_consent_after_revoked_refresh_token(self):
        old_credentials = Mock(valid=False, expired=True, refresh_token="old-refresh-token")
        old_credentials.refresh.side_effect = RefreshError("invalid_grant")
        new_credentials = Mock(valid=True)
        new_credentials.to_json.return_value = '{"access_token":"new"}'
        flow = Mock()
        flow.run_local_server.return_value = new_credentials
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token.json"
            secret_path = Path(temp_dir) / "client.json"
            token_path.write_text('{"refresh_token":"old"}', encoding="utf-8")
            secret_path.write_text("{}", encoding="utf-8")
            channel = {"tokenPath": str(token_path), "secretPath": str(secret_path), "channelName": "Test"}
            with patch("schedule.Credentials.from_authorized_user_file", return_value=old_credentials), \
                    patch("schedule.InstalledAppFlow.from_client_secrets_file", return_value=flow), \
                    patch("schedule.build", return_value="youtube-client"):
                result = authenticate_youtube(channel, interactive=True)

            self.assertEqual(result, "youtube-client")
            flow.run_local_server.assert_called_once_with(port=8080, open_browser=False)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"access_token":"new"}')
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)

    def test_playlist_visibility_update_preserves_existing_metadata(self):
        response = {"items": [{
            "id": "playlist-hc7",
            "snippet": {
                "title": "Existing title",
                "description": "Keep this description",
                "tags": ["ward", "meeting"],
                "defaultLanguage": "en",
            },
            "localizations": {"es": {"title": "Reuniones sacramentales", "description": ""}},
            "status": {"privacyStatus": "public", "podcastStatus": "disabled"},
        }]}
        youtube = self.make_youtube(playlist_response=response)

        ensure_playlist_unlisted(youtube, "playlist-hc7")

        youtube.playlists.return_value.update.assert_called_once_with(
            part="snippet,status",
            body={
                "id": "playlist-hc7",
                "snippet": {
                    "title": "Existing title",
                    "description": "Keep this description",
                    "tags": ["ward", "meeting"],
                    "defaultLanguage": "en",
                },
                "localizations": {"es": {"title": "Reuniones sacramentales", "description": ""}},
                "status": {"privacyStatus": "unlisted", "podcastStatus": "disabled"},
            },
        )

    def test_new_broadcast_is_created_bound_and_added_to_playlist(self):
        youtube = self.make_youtube()
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)

        result = schedule_ward(youtube, CHANNEL, WARD, start, "America/Denver")

        self.assertEqual(result["id"], "broadcast-new")
        inserted = youtube.liveBroadcasts.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(inserted["snippet"]["title"], "Hobble Creek 7th Ward: Sacrament Meeting - 10/25/2026")
        youtube.liveBroadcasts.return_value.bind.assert_called_once_with(
            part="id,contentDetails", id="broadcast-new", streamId="stream-hc7"
        )
        youtube.playlistItems.return_value.insert.assert_called_once_with(
            part="snippet",
            body={"snippet": {
                "playlistId": "playlist-hc7",
                "resourceId": {"kind": "youtube#video", "videoId": "broadcast-new"},
            }},
        )

    def test_retry_reuses_matching_event_without_duplicate_calls(self):
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        title = format_broadcast_title(WARD, start, "America/Denver")
        existing = {
            "id": "broadcast-existing",
            "snippet": {"title": title, "scheduledStartTime": "2026-10-25T15:00:00Z"},
            "contentDetails": {"boundStreamId": "stream-hc7"},
        }
        items = {"items": [{"contentDetails": {"videoId": "broadcast-existing"}}]}
        youtube = self.make_youtube(
            broadcast_response={"items": [existing]}, items_response=items
        )

        result = schedule_ward(youtube, CHANNEL, WARD, start, "America/Denver")

        self.assertEqual(result["id"], "broadcast-existing")
        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_not_called()
        youtube.playlistItems.return_value.insert.assert_not_called()

    def test_retry_repairs_missing_binding_and_playlist_item(self):
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        title = format_broadcast_title(WARD, start, "America/Denver")
        existing = {
            "id": "broadcast-existing",
            "snippet": {"title": title, "scheduledStartTime": "2026-10-25T15:00:00Z"},
            "contentDetails": {},
        }
        youtube = self.make_youtube(broadcast_response={"items": [existing]})

        schedule_ward(youtube, CHANNEL, WARD, start, "America/Denver")

        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_called_once()
        youtube.playlistItems.return_value.insert.assert_called_once()

    def test_finds_exact_event_after_first_api_page(self):
        start = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
        title = format_broadcast_title(WARD, start, "America/Denver")
        youtube = self.make_youtube()
        youtube.liveBroadcasts.return_value.list.return_value.execute.side_effect = [
            {"items": [], "nextPageToken": "next"},
            {"items": [{"id": "broadcast-page-two", "snippet": {
                "title": title, "scheduledStartTime": "2026-10-25T15:00:00Z"
            }}]},
        ]

        found = find_upcoming_broadcast(youtube, title, start)

        self.assertEqual(found["id"], "broadcast-page-two")
        self.assertEqual(
            youtube.liveBroadcasts.return_value.list.call_args_list[1].kwargs["pageToken"], "next"
        )

    def test_dry_run_supports_multiple_wards_and_different_stream_ids(self):
        second_ward = copy.deepcopy(WARD)
        second_ward.update({
            "wardName": "Nearby Ward",
            "eventName": "Sacrament Meeting",
            "startTime": "10:30:00",
            "streamID": "stream-nearby",
            "playlistID": "playlist-nearby",
        })
        config = {"timezone": "America/Denver", "channels": [dict(CHANNEL, wards=[WARD, second_ward])]}
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        results = schedule_config(config, now=now, dry_run=True)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026")
        self.assertEqual(results[1]["title"], "Nearby Ward: Sacrament Meeting - 11/01/2026")
        self.assertNotEqual(second_ward["streamID"], WARD["streamID"])

    def test_each_ward_binds_its_configured_stream_id(self):
        second_ward = copy.deepcopy(WARD)
        second_ward.update({
            "wardName": "Nearby Ward",
            "startTime": "10:30:00",
            "streamID": "stream-nearby",
            "playlistID": "playlist-nearby",
        })
        config = {"timezone": "America/Denver", "channels": [dict(CHANNEL, wards=[WARD, second_ward])]}
        youtube = self.make_youtube()
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        with patch("schedule.authenticate_youtube", return_value=youtube):
            schedule_config(config, now=now)

        bind_stream_ids = [
            call.kwargs["streamId"]
            for call in youtube.liveBroadcasts.return_value.bind.call_args_list
        ]
        self.assertEqual(bind_stream_ids, ["stream-hc7", "stream-nearby"])


class SpecialEventTests(unittest.TestCase):
    def upcoming_response(self, items):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": items}
        return youtube

    def test_upcoming_selector_defaults_to_earliest_special_before_sunday(self):
        items = [
            {"id": "sac", "snippet": {"title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026", "scheduledStartTime": "2026-11-01T16:00:00Z"}, "contentDetails": {"boundStreamId": "stream-hc7"}},
            {"id": "funeral", "snippet": {"title": "Oneita Fackrell's Funeral - October 31, 2026", "scheduledStartTime": "2026-10-31T17:00:00Z"}, "contentDetails": {"boundStreamId": "stream-funeral"}},
            {"id": "later", "snippet": {"title": "Christmas Program", "scheduledStartTime": "2026-12-20T17:00:00Z"}, "contentDetails": {"boundStreamId": "stream-later"}},
        ]
        youtube = self.upcoming_response(items)
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = list_upcoming_events(youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, now=now)

        self.assertEqual(result["defaultBroadcastId"], "funeral")
        self.assertEqual([event["id"] for event in result["events"]], ["funeral", "sac", "later"])
        self.assertEqual(result["events"][0]["shareUrl"], "https://youtu.be/funeral")

    def test_delete_previous_sacrament_removes_exact_sunday_video_and_playlist_entry(self):
        youtube = Mock()
        title = "Hobble Creek 7th Ward: Sacrament Meeting - 11/08/2026"
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "sacrament-video", "snippet": {
                "title": title,
                "scheduledStartTime": "2026-11-08T16:00:00Z",
                "actualEndTime": "2026-11-08T17:00:00Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        }]}
        youtube.videos.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "sacrament-video", "snippet": {"title": title}
        }]}
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "playlist-item", "contentDetails": {"videoId": "sacrament-video"}
        }]}
        now = datetime(2026, 11, 9, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = delete_previous_sacrament_recording(
            youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, now=now
        )

        self.assertEqual(result, {"deleted": True, "reason": "deleted", "id": "sacrament-video", "title": title})
        youtube.playlistItems.return_value.delete.assert_called_once_with(id="playlist-item")
        youtube.videos.return_value.delete.assert_called_once_with(id="sacrament-video")

    def test_delete_previous_sacrament_skips_when_exact_completed_recording_is_missing(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [
            {"id": "other", "snippet": {"title": "Funeral - 11/08/2026", "scheduledStartTime": "2026-11-08T16:00:00Z"}, "status": {"lifeCycleStatus": "complete"}}
        ]}
        now = datetime(2026, 11, 9, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = delete_previous_sacrament_recording(
            youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, now=now
        )

        self.assertEqual(result["reason"], "no_completed_recording")
        youtube.videos.return_value.delete.assert_not_called()

    def test_sunday_defaults_to_sacrament_even_if_special_is_upcoming(self):
        items = [
            {"id": "sac", "snippet": {"title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026", "scheduledStartTime": "2026-11-01T16:00:00Z"}, "contentDetails": {"boundStreamId": "stream-hc7"}},
            {"id": "special", "snippet": {"title": "Funeral", "scheduledStartTime": "2026-10-31T17:00:00Z"}, "contentDetails": {"boundStreamId": "stream-funeral"}},
        ]
        now = datetime(2026, 10, 25, 8, 0, tzinfo=ZoneInfo("America/Denver"))

        result = list_upcoming_events(self.upcoming_response(items), {"timezone": "America/Denver", "channels": [CHANNEL]}, now=now)

        self.assertEqual(result["defaultBroadcastId"], "sac")

    def test_shared_sacrament_stream_marks_special_unsafe_and_skips_default(self):
        items = [
            {"id": "sac", "snippet": {"title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026", "scheduledStartTime": "2026-11-01T16:00:00Z"}, "contentDetails": {"boundStreamId": "stream-hc7"}},
            {"id": "funeral", "snippet": {"title": "Funeral", "scheduledStartTime": "2026-10-31T17:00:00Z"}, "contentDetails": {"boundStreamId": "stream-hc7"}},
        ]
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = list_upcoming_events(self.upcoming_response(items), {"timezone": "America/Denver", "channels": [CHANNEL]}, now=now)

        funeral = next(event for event in result["events"] if event["id"] == "funeral")
        self.assertTrue(funeral["needsDedicatedStream"])
        self.assertEqual(result["defaultBroadcastId"], "sac")

    def test_create_special_event_uses_unique_stream_and_never_playlist(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": []}
        youtube.liveBroadcasts.return_value.insert.return_value.execute.return_value = {
            "id": "broadcast-special", "contentDetails": {}
        }
        youtube.liveBroadcasts.return_value.bind.return_value.execute.return_value = {
            "contentDetails": {"boundStreamId": "stream-special"}
        }
        youtube.liveStreams.return_value.list.return_value.execute.return_value = {"items": []}
        youtube.liveStreams.return_value.insert.return_value.execute.return_value = {"id": "stream-special"}
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = create_special_event(youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, {
            "title": "Hobble Creek 7th Ward: Funeral - 10/31/2026",
            "description": "Service details",
            "date": "2026-10-31",
            "time": "11:00",
        }, now=now)

        self.assertEqual(result["shareUrl"], "https://youtu.be/broadcast-special")
        self.assertEqual(result["streamId"], "stream-special")
        body = youtube.liveBroadcasts.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")
        self.assertEqual(body["contentDetails"]["enableAutoStart"], True)
        self.assertEqual(body["snippet"]["scheduledStartTime"], "2026-10-31T17:00:00Z")
        youtube.playlists.assert_not_called()
        youtube.playlistItems.assert_not_called()

    def test_assign_dedicated_stream_rebinds_existing_broadcast_without_recreating(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "existing-funeral", "snippet": {"title": "Oneita Fackrell's Funeral"},
            "status": {"lifeCycleStatus": "ready"},
            "contentDetails": {"boundStreamId": "stream-hc7"},
        }]}
        youtube.liveStreams.return_value.list.return_value.execute.return_value = {"items": []}
        youtube.liveStreams.return_value.insert.return_value.execute.return_value = {"id": "stream-funeral"}
        youtube.liveBroadcasts.return_value.bind.return_value.execute.return_value = {
            "contentDetails": {"boundStreamId": "stream-funeral"}
        }

        result = assign_dedicated_stream(youtube, {"channels": [CHANNEL]}, "existing-funeral")

        self.assertEqual(result["id"], "existing-funeral")
        self.assertEqual(result["shareUrl"], "https://youtu.be/existing-funeral")
        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_called_once_with(
            part="id,contentDetails", id="existing-funeral", streamId="stream-funeral"
        )

    def test_assign_dedicated_stream_protects_sacrament_broadcast(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "sacrament", "snippet": {"title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026"},
            "status": {"lifeCycleStatus": "ready"},
            "contentDetails": {"boundStreamId": "stream-hc7"},
        }]}

        with self.assertRaisesRegex(ValueError, "must keep its configured stream"):
            assign_dedicated_stream(youtube, {"channels": [CHANNEL]}, "sacrament")

        youtube.liveStreams.return_value.list.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_not_called()

    def test_delete_special_event_only_deletes_upcoming_non_sacrament_with_dedicated_stream(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "funeral-123", "snippet": {"title": "Oneita Fackrell's Funeral"},
            "status": {"lifeCycleStatus": "ready"},
            "contentDetails": {"boundStreamId": "stream-funeral"},
        }]}

        result = delete_special_event(youtube, {"channels": [CHANNEL]}, "funeral-123")

        self.assertEqual(result, {"id": "funeral-123", "title": "Oneita Fackrell's Funeral", "deleted": True})
        youtube.liveBroadcasts.return_value.delete.assert_called_once_with(id="funeral-123")

    def test_delete_special_event_protects_sacrament_and_completed_events(self):
        youtube = Mock()
        list_execute = youtube.liveBroadcasts.return_value.list.return_value.execute
        list_execute.return_value = {"items": [{
            "id": "sacrament", "snippet": {"title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026"},
            "status": {"lifeCycleStatus": "ready"}, "contentDetails": {"boundStreamId": "stream-hc7"},
        }]}
        with self.assertRaisesRegex(ValueError, "cannot be deleted"):
            delete_special_event(youtube, {"channels": [CHANNEL]}, "sacrament")

        list_execute.return_value = {"items": [{
            "id": "old-event", "snippet": {"title": "Old event"},
            "status": {"lifeCycleStatus": "complete"}, "contentDetails": {"boundStreamId": "stream-old"},
        }]}
        with self.assertRaisesRegex(ValueError, "only upcoming"):
            delete_special_event(youtube, {"channels": [CHANNEL]}, "old-event")
        youtube.liveBroadcasts.return_value.delete.assert_not_called()

    def test_special_create_rejects_sacrament_title_pattern(self):
        youtube = Mock()
        request = {
            "title": "Hobble Creek 7th Ward: Sacrament Meeting - 11/01/2026",
            "date": "2026-11-01", "time": "09:00", "description": "",
        }
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        with self.assertRaisesRegex(ValueError, "cannot use the Sacrament Meeting title pattern"):
            create_special_event(youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, request, now=now)

        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveStreams.return_value.insert.assert_not_called()

    def test_select_target_writes_key_only_to_private_env_file(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "special-123", "snippet": {"title": "Oneita Funeral"},
            "status": {"lifeCycleStatus": "ready"},
            "contentDetails": {"boundStreamId": "stream-funeral"},
        }]}
        youtube.liveStreams.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "stream-funeral", "cdn": {"ingestionInfo": {"streamName": "secretStreamKey123"}}
        }]}
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "selected-stream.env"
            config = {"channels": [CHANNEL], "relayTargetEnvPath": str(target)}

            title = select_relay_target(youtube, config, "special-123")

            self.assertEqual(title, "Oneita Funeral")
            self.assertEqual(target.read_text(), "YOUTUBE_STREAM_KEY=secretStreamKey123\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_select_target_rejects_special_event_using_sacrament_stream(self):
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "special-123", "snippet": {"title": "Funeral"},
            "status": {"lifeCycleStatus": "ready"},
            "contentDetails": {"boundStreamId": "stream-hc7"},
        }]}
        with self.assertRaisesRegex(ValueError, "dedicated stream"):
            select_relay_target(youtube, {"channels": [CHANNEL]}, "special-123")
        youtube.liveStreams.return_value.list.assert_not_called()

    def test_nonexistent_mountain_time_is_rejected_at_spring_dst_boundary(self):
        with self.assertRaisesRegex(ValueError, "daylight-saving"):
            _local_start("2026-03-08", "02:30", "America/Denver")

    def test_retry_reuses_existing_event_and_stream_without_creating_duplicates(self):
        title = "Hobble Creek 7th Ward: Funeral - 10/31/2026"
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "broadcast-special",
            "snippet": {"title": title, "scheduledStartTime": "2026-10-31T17:00:00Z"},
            "contentDetails": {"boundStreamId": "stream-special"},
        }]}
        youtube.liveStreams.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "stream-special", "snippet": {"title": "YT Scheduler special event broadcast-special"}
        }]}
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        result = create_special_event(youtube, {"timezone": "America/Denver", "channels": [CHANNEL]}, {
            "title": title, "date": "2026-10-31", "time": "11:00", "description": ""
        }, now=now)

        self.assertEqual(result["id"], "broadcast-special")
        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveStreams.return_value.insert.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_not_called()

    def test_retry_after_stream_creation_failure_recovers_existing_stream(self):
        title = "Hobble Creek 7th Ward: Funeral - 10/31/2026"
        new_broadcast = {"id": "broadcast-special", "contentDetails": {}}
        existing_broadcast = {
            "id": "broadcast-special",
            "snippet": {"title": title, "scheduledStartTime": "2026-10-31T17:00:00Z"},
            "contentDetails": {},
        }
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.side_effect = [
            {"items": []}, {"items": [existing_broadcast]}
        ]
        youtube.liveBroadcasts.return_value.insert.return_value.execute.return_value = new_broadcast
        youtube.liveBroadcasts.return_value.bind.return_value.execute.side_effect = [
            RuntimeError("temporary bind failure"),
            {"contentDetails": {"boundStreamId": "stream-special"}},
        ]
        youtube.liveStreams.return_value.list.return_value.execute.side_effect = [
            {"items": []},
            {"items": [{"id": "stream-special", "snippet": {"title": "YT Scheduler special event broadcast-special"}}]},
        ]
        youtube.liveStreams.return_value.insert.return_value.execute.return_value = {"id": "stream-special"}
        config = {"timezone": "America/Denver", "channels": [CHANNEL]}
        request = {"title": title, "date": "2026-10-31", "time": "11:00", "description": ""}
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("America/Denver"))

        with self.assertRaisesRegex(RuntimeError, "bind failure"):
            create_special_event(youtube, config, request, now=now)
        result = create_special_event(youtube, config, request, now=now)

        self.assertEqual(result["id"], "broadcast-special")
        youtube.liveBroadcasts.return_value.insert.assert_called_once()
        youtube.liveStreams.return_value.insert.assert_called_once()
        self.assertEqual(youtube.liveBroadcasts.return_value.bind.call_count, 2)


if __name__ == "__main__":
    unittest.main()
