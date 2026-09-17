# Pi Jukebox

A shared request queue for a Raspberry Pi wired to speakers. Guests open a page on
the local network, search YouTube Music, and add songs. The queue rotates between
people so nobody can hog the room.

## Hardware

The Pi 3B's 3.5mm jack shares a power rail with the rest of the board and sounds
noticeably noisy at volume. Options, roughly in order of cost:

- **USB DAC** (~$10 and up) — cheapest real fix, works with no config beyond picking the device.
- **I2S DAC HAT** (HiFiBerry DAC+ or similar) — best quality, needs a `dtoverlay` line in `/boot/firmware/config.txt`.
- **HDMI** — fine if the Pi is already going into an AV receiver.

The 3B is 2.4GHz wifi only, and it shares the bus with USB. If the DAC is USB and
the stream is over wifi, buffering matters — that's what the generous `--cache`
settings in `mpv.py` are for. Ethernet removes the problem entirely.

## Install

```bash
sudo apt update && sudo apt install -y mpv python3-venv
git clone <your repo> ~/jukebox && cd ~/jukebox
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Find your audio device and test it before involving any of this code:

```bash
mpv --audio-device=help
mpv --audio-device=alsa/hw:1,0 /usr/share/sounds/alsa/Front_Center.wav
```

Then run it:

```bash
JUKEBOX_AUDIO_DEVICE=alsa/hw:1,0 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open `http://<pi-address>:8080` from a phone on the same network. To install as a
service, edit the paths in `jukebox.service`, then:

```bash
sudo cp jukebox.service /etc/systemd/system/
sudo systemctl enable --now jukebox
journalctl -u jukebox -f
```

## Configuration

All via environment variables (see `app/config.py`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `JUKEBOX_AUDIO_DEVICE` | system default | mpv device string |
| `JUKEBOX_VOLUME` | `85` | starting volume (0–130) |
| `JUKEBOX_MAX_PER_PERSON` | `3` | songs one person can have waiting |
| `JUKEBOX_MAX_DURATION` | `720` | reject tracks longer than this, in seconds |
| `JUKEBOX_SKIP_VOTES` | `2` | votes needed to skip someone else's song |
| `JUKEBOX_ADMIN_TOKEN` | unset | enables volume control and force-remove |

## How the pieces fit

`mpv.py` owns a single mpv process started in idle mode with a JSON IPC socket.
Commands go out as one JSON object per line with a `request_id`; a reader task
matches replies back to futures and watches for `end-file` events. Because mpv is
a separate process, restarting the API server does not interrupt the current song.

`music.py` does two separate jobs. `ytmusicapi` handles search — it returns proper
song metadata (artist, album, duration) rather than the video titles you'd get from
scraping YouTube proper, and needs no credentials. `yt-dlp` turns a video id into a
stream URL. That call takes a few seconds on a 3B, so the player resolves the next
track's URL while the current one plays. Stream URLs are signed and expire, which is
why resolution happens at play time rather than at submission time.

`queue.py` orders by fewest plays per submitter, then by when the song was added.
Someone joining mid-party starts level with the lowest active count so they get a
turn soon without displacing everyone already waiting.

`main.py` runs a single player task: pop a track, resolve it, play it, wait for
`end-file`, repeat. A skip is just `stop`, which produces the same `end-file` the
loop is already waiting on.

## Things worth adding next

- **A QR code on the wall.** `qrcode` can render one for the Pi's URL at startup — far easier than reading an IP address aloud.
- **mDNS.** `avahi-daemon` gets you `http://jukebox.local:8080` instead of a changing DHCP address.
- **Server-sent events instead of polling.** Twenty phones polling every 2.5s is about 8 requests/sec; the 3B copes, but SSE is cheaper and makes the queue feel live.
- **A fallback playlist.** When the queue empties, drop into a preselected radio mix so the room doesn't go silent.
- **Persistence.** History in SQLite lets you show "played earlier" and block repeats across the night.

One caveat: pulling audio out of YouTube this way is against YouTube's terms of
service. It's the standard approach for a home setup like this, but it isn't
something to build a product on, and `yt-dlp` occasionally breaks for a day or two
when Google changes something — keep it updated.
