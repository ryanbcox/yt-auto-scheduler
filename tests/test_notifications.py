import json
import unittest
from unittest.mock import Mock, patch

from notifications import send_problem_notification


class NotificationTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "BROADCAST_PROBLEMS_WEBHOOK_URL": "https://notify.example/webhook/token",
        "BROADCAST_PROBLEMS_WEBHOOK_SECRET": "body-secret",
    })
    @patch("notifications.urlopen")
    def test_posts_double_secret_payload_as_json(self, urlopen):
        response = Mock(status=200)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        urlopen.return_value = context

        self.assertTrue(send_problem_notification("Test broadcast alert"))

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://notify.example/webhook/token")
        self.assertEqual(json.loads(request.data), {"secret": "body-secret", "message": "Test broadcast alert"})

    @patch.dict("os.environ", {
        "BROADCAST_PROBLEMS_WEBHOOK_URL": "http://notify.example/webhook/token",
        "BROADCAST_PROBLEMS_WEBHOOK_SECRET": "body-secret",
    })
    @patch("notifications.urlopen")
    def test_refuses_non_https_webhook(self, urlopen):
        self.assertFalse(send_problem_notification("Test"))
        urlopen.assert_not_called()
