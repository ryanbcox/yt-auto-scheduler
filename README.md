# YouTube Auto Scheduler

This repository is a private working copy of [evade-ninja/yt-auto-scheduler](https://github.com/evade-ninja/yt-auto-scheduler). Its public source is retained as the `upstream` Git remote. This copy schedules one upcoming Sunday live broadcast per configured ward and adds it to the configured playlist. It does not delete recordings or schedule cleanup jobs.

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

## Weekly timer

The systemd service and timer templates are in `systemd/`. Install them on the broadcast host only after a manual event has been checked in YouTube Studio. The timer runs Monday at 9:00 AM Mountain time and creates the following Sunday's 9:00 AM event. The timer is not enabled by this repository setup.

The upstream `remove_videos.py` and `hide_videos.py` scripts remain for reference but are not used by this phase.
