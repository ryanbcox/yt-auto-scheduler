import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from schedule import (
    broadcast_body,
    ensure_playlist_unlisted,
    find_upcoming_broadcast,
    format_broadcast_title,
    next_sunday_start,
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


if __name__ == "__main__":
    unittest.main()
