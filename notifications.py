"""Send private problem alerts through the configured Home Assistant webhook."""
from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def send_problem_notification(message: str) -> bool:
    """Submit an alert without logging the bearer URL or body secret."""
    webhook_url = os.environ.get("BROADCAST_PROBLEMS_WEBHOOK_URL", "")
    secret = os.environ.get("BROADCAST_PROBLEMS_WEBHOOK_SECRET", "")
    if not webhook_url or not secret or urlparse(webhook_url).scheme != "https":
        print("problem notification is not configured with an HTTPS webhook", file=sys.stderr)
        return False

    payload = json.dumps({"secret": secret, "message": message}).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if 200 <= response.status < 300:
                return True
            print(f"problem notification endpoint returned HTTP {response.status}", file=sys.stderr)
    except HTTPError as exc:
        print(f"problem notification endpoint returned HTTP {exc.code}", file=sys.stderr)
    except (URLError, TimeoutError, OSError) as exc:
        print(f"problem notification delivery failed ({type(exc).__name__})", file=sys.stderr)
    return False
