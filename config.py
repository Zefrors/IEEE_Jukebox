import os

MPV_SOCKET = os.environ.get("JUKEBOX_MPV_SOCKET", "/tmp/jukebox-mpv.sock")
AUDIO_DEVICE = os.environ.get("JUKEBOX_AUDIO_DEVICE", "")  # e.g. alsa/hw:1,0
START_VOLUME = int(os.environ.get("JUKEBOX_VOLUME", "85"))

MAX_PER_PERSON = int(os.environ.get("JUKEBOX_MAX_PER_PERSON", "3"))
MAX_QUEUE = int(os.environ.get("JUKEBOX_MAX_QUEUE", "60"))
MAX_DURATION = int(os.environ.get("JUKEBOX_MAX_DURATION", str(12 * 60)))  # seconds

SKIP_VOTES = int(os.environ.get("JUKEBOX_SKIP_VOTES", "2"))
ADMIN_TOKEN = os.environ.get("JUKEBOX_ADMIN_TOKEN", "")  # empty = admin routes off

HOST = os.environ.get("JUKEBOX_HOST", "0.0.0.0")
PORT = int(os.environ.get("JUKEBOX_PORT", "8080"))
