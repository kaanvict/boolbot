"""BoolBot - Discord radio bot. Joins your voice channel and plays YouTube audio via yt-dlp.

Run hidden (no console) with BoolBot.vbs; logs go to boolbot.log.
"""
import asyncio
import itertools
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import re
import urllib.parse
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
BOOLBOT_CHANNEL_NAME = "boolbot"
VOLUME = 0.5

# ─── logging (works under pythonw, where there is no console) ───
LOG_FILE = HERE / "boolbot.log"
TOOLS_LOG = HERE / "tools.log"  # yt-dlp/ffmpeg stderr
handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stderr is not None:
    handlers.append(logging.StreamHandler(sys.stderr))
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
    handlers=handlers,
)
log = logging.getLogger("boolbot")

# ─── external tools ───
FFMPEG = shutil.which("ffmpeg") or (
    r"C:\Users\Kaan\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)
# Use python.exe (not pythonw.exe) to run yt-dlp so its stdout pipe works.
PYTHON = sys.executable
if PYTHON.lower().endswith("pythonw.exe"):
    PYTHON = PYTHON[:-len("pythonw.exe")] + "python.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

queues: dict[int, list[dict]] = {}
current_track: dict[int, dict] = {}
loading: set[int] = set()  # guilds currently fetching a stream
artist_session: dict[int, dict] = {}  # guild -> {query, seen titles} for /more
batch_ids = itertools.count(1)  # tags each artist batch so /skipartist can drop it


def get_queue(guild_id):
    return queues.setdefault(guild_id, [])


def get_bot_channel(guild):
    return discord.utils.get(guild.text_channels, name=BOOLBOT_CHANNEL_NAME)


def search_track(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise LookupError("no results")
            info = entries[0]
        return {
            "title": info.get("title", "Unknown"),
            "webpage_url": info.get("webpage_url") or query,
            "duration": info.get("duration") or 0,
        }


ARTIST_TRACKS = 10
STOPWORDS = {"the", "a", "an", "of", "and", "feat", "ft", "x", "with", "by", "official", "music", "video", "audio"}


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def music_search(query, limit=ARTIST_TRACKS + 5):
    """Top songs from YouTube Music's song search, in its (popularity) order."""
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(query) + "#songs"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": limit}) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks, seen = [], set()
    for e in info.get("entries") or []:
        if not e or not e.get("id") or not e.get("title"):
            continue
        key = e["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        tracks.append({
            "title": e["title"],
            "webpage_url": f"https://www.youtube.com/watch?v={e['id']}",
            "duration": e.get("duration") or 0,
        })
    return tracks


def artist_tracks(query):
    """If the query looks like just an artist name, return their top songs, else None.

    Heuristic: for a specific-song query ("gucci mane i get the bag") the top song's
    title shares words with the query; for an artist-only query ("gucci mane") it doesn't.
    """
    if re.match(r"https?://", query.strip()):
        return None
    q = words(query)
    if not q:
        return None
    try:
        songs = music_search(query)
    except Exception:
        log.exception("YouTube Music search failed: %s", query)
        return None
    if len(songs) < 3 or q & words(songs[0]["title"]):
        return None
    return songs[:ARTIST_TRACKS]


def fmt(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def label(track):
    d = fmt(track["duration"])
    return f"**{track['title']}**" + (f" [{d}]" if d else "")


class YTDLSource(discord.AudioSource):
    """yt-dlp | ffmpeg -> 48kHz s16le PCM for Discord. No console windows.

    YouTube intermittently answers 403 on the first request, so we read the first
    frame up front and retry with a fresh yt-dlp run if no audio arrives.
    Blocking: construct it in a worker thread.
    """

    FRAME = 3840  # 20ms of 48kHz 16-bit stereo
    ATTEMPTS = 4

    def __init__(self, url):
        self.errlog = open(TOOLS_LOG, "ab")
        self.ytdlp = self.ffmpeg = None
        for attempt in range(1, self.ATTEMPTS + 1):
            self._spawn(url)
            first = self.ffmpeg.stdout.read(self.FRAME)
            if len(first) == self.FRAME:
                self.first = first
                return
            log.warning("No audio from %s (attempt %d/%d), retrying", url, attempt, self.ATTEMPTS)
            self._kill()
            time.sleep(1.5 * attempt)
        self.errlog.close()
        raise RuntimeError("YouTube refused the stream several times")

    def _spawn(self, url):
        self.ytdlp = subprocess.Popen(
            [PYTHON, "-m", "yt_dlp", "-f", "bestaudio/best", "--no-playlist",
             "--quiet", "--no-warnings", "--retries", "10", "--fragment-retries", "10",
             "-o", "-", url],
            stdout=subprocess.PIPE, stderr=self.errlog, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        self.ffmpeg = subprocess.Popen(
            [FFMPEG, "-loglevel", "error", "-i", "pipe:0",
             "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
            stdin=self.ytdlp.stdout, stdout=subprocess.PIPE, stderr=self.errlog,
            creationflags=NO_WINDOW,
        )
        self.ytdlp.stdout.close()

    def read(self):
        if self.first:
            data, self.first = self.first, None
            return data
        data = self.ffmpeg.stdout.read(self.FRAME)
        if len(data) < self.FRAME:
            return b""
        return data

    def is_opus(self):
        return False

    def _kill(self):
        for p in (self.ffmpeg, self.ytdlp):
            try:
                p.kill()
            except Exception:
                pass

    def cleanup(self):
        self._kill()
        try:
            self.errlog.close()
        except Exception:
            pass


async def announce(guild, msg, skip_channel_id=None):
    ch = get_bot_channel(guild)
    if ch and ch.id != skip_channel_id:
        try:
            await ch.send(msg, silent=True)
        except discord.HTTPException:
            pass


async def start_track(guild, vc, track):
    loading.add(guild.id)
    try:
        raw = await asyncio.to_thread(YTDLSource, track["webpage_url"])
    finally:
        loading.discard(guild.id)
    if not vc.is_connected() or vc.is_playing() or vc.is_paused():
        raw.cleanup()
        raise RuntimeError("voice state changed while loading")
    current_track[guild.id] = track
    source = discord.PCMVolumeTransformer(raw, volume=VOLUME)

    def after(err):
        if err:
            log.error("Playback error: %s", err)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after)
    log.info("Now playing: %s", track["title"])


async def play_next(guild):
    vc = guild.voice_client
    queue = get_queue(guild.id)
    if not vc or not vc.is_connected():
        current_track.pop(guild.id, None)
        return
    if vc.is_playing() or vc.is_paused() or guild.id in loading:
        return
    if not queue:
        current_track.pop(guild.id, None)
        await announce(guild, "Queue empty. Use `/play` or `/add` to keep the music going.")
        return
    track = queue.pop(0)
    try:
        await start_track(guild, vc, track)
    except Exception:
        log.exception("Failed to start %s", track["title"])
        await announce(guild, f"Failed to load {label(track)}, skipping.")
        await play_next(guild)
        return
    await announce(guild, f"Now playing: {label(track)}")


async def ensure_voice(interaction):
    user_voice = getattr(interaction.user, "voice", None)
    if not user_voice or not user_voice.channel:
        await interaction.followup.send("You need to be in a voice channel.")
        return None
    target = user_voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel != target:
                await vc.move_to(target)
        else:
            if vc:  # stale, half-dead client
                await vc.disconnect(force=True)
            vc = await target.connect(timeout=20, self_deaf=True)
    except Exception as e:
        log.exception("Voice connect failed")
        await interaction.followup.send(f"Couldn't join voice: `{e}`")
        return None
    return vc


@bot.event
async def on_ready():
    log.info("BoolBot online as %s in %d guild(s)", bot.user, len(bot.guilds))
    if getattr(bot, "_synced", False):
        return
    # Register commands per-guild (instant update in clients) and clear global
    # copies, so Discord never shows stale "command is outdated" definitions.
    try:
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        for guild in bot.guilds:
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d slash commands to %s", len(synced), guild.name)
        bot._synced = True
    except Exception:
        log.exception("Failed to sync commands")


@bot.event
async def on_guild_join(guild):
    source = next((g for g in bot.guilds if g.id != guild.id and bot.tree.get_commands(guild=g)), None)
    if source:
        for cmd in bot.tree.get_commands(guild=source):
            bot.tree.add_command(cmd, guild=guild, override=True)
        await bot.tree.sync(guild=guild)


@bot.event
async def on_voice_state_update(member, before, after):
    # Leave when everyone else has left the channel.
    vc = member.guild.voice_client
    if not vc or not vc.channel or member.id == bot.user.id:
        return
    if before.channel == vc.channel and not [m for m in vc.channel.members if not m.bot]:
        queues.pop(member.guild.id, None)
        current_track.pop(member.guild.id, None)
        await vc.disconnect()
        await announce(member.guild, "Everyone left, so I did too.")


@bot.tree.error
async def on_app_command_error(interaction, error):
    log.exception("Command error", exc_info=error)
    msg = f"Something broke: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)
    except discord.HTTPException:
        pass


@bot.tree.command(name="play", description="Play a track by name/URL, or resume if paused")
@app_commands.describe(query="Song name or YouTube URL (leave empty to resume)")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not query:
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("Resumed.")
        elif vc and vc.is_playing():
            await interaction.followup.send("Already playing.")
        else:
            await interaction.followup.send("Give me a song name or URL.")
        return

    vc = await ensure_voice(interaction)
    if not vc:
        return

    tracks = await asyncio.to_thread(artist_tracks, query)
    if tracks:
        await play_artist(interaction, vc, query, tracks)
        return

    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception:
        log.exception("Search failed: %s", query)
        await interaction.followup.send(f"Couldn't find anything for: {query}")
        return

    if vc.is_playing() or vc.is_paused() or interaction.guild_id in loading:
        queue = get_queue(interaction.guild_id)
        queue.append(track)
        msg = f"Queued #{len(queue)}: {label(track)}"
    else:
        try:
            await start_track(interaction.guild, vc, track)
        except Exception as e:
            log.exception("Failed to stream")
            await interaction.followup.send(f"Failed to stream {label(track)}: `{e}`")
            return
        msg = f"Now playing: {label(track)}"

    await interaction.followup.send(msg)
    await announce(interaction.guild, msg, skip_channel_id=interaction.channel_id)


async def play_artist(interaction, vc, query, tracks):
    artist_session[interaction.guild_id] = {"query": query, "seen": {t["title"].lower() for t in tracks}}
    batch = next(batch_ids)
    tracks = [dict(t, batch=batch, artist=query) for t in tracks]
    queue = get_queue(interaction.guild_id)
    listing = "\n".join(f"{i}. {t['title']}" for i, t in enumerate(tracks, 1))
    busy = vc.is_playing() or vc.is_paused() or interaction.guild_id in loading
    queue.extend(tracks)
    if busy:
        msg = f"Queued top {len(tracks)} songs for **{query}**:\n{listing}"
    else:
        msg = f"Playing top {len(tracks)} songs for **{query}**:\n{listing}"
    await interaction.followup.send(msg[:2000])
    if not busy:
        await play_next(interaction.guild)
        return
    await announce(interaction.guild, msg[:2000], skip_channel_id=interaction.channel_id)


@bot.tree.command(name="add", description="Add a track to the queue")
@app_commands.describe(query="Song name or YouTube URL", position="Queue position (optional)")
@app_commands.guild_only()
async def add(interaction: discord.Interaction, query: str, position: int = None):
    await interaction.response.defer()
    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception:
        log.exception("Search failed: %s", query)
        await interaction.followup.send(f"Couldn't find anything for: {query}")
        return

    queue = get_queue(interaction.guild_id)
    if position is not None:
        idx = max(0, min(position - 1, len(queue)))
        queue.insert(idx, track)
    else:
        idx = len(queue)
        queue.append(track)
    msg = f"Queued #{idx + 1}: {label(track)}"
    await interaction.followup.send(msg)
    await announce(interaction.guild, msg, skip_channel_id=interaction.channel_id)

    # If connected but idle, start playing right away.
    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and not (vc.is_playing() or vc.is_paused()) and interaction.guild_id not in loading:
        await play_next(interaction.guild)


@bot.tree.command(name="move", description="Move a track in the queue")
@app_commands.describe(current_position="Current position in queue", new_position="Desired position")
@app_commands.guild_only()
async def move(interaction: discord.Interaction, current_position: int, new_position: int):
    queue = get_queue(interaction.guild_id)
    if not queue:
        await interaction.response.send_message("Queue is empty.")
        return
    cur = current_position - 1
    if not 0 <= cur < len(queue):
        await interaction.response.send_message(f"Invalid position. Queue has {len(queue)} tracks.")
        return
    track = queue.pop(cur)
    new = max(0, min(new_position - 1, len(queue)))
    queue.insert(new, track)
    await interaction.response.send_message(
        f"Moved **{track['title']}** from #{current_position} to #{new + 1}."
    )


@bot.tree.command(name="remove", description="Remove a track from the queue")
@app_commands.describe(position="Position in queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: int):
    queue = get_queue(interaction.guild_id)
    if not 1 <= position <= len(queue):
        await interaction.response.send_message(f"Invalid position. Queue has {len(queue)} tracks.")
        return
    track = queue.pop(position - 1)
    await interaction.response.send_message(f"Removed {label(track)}.")


@bot.tree.command(name="clear", description="Clear the queue (keeps the current song playing)")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    n = len(get_queue(interaction.guild_id))
    queues.pop(interaction.guild_id, None)
    await interaction.response.send_message(f"Cleared {n} queued track(s).")


@bot.tree.command(name="more", description="Jump to the next 10 songs of the last artist you played")
@app_commands.guild_only()
async def more(interaction: discord.Interaction):
    session = artist_session.get(interaction.guild_id)
    if not session:
        await interaction.response.send_message("No artist on the go. Use `/play <artist name>` first.")
        return
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return
    try:
        songs = await asyncio.to_thread(music_search, session["query"], len(session["seen"]) + 30)
    except Exception:
        log.exception("More search failed")
        await interaction.followup.send("Couldn't fetch more songs right now.")
        return
    fresh = [t for t in songs if t["title"].lower() not in session["seen"]][:ARTIST_TRACKS]
    if not fresh:
        await interaction.followup.send(f"That's everything I can find for **{session['query']}**.")
        return
    session["seen"].update(t["title"].lower() for t in fresh)
    batch = next(batch_ids)
    queues[interaction.guild_id] = [dict(t, batch=batch, artist=session["query"]) for t in fresh]
    listing = "\n".join(f"{i}. {t['title']}" for i, t in enumerate(fresh, 1))
    await interaction.followup.send(f"Next {len(fresh)} for **{session['query']}**:\n{listing}"[:2000])
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # 'after' callback starts the first of the new batch
    else:
        await play_next(interaction.guild)


@bot.tree.command(name="skipartist", description="Skip the rest of the current artist and move on to what's queued next")
@app_commands.guild_only()
async def skipartist(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    now = current_track.get(interaction.guild_id)
    if not vc or not (vc.is_playing() or vc.is_paused()) or not now:
        await interaction.response.send_message("Nothing is playing.")
        return
    batch = now.get("batch")
    if not batch:
        await interaction.response.send_message("This song isn't part of an artist batch. Use `/skip`.")
        return
    queue = get_queue(interaction.guild_id)
    dropped = sum(1 for t in queue if t.get("batch") == batch)
    queue[:] = [t for t in queue if t.get("batch") != batch]
    msg = f"Skipped the rest of **{now.get('artist', 'this artist')}** ({dropped + 1} songs)."
    msg += f" Up next: {label(queue[0])}" if queue else " Queue is now empty."
    vc.stop()  # 'after' callback starts the next track
    await interaction.response.send_message(msg)


@bot.tree.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("Nothing is playing.")
        return
    vc.stop()  # 'after' callback starts the next track
    await interaction.response.send_message("Skipped.")


@bot.tree.command(name="pause", description="Pause the current track")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.")
        return
    vc.pause()
    await interaction.response.send_message("Paused. `/play` to resume.")


@bot.tree.command(name="stop", description="Stop playback, clear queue, and leave voice")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    queues.pop(interaction.guild_id, None)
    current_track.pop(interaction.guild_id, None)
    artist_session.pop(interaction.guild_id, None)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect(force=True)
    await interaction.response.send_message("Stopped and disconnected.")


@bot.tree.command(name="queue", description="Show the current queue")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    queue = get_queue(interaction.guild_id)
    now = current_track.get(interaction.guild_id)
    lines = []
    if now:
        lines.append(f"**Now playing:** {label(now)}")
    for i, t in enumerate(queue[:20], 1):
        lines.append(f"**{i}.** {label(t)}")
    if len(queue) > 20:
        lines.append(f"...and {len(queue) - 20} more")
    if not lines:
        lines.append("Queue is empty.")
    await interaction.response.send_message("\n".join(lines)[:2000])


@bot.tree.command(name="nowplaying", description="Show the currently playing track")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    now = current_track.get(interaction.guild_id)
    if not now:
        await interaction.response.send_message("Nothing is playing.")
        return
    await interaction.response.send_message(f"Now playing: {label(now)}\n{now['webpage_url']}")


def single_instance_lock():
    """Prevent two copies running at once (e.g. double-clicking the launcher)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47613))
    except OSError:
        log.error("BoolBot is already running. Exiting.")
        sys.exit(0)
    return s


if __name__ == "__main__":
    if not TOKEN:
        log.error("DISCORD_TOKEN missing. Put it in %s as DISCORD_TOKEN=...", HERE / ".env")
        sys.exit(1)
    _lock = single_instance_lock()
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Reset it in the Developer Portal and update .env.")
        sys.exit(1)
    except Exception:
        log.exception("BoolBot crashed")
        raise
