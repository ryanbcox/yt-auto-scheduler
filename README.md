# YouTube Auto Scheduler

This repository is a private working copy of [evade-ninja/yt-auto-scheduler](https://github.com/evade-ninja/yt-auto-scheduler). Its public source is retained as the `upstream` Git remote. This copy schedules one upcoming Sunday live broadcast per configured ward and adds it to the configured playlist. An optional Monday cleanup timer deletes the previous Sunday’s completed Sacrament recording.

The public information and privacy pages for the app are in [`docs/`](docs/), intended for the Google OAuth branding page.

## Hobble Creek 7th Ward defaults

- Sunday broadcast time: 9:00 AM, `America/Denver`
- Title: `Hobble Creek 7th Ward: Sacrament Meeting - MM/DD/YYYY`
- Broadcast visibility: unlisted
- Category: People & Blogs
- Audience: not Made for Kids; not age restricted
- Auto-start, auto-stop, DVR, and archive recording: enabled
- Embedding: disabled
- Playlist: `Hobble Creek 7th Ward Sacrament Meetings` (`PLBQacIHDjC2k`), set to unlisted

The YouTube API does not expose the selected live-chat, replay, reaction, slow-mode, or React Live options shown in Studio. Set those channel defaults in Studio if they must apply to every event.

## Configuration and credentials

Copy `sample.config.json` to `/etc/yt-scheduler/config.json`, replace the stream ID placeholder with the YouTube **stream resource ID** from `list-streams`, and keep each account's OAuth client file in `/etc/yt-scheduler/`. OAuth token files belong in `/var/lib/yt-scheduler/`. Neither credentials nor runtime config should be committed.

The service runs as a dedicated account. A typical host setup is:

```sh
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin yt-scheduler
sudo install -d -o root -g yt-scheduler -m 0750 /etc/yt-scheduler
sudo install -d -o yt-scheduler -g yt-scheduler -m 0750 /var/lib/yt-scheduler
sudo install -o root -g yt-scheduler -m 0640 sample.config.json /etc/yt-scheduler/config.json
```

Install the Google OAuth client file into `/etc/yt-scheduler/` with the same read permissions for the `yt-scheduler` group. Keep token files readable only by the service account.

Use a dedicated `yt-scheduler` system account. Install dependencies into `/opt/yt-scheduler/venv` from `requirements.txt`, then run the interactive authorization command once per configured account:

```sh
sudo -u yt-scheduler /opt/yt-scheduler/venv/bin/python /opt/yt-scheduler/schedule.py auth --config /etc/yt-scheduler/config.json
```

OAuth setup needs a browser for the initial consent flow. Do not put YouTube RTMP stream keys in this project; the scheduler needs only the YouTube stream resource ID.

## Manual operation

Preview the next event without contacting YouTube:

```sh
python3 schedule.py schedule --config sample.config.json --dry-run
```

Create or reconcile the next event after credentials and real IDs are configured:

```sh
python3 schedule.py schedule --config /etc/yt-scheduler/config.json
```

The operation is safe to retry: it reuses an upcoming event with the same title and scheduled start, repairs its stream binding, and adds it to the playlist only if it is missing. It also ensures the playlist is unlisted while preserving its existing title and description.

`list-streams.py` displays stream IDs and titles only; it deliberately does not request or print ingest URLs or stream keys.

## One-time special events

The bcast-control panel lists all upcoming broadcasts, defaults to the earliest
special event before the next Sunday meeting on non-Sundays, and defaults to
Sacrament Meeting on Sundays. Its bottom link opens a form for a one-time
unlisted event. Each special event gets its own YouTube stream resource and
key; it is not added to a playlist. The existing broadcast ID is retained
when an upcoming event is assigned a dedicated stream, so its share URL does
not change.

The web panel invokes the fixed `web-*` CLI actions over stdin under the
`yt-scheduler` account. These commands return event metadata only. The selected
stream key is written to `/var/lib/yt-scheduler/selected-stream.env` with mode
`0600`; the relay service reads it as an optional override to its configured
Sacrament stream key. Apache Basic Auth protects the page, and state-changing
web requests require a CSRF token. The relay destination can only be changed
while the relay is stopped.

YouTube Studio still controls comments, live chat, and automatic live
captions. Use the Studio link for each event to turn off interaction settings
and enable automatic English captions when the channel supports them.

## Weekly timer

The systemd service and timer templates are in `systemd/`. Install them on the broadcast host only after a manual event has been checked in YouTube Studio. The timer runs Monday at 9:00 AM Mountain time and creates the following Sunday's 9:00 AM event. The timer is not enabled by this repository setup.

The separate `yt-scheduler-delete.service` and `yt-scheduler-delete.timer`
templates delete the previous Sunday's completed Sacrament Meeting video every
Monday at 9:00 AM Mountain time. The cleanup matches the exact ward title and
local date, removes that video's configured playlist entry, and then permanently
deletes the video. It skips cleanup if there is no exact completed match or if
multiple matches are found.

The `yt-scheduler-health.service` and `yt-scheduler-health.timer` templates run
a read-only readiness check Thursday through Sunday at 7:00 AM Mountain time.
The check refreshes/validates YouTube OAuth, verifies the next Sunday broadcast,
stream binding, unlisted playlist membership, and local stream/camera software
availability. It never starts a broadcast. Health failures and Monday cleanup
problems are sent through the configured Home Assistant webhook to the supplied
phone notification. Set `BROADCAST_PROBLEMS_WEBHOOK_URL` and
`BROADCAST_PROBLEMS_WEBHOOK_SECRET` in `/etc/yt-scheduler/notifications.env`,
owned by root and readable by the `yt-scheduler` group (mode `0640`). The webhook
request is HTTPS-only; do not commit that environment file.

The upstream `remove_videos.py` and `hide_videos.py` scripts remain for reference but are not used by this phase.
