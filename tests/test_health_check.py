import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from health_check import check_readiness
from schedule import next_sunday_start


WARD = {
    "wardName": "Hobble Creek 7th Ward",
    "eventName": "Sacrament Meeting",
    "startTime": "09:00:00",
    "streamID": "stream-hc7",
    "playlistID": "playlist-hc7",
}
CHANNEL = {"channelName": "Hobble Creek 7th Ward", "wards": [WARD]}
CONFIG = {"timezone": "America/Denver", "cameraHealthUrl": "http://camera/", "channels": [CHANNEL]}


class HealthCheckTests(unittest.TestCase):
    def make_youtube(self, now):
        start = next_sunday_start(now, CONFIG["timezone"], WARD["startTime"])
        title = f"{WARD['wardName']}: {WARD['eventName']} - {start.astimezone(ZoneInfo(CONFIG['timezone'])).strftime('%m/%d/%Y')}"
        youtube = Mock()
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "broadcast-sunday",
            "snippet": {"title": title, "scheduledStartTime": start.isoformat().replace("+00:00", "Z")},
            "status": {"lifeCycleStatus": "ready", "privacyStatus": "unlisted"},
            "contentDetails": {"boundStreamId": WARD["streamID"]},
        }]}
        youtube.playlists.return_value.list.return_value.execute.return_value = {"items": [{
            "status": {"privacyStatus": "unlisted"}
        }]}
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {"items": [{
            "contentDetails": {"videoId": "broadcast-sunday"}
        }]}
        youtube.liveStreams.return_value.list.return_value.execute.return_value = {"items": [{"id": WARD["streamID"]}]}
        return youtube

    def test_read_only_check_accepts_expected_upcoming_broadcast_and_components(self):
        now = datetime(2026, 11, 12, 7, 0, tzinfo=ZoneInfo("America/Denver"))
        with patch("health_check._camera_is_reachable", return_value=(True, "camera reachable")), \
                patch("health_check._plugin_is_available", return_value=(True, "NDI plugin available")):
            problems = check_readiness(CONFIG, now=now, youtube=self.make_youtube(now))
        self.assertEqual(problems, [])

    def test_check_reports_missing_sunday_broadcast_without_creating_one(self):
        now = datetime(2026, 11, 12, 7, 0, tzinfo=ZoneInfo("America/Denver"))
        youtube = self.make_youtube(now)
        youtube.liveBroadcasts.return_value.list.return_value.execute.return_value = {"items": []}
        with patch("health_check._camera_is_reachable", return_value=(True, "camera reachable")), \
                patch("health_check._plugin_is_available", return_value=(True, "NDI plugin available")):
            problems = check_readiness(CONFIG, now=now, youtube=youtube)
        self.assertTrue(any("No upcoming Sacrament Meeting broadcast" in problem for problem in problems))
        youtube.liveBroadcasts.return_value.insert.assert_not_called()
        youtube.liveBroadcasts.return_value.bind.assert_not_called()

    def test_check_reports_oauth_refresh_error(self):
        now = datetime(2026, 11, 12, 7, 0, tzinfo=ZoneInfo("America/Denver"))
        with patch("health_check.authenticate_youtube", side_effect=Exception("revoked")):
            problems = check_readiness(CONFIG, now=now)
        self.assertTrue(any("YouTube authentication failed" in problem for problem in problems))
