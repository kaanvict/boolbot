"""BoolBot - a Discord voice "radio" bot driven by slash commands and yt-dlp.

Configuration comes from environment variables or a `.env` file next to this
script (see `.env.example`). The bot token is never stored in this file.
"""

import asyncio
import glob
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("boolbot")


# ─── Configuration ───────────────────────────────────────

def load_env_file(path):
    """Minimal .env reader: KEY=VALUE lines, '#' comments, optional quotes."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(BASE_DIR / ".env")

BOOLBOT_CHANNEL_NAME = os.environ.get("BOOLBOT_CHANNEL", "boolbot")
VOLUME = float(os.environ.get("BOOLBOT_VOLUME", "0.5"))
LOCK_PORT = int(os.environ.get("BOOLBOT_LOCK_PORT", "47631"))

# Never let a child process open a console window (matters under pythonw.exe).
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# yt-dlp needs a JavaScript runtime for YouTube. It only looks for deno by
# default, so node is enabled as a fallback.
JS_RUNTIMES = ("deno", "node")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "js_runtimes": {name: {} for name in JS_RUNTIMES},
}


def find_ffmpeg():
    """FFMPEG_PATH from the environment, then PATH, then the winget install dir."""
    configured = os.environ.get("FFMPEG_PATH")
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        pattern = os.path.join(
            local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"
        )
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[-1]
    return None


def python_console_exe():
    """The interpreter used to run yt-dlp as a child process.

    Under pythonw.exe the child must be python.exe so its stdout pipe works;
    NO_WINDOW keeps it from opening a console.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


FFMPEG_PATH = find_ffmpeg()
YTDLP_CMD = [python_console_exe(), "-m", "yt_dlp"]


def setup_logging():
    handlers = [
        logging.handlers.RotatingFileHandler(
            BASE_DIR / "boolbot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    ]
    if sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# ─── Bot + per-guild state ───────────────────────────────

intents = discord.Intents.default()  # slash commands + voice need no privileged intents
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

queues = {}  # guild_id -> list of track dicts
current_track = {}  # guild_id -> track dict
bot_channel = {}  # guild_id -> text channel
play_locks = {}  # guild_id -> asyncio.Lock guarding "start the next track"


def get_queue(guild_id):
    return queues.setdefault(guild_id, [])


def get_lock(guild_id):
    return play_locks.setdefault(guild_id, asyncio.Lock())


async def get_bot_channel(guild):
    ch = bot_channel.get(guild.id)
    if ch and guild.get_channel(ch.id):
        return ch
    for ch in guild.text_channels:
        if ch.name == BOOLBOT_CHANNEL_NAME:
            bot_channel[guild.id] = ch
            return ch
    return None


async def announce(guild, msg, interaction=None):
    """Post to the #boolbot channel, and answer the interaction if there is one."""
    ch = await get_bot_channel(guild)
    if ch and (interaction is None or ch.id != interaction.channel_id):
        try:
            await ch.send(msg)
        except discord.HTTPException as exc:
            log.warning("Could not post to #%s: %s", ch.name, exc)
    if interaction is not None:
        await interaction.followup.send(msg)


def format_duration(seconds):
    if not seconds:
        return "??:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def describe(track):
    return f"**{track['title']}** [{format_duration(track['duration'])}]"


# ─── yt-dlp ──────────────────────────────────────────────

def search_track(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise LookupError(f"no results for {query!r}")
            info = entries[0]
        return {
            "title": info.get("title") or "Unknown",
            "webpage_url": info.get("webpage_url") or info.get("url") or query,
            "duration": info.get("duration") or 0,
        }


class YTDLSource(discord.AudioSource):
    """Audio source that pipes yt-dlp output through FFmpeg into Discord."""

    FRAME_SIZE = 3840  # 20ms of 48kHz 16-bit stereo

    def __init__(self, webpage_url):
        self.frames = 0
        self.error = None  # set when the stream ends without producing any audio
        self._ytdlp_log = tempfile.TemporaryFile()
        self._ffmpeg_log = tempfile.TemporaryFile()
        js_args = []
        for name in JS_RUNTIMES:
            js_args += ["--js-runtimes", name]
        self.process = subprocess.Popen(
            [
                *YTDLP_CMD,
                "-f", "bestaudio/best",
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                *js_args,
                "-o", "-",
                webpage_url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._ytdlp_log,
            creationflags=NO_WINDOW,
        )
        try:
            self.ffmpeg = subprocess.Popen(
                [
                    FFMPEG_PATH,
                    "-nostdin",
                    "-i", "pipe:0",
                    "-vn",
                    "-f", "s16le",
                    "-ar", "48000",
                    "-ac", "2",
                    "-loglevel", "error",
                    "pipe:1",
                ],
                stdin=self.process.stdout,
                stdout=subprocess.PIPE,
                stderr=self._ffmpeg_log,
                creationflags=NO_WINDOW,
            )
        except Exception:
            self.process.kill()
            self.process.wait()
            self._ytdlp_log.close()
            self._ffmpeg_log.close()
            raise
        finally:
            self.process.stdout.close()  # ffmpeg owns the read end now

    def read(self):
        data = self.ffmpeg.stdout.read(self.FRAME_SIZE)
        if len(data) != self.FRAME_SIZE:
            if self.frames == 0:
                self.error = self._read_error() or "no audio was produced"
            return b""
        self.frames += 1
        return data

    def _read_error(self):
        """Last stderr line from yt-dlp (the usual culprit), else from ffmpeg."""
        for proc in (self.process, self.ffmpeg):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for logfile in (self._ytdlp_log, self._ffmpeg_log):
            try:
                logfile.seek(0)
                text = logfile.read().decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                continue
            if text:
                return text.splitlines()[-1][:300]
        return ""

    def is_opus(self):
        return False

    def cleanup(self):
        for proc in (self.ffmpeg, self.process):
            try:
                proc.kill()
            except Exception:
                pass
        for proc in (self.ffmpeg, self.process):
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        for stream in (self.ffmpeg.stdout, self._ytdlp_log, self._ffmpeg_log):
            try:
                stream.close()
            except Exception:
                pass


# ─── Playback ────────────────────────────────────────────

def is_active(vc):
    return bool(vc) and (vc.is_playing() or vc.is_paused())


async def start_next(guild):
    """Start the next queued track if the bot is connected and idle.

    Returns the track that started, or None. Safe to call from anywhere: the
    per-guild lock stops two callers from starting playback at once.
    """
    async with get_lock(guild.id):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            current_track.pop(guild.id, None)
            return None
        if is_active(vc):
            return None

        queue = get_queue(guild.id)
        while queue:
            track = queue.pop(0)
            try:
                source = await asyncio.to_thread(YTDLSource, track["webpage_url"])
            except Exception as exc:
                log.exception("Could not start stream for %s", track["webpage_url"])
                await announce(guild, f"Failed to load {describe(track)}, skipping. ({exc})")
                continue

            def after_play(err, source=source, track=track):
                asyncio.run_coroutine_threadsafe(on_track_end(guild, track, source, err), bot.loop)

            try:
                vc.play(discord.PCMVolumeTransformer(source, volume=VOLUME), after=after_play)
            except Exception as exc:
                source.cleanup()
                log.exception("vc.play failed")
                await announce(guild, f"Playback error: {exc}")
                current_track.pop(guild.id, None)
                return None

            current_track[guild.id] = track
            return track

        current_track.pop(guild.id, None)
        return None


async def on_track_end(guild, track, source, error):
    try:
        if current_track.get(guild.id) is track:
            current_track.pop(guild.id, None)
        if error:
            log.error("Playback error on %s: %s", track["webpage_url"], error)
            await announce(guild, f"Playback error: {error}")
        elif source.error:
            log.error("No audio from %s: %s", track["webpage_url"], source.error)
            await announce(guild, f"Failed to stream {describe(track)}: {source.error}")

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        nxt = await start_next(guild)
        if nxt:
            await announce(guild, f"Now playing: {describe(nxt)}")
        elif not is_active(vc):
            await announce(guild, "Queue empty. Use `/play` or `/add` to keep the music going.")
    except Exception:
        log.exception("Error while advancing the queue")


async def ensure_voice(interaction):
    """Join (or move to) the caller's voice channel. Replies and returns None on failure."""
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        await interaction.followup.send("You need to be in a voice channel.")
        return None

    vc = interaction.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel != voice.channel:
                await vc.move_to(voice.channel)
        else:
            if vc:  # stale client left over from a dropped connection
                await vc.disconnect(force=True)
            vc = await voice.channel.connect(timeout=30, self_deaf=True)
    except Exception as exc:
        log.exception("Voice connect failed")
        await interaction.followup.send(f"Could not join **{voice.channel.name}**: {exc}")
        return None
    return vc


# ─── Lifecycle ───────────────────────────────────────────

@bot.event
async def setup_hook():
    synced = await bot.tree.sync()
    log.info("Synced %d slash commands", len(synced))


@bot.event
async def on_ready():
    log.info("BoolBot online as %s", bot.user)
    for guild in bot.guilds:
        ch = await get_bot_channel(guild)
        if ch:
            log.info("Output channel: #%s in %s", ch.name, guild.name)
        else:
            log.info("No #%s channel in %s", BOOLBOT_CHANNEL_NAME, guild.name)


@bot.tree.error
async def on_app_command_error(interaction, error):
    log.exception("Command /%s failed", getattr(interaction.command, "name", "?"), exc_info=error)
    msg = f"Something went wrong: {getattr(error, 'original', error)}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)
    except discord.HTTPException:
        pass


# ─── /play ───────────────────────────────────────────────

@bot.tree.command(name="play", description="Play a track by name/URL, or resume / start the queue")
@app_commands.describe(query="Song name or YouTube URL (leave empty to resume)")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()
    guild = interaction.guild
    vc = guild.voice_client

    # No query: resume, or start whatever is waiting in the queue
    if not query:
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("Resumed.")
            return
        if vc and vc.is_playing():
            await interaction.followup.send("Already playing.")
            return
        if not get_queue(guild.id):
            await interaction.followup.send(
                "`/play` must be followed by a query/URL, or have media which is actively queued/paused."
            )
            return
        vc = await ensure_voice(interaction)
        if not vc:
            return
        track = await start_next(guild)
        if track:
            await announce(guild, f"Now playing: {describe(track)}", interaction)
        else:
            await interaction.followup.send("Nothing in the queue could be played.")
        return

    vc = await ensure_voice(interaction)
    if not vc:
        return

    await interaction.followup.send(f"Searching: **{query}**...")

    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception as exc:
        log.warning("Search failed for %r: %s", query, exc)
        await interaction.followup.send(f"Could not find anything for: {query}")
        return

    queue = get_queue(guild.id)
    queue.append(track)
    started = await start_next(guild)

    if started:
        await announce(guild, f"Now playing: {describe(started)}", interaction)
    if track in queue:
        await announce(
            guild,
            f"Queued #{queue.index(track) + 1}: {describe(track)}",
            None if started else interaction,
        )
    elif started is not track:
        await interaction.followup.send(f"Could not play {describe(track)}.")


# ─── /add ────────────────────────────────────────────────

@bot.tree.command(name="add", description="Add a track to the queue")
@app_commands.describe(query="Song name or YouTube URL", position="Queue position (optional)")
@app_commands.guild_only()
async def add(interaction: discord.Interaction, query: str, position: int = None):
    await interaction.response.defer()
    guild = interaction.guild

    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception as exc:
        log.warning("Search failed for %r: %s", query, exc)
        await interaction.followup.send(f"Could not find anything for: {query}")
        return

    queue = get_queue(guild.id)

    if position is not None:
        idx = max(0, min(position - 1, len(queue)))
        queue.insert(idx, track)
    else:
        queue.append(track)

    # If the bot is sitting idle in voice, start right away
    started = await start_next(guild)
    if started:
        await announce(guild, f"Now playing: {describe(started)}", interaction)
        if started is not track and track in queue:
            await announce(guild, f"Queued #{queue.index(track) + 1}: {describe(track)}")
        return

    msg = f"Queued #{queue.index(track) + 1}: {describe(track)}"
    if not is_active(guild.voice_client):
        msg += "\nNothing is playing - use `/play` to start the queue."
    await announce(guild, msg, interaction)


# ─── /move ───────────────────────────────────────────────

@bot.tree.command(name="move", description="Move a track in the queue")
@app_commands.describe(
    current_position="Current position in queue",
    new_position="Desired position in queue",
)
@app_commands.guild_only()
async def move(interaction: discord.Interaction, current_position: int, new_position: int):
    await interaction.response.defer()
    queue = get_queue(interaction.guild_id)

    if not queue:
        await interaction.followup.send("Queue is empty.")
        return

    cur = current_position - 1

    if cur < 0 or cur >= len(queue):
        await interaction.followup.send(f"Invalid current position. Queue has {len(queue)} tracks.")
        return

    track = queue.pop(cur)
    new = max(0, min(new_position - 1, len(queue)))
    queue.insert(new, track)

    await announce(
        interaction.guild,
        f"Moved **{track['title']}** from #{current_position} to #{new + 1}.",
        interaction,
    )


# ─── /skip ───────────────────────────────────────────────

@bot.tree.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not is_active(vc):
        await interaction.followup.send("Nothing is playing.")
        return

    vc.stop()  # triggers after_play → on_track_end → next track
    await interaction.followup.send("Skipped.")


# ─── /pause ──────────────────────────────────────────────

@bot.tree.command(name="pause", description="Pause the current track")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not vc or not vc.is_playing():
        await interaction.followup.send("Nothing is playing.")
        return

    vc.pause()
    await interaction.followup.send("Paused. Use `/play` to resume.")


# ─── /stop ───────────────────────────────────────────────

@bot.tree.command(name="stop", description="Stop playback, clear queue, and leave voice")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    get_queue(interaction.guild_id).clear()
    current_track.pop(interaction.guild_id, None)

    if vc:
        await vc.disconnect(force=True)  # also stops playback

    await interaction.followup.send("Stopped and disconnected.")


# ─── /queue ──────────────────────────────────────────────

@bot.tree.command(name="queue", description="Show the current queue")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    await interaction.response.defer()
    queue = get_queue(interaction.guild_id)
    now = current_track.get(interaction.guild_id)

    lines = []
    if now:
        lines.append(f"**Now playing:** {now['title']} [{format_duration(now['duration'])}]")

    if queue:
        if lines:
            lines.append("")
        shown = queue[:15]  # stay under Discord's 2000-character message limit
        for i, t in enumerate(shown, 1):
            lines.append(f"**{i}.** {t['title'][:90]} [{format_duration(t['duration'])}]")
        if len(queue) > len(shown):
            lines.append(f"...and {len(queue) - len(shown)} more")
    elif not now:
        lines.append("Queue is empty.")

    await interaction.followup.send("\n".join(lines))


# ─── /nowplaying ─────────────────────────────────────────

@bot.tree.command(name="nowplaying", description="Show the currently playing track")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    await interaction.response.defer()
    now = current_track.get(interaction.guild_id)

    if not now:
        await interaction.followup.send("Nothing is playing.")
        return

    await interaction.followup.send(f"Now playing: {describe(now)}\n{now['webpage_url']}")


# ─── Entry point ─────────────────────────────────────────

def acquire_single_instance_lock():
    """Hold a localhost port so a second copy of the bot refuses to start."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        sock.close()
        return None
    return sock


def main():
    setup_logging()

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_TOKEN is not set. Copy .env.example to .env and put the bot token in it.")
        return 1
    if not FFMPEG_PATH:
        log.error("ffmpeg was not found. Install it or set FFMPEG_PATH in .env.")
        return 1

    lock = acquire_single_instance_lock()
    if lock is None:
        log.error("BoolBot is already running (port %d is in use). Not starting a second copy.", LOCK_PORT)
        return 1

    pid_file = BASE_DIR / "boolbot.pid"
    pid_file.write_text(str(os.getpid()))
    log.info("Starting BoolBot (pid %d, ffmpeg: %s)", os.getpid(), FFMPEG_PATH)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Reset it in the Developer Portal and update .env.")
        return 1
    finally:
        pid_file.unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
