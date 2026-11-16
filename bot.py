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
import urllib.request
import html
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks
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
current_source: dict[int, "YTDLSource"] = {}  # for elapsed time
panels: dict[int, discord.Message] = {}  # guild -> live now-playing message
panel_channel: dict[int, int] = {}  # guild -> channel id of the last command (fallback home)
PANEL_REFRESH = 5  # seconds
radio_off: set[int] = set()  # guilds that turned endless radio off
played_ids: dict[int, list[str]] = {}  # recent video ids per guild, so radio avoids repeats
RADIO_BATCH = 10


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
        self.frames = 0  # frames handed to Discord; drives the progress bar
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

    @property
    def elapsed(self):
        return self.frames * 0.02

    def read(self):
        if self.first:
            data, self.first = self.first, None
        else:
            data = self.ffmpeg.stdout.read(self.FRAME)
            if len(data) < self.FRAME:
                return b""
        self.frames += 1
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


async def fill_metadata(track):
    """Artist-mix songs come without a duration; look it up while the stream loads."""
    if track.get("duration"):
        return
    try:
        meta = await asyncio.to_thread(search_track, track["webpage_url"])
        track["duration"] = meta["duration"]
    except Exception:
        log.warning("Couldn't fetch metadata for %s", track["title"])


async def start_track(guild, vc, track):
    loading.add(guild.id)
    try:
        raw, _ = await asyncio.gather(
            asyncio.to_thread(YTDLSource, track["webpage_url"]), fill_metadata(track)
        )
    finally:
        loading.discard(guild.id)
    if not vc.is_connected() or vc.is_playing() or vc.is_paused():
        raw.cleanup()
        raise RuntimeError("voice state changed while loading")
    current_track[guild.id] = track
    current_source[guild.id] = raw
    source = discord.PCMVolumeTransformer(raw, volume=VOLUME)

    def after(err):
        if err:
            log.error("Playback error: %s", err)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after)
    log.info("Now playing: %s", track["title"])
    vid = video_id(track["webpage_url"])
    if vid:
        recent = played_ids.setdefault(guild.id, [])
        recent.append(vid)
        del recent[:-200]
    await set_presence(track)
    await set_vc_status(guild, f"🎵 {track['title']}")
    await post_panel(guild)


def mix_search(seed_id, exclude, limit=RADIO_BATCH):
    """YouTube's auto-generated mix for a song, minus anything played recently."""
    url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": limit + len(exclude) + 10}) as ydl:
        info = ydl.extract_info(url, download=False)
    out, seen = [], set(exclude)
    for e in info.get("entries") or []:
        if not e or not e.get("id") or e["id"] in seen or not e.get("title"):
            continue
        seen.add(e["id"])
        out.append({
            "title": e["title"],
            "webpage_url": f"https://www.youtube.com/watch?v={e['id']}",
            "duration": e.get("duration") or 0,
        })
        if len(out) >= limit:
            break
    return out


async def refill_radio(guild):
    """Queue ran dry: top it up with a mix based on the song that just finished."""
    if guild.id in radio_off:
        return False
    seed = current_track.get(guild.id)
    seed_id = video_id(seed["webpage_url"]) if seed else None
    if not seed_id:
        return False
    loading.add(guild.id)
    try:
        tracks = await asyncio.to_thread(mix_search, seed_id, played_ids.get(guild.id, []))
    except Exception:
        log.exception("Radio lookup failed")
        return False
    finally:
        loading.discard(guild.id)
    if not tracks:
        return False
    batch = next(batch_ids)
    get_queue(guild.id).extend(
        dict(t, batch=batch, artist="the radio", radio=seed["title"]) for t in tracks
    )
    log.info("Radio: queued %d songs based on %s", len(tracks), seed["title"])
    return True


async def set_vc_status(guild, text):
    """The little status line under the voice channel, visible to the whole server."""
    vc = guild.voice_client
    if not vc or not vc.channel:
        return
    try:
        await bot.http.edit_voice_channel_status(trim(text, 500) if text else None, channel_id=vc.channel.id)
    except discord.HTTPException as e:
        log.info("Couldn't set voice channel status (%s)", e.status)


async def play_next(guild):
    vc = guild.voice_client
    queue = get_queue(guild.id)
    if not vc or not vc.is_connected():
        await end_session(guild)
        return
    if vc.is_playing() or vc.is_paused() or guild.id in loading:
        return
    if not queue and not await refill_radio(guild):
        await end_session(guild, "Queue finished. Use `/play` to keep the music going.")
        return
    if vc.is_playing() or vc.is_paused() or not queue:
        return  # something else started playing while the radio was loading
    track = queue.pop(0)
    try:
        await start_track(guild, vc, track)
    except Exception:
        log.exception("Failed to start %s", track["title"])
        await announce(guild, f"Failed to load {label(track)}, skipping.")
        await play_next(guild)


async def end_session(guild, note="Nothing playing. Use `/play` to start some music."):
    if guild.id in current_track:
        await set_vc_status(guild, None)
    current_track.pop(guild.id, None)
    current_source.pop(guild.id, None)
    msg = panels.pop(guild.id, None)
    if msg:
        try:
            await msg.edit(embed=idle_embed(note), view=None)
        except discord.HTTPException:
            pass
    if not current_track:
        await set_presence(None)


# ─── live now-playing panel ─────────────────────────────

ACCENT_PLAYING = 0x5865F2
ACCENT_PAUSED = 0xFAA61A
ACCENT_IDLE = 0x2B2D31
BAR_LEN = 20


def video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def trim(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


def progress_bar(elapsed, duration):
    if not duration:
        return f"🔴 **LIVE** · `{fmt(elapsed) or '0:00'}`"
    ratio = max(0.0, min(1.0, elapsed / duration))
    pos = min(BAR_LEN - 1, int(ratio * BAR_LEN))
    bar = "━" * pos + "⬤" + "─" * (BAR_LEN - pos - 1)
    return f"`{fmt(elapsed) or '0:00'}`  {bar}  `{fmt(duration)}`"


def idle_embed(note):
    e = discord.Embed(description=note, color=ACCENT_IDLE)
    e.set_author(name="BoolBot Radio", icon_url=bot.user.display_avatar.url if bot.user else None)
    return e


def build_embed(guild):
    track = current_track.get(guild.id)
    if not track:
        return idle_embed("Nothing playing. Use `/play` to start some music.")
    vc = guild.voice_client
    paused = bool(vc and vc.is_paused())
    src = current_source.get(guild.id)
    elapsed = src.elapsed if src else 0
    queue = get_queue(guild.id)

    status = "⏸  Paused" if paused else "▶  Playing"
    where = f" in **{vc.channel.name}**" if vc and vc.channel else ""
    e = discord.Embed(
        title=trim(track["title"], 250),
        url=track["webpage_url"],
        description=f"{status}{where}\n\n{progress_bar(elapsed, track.get('duration'))}",
        color=ACCENT_PAUSED if paused else ACCENT_PLAYING,
    )
    e.set_author(name="Now Playing", icon_url=bot.user.display_avatar.url)
    vid = video_id(track["webpage_url"])
    if vid:
        e.set_thumbnail(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    if track.get("radio"):
        e.add_field(name="📻 Radio", value="Based on " + trim(track["radio"], 90), inline=True)
    elif track.get("artist"):
        e.add_field(name="🎤 Artist mix", value=trim(track["artist"].title(), 100), inline=True)
    if track.get("requester"):
        e.add_field(name="Requested by", value=trim(track["requester"], 100), inline=True)

    if queue:
        lines = []
        for i, t in enumerate(queue[:5], 1):
            d = fmt(t.get("duration"))
            lines.append(f"`{i}.` {trim(t['title'], 55)}" + (f" · `{d}`" if d else ""))
        if len(queue) > 5:
            lines.append(f"*…and {len(queue) - 5} more*")
        e.add_field(name="Up Next", value="\n".join(lines), inline=False)
    else:
        empty = ("*Nothing queued. Radio will keep it going.*" if guild.id not in radio_off
                 else "*Nothing queued. `/add` something.*")
        e.add_field(name="Up Next", value=empty, inline=False)

    known = sum(t.get("duration") or 0 for t in queue)
    footer = f"{len(queue)} in queue"
    if known:
        footer += f" · {fmt(known)}+ of music"
    footer += " · radio " + ("off" if guild.id in radio_off else "on")
    e.set_footer(text=footer + f" · live, updates every {PANEL_REFRESH}s")
    return e


class PanelView(discord.ui.View):
    """Buttons under the panel. Anyone can see them; only people in the bot's
    voice channel can use them."""

    def __init__(self, guild):
        super().__init__(timeout=None)
        vc = guild.voice_client
        paused = bool(vc and vc.is_paused())
        track = current_track.get(guild.id) or {}
        self.pause_btn.label = "Resume" if paused else "Pause"
        self.pause_btn.emoji = "▶️" if paused else "⏸️"
        self.skipartist_btn.disabled = not track.get("batch")

    async def interaction_check(self, interaction):
        vc = interaction.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message(
                "Join the bot's voice channel to use the controls.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction, button):
        msg = toggle_pause(interaction.guild)
        await interaction.response.edit_message(
            embed=build_embed(interaction.guild), view=PanelView(interaction.guild)
        )
        log.info("%s via panel: %s", interaction.user, msg)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction, button):
        await interaction.response.defer()
        do_skip(interaction.guild)

    @discord.ui.button(label="Skip artist", emoji="⏩", style=discord.ButtonStyle.secondary)
    async def skipartist_btn(self, interaction, button):
        msg = do_skipartist(interaction.guild)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction, button):
        await interaction.response.defer()
        await do_stop(interaction.guild)
        await announce_in(interaction.channel, f"⏹️ Stopped by {interaction.user.display_name}.")


def panel_home(guild):
    ch = get_bot_channel(guild)
    if ch:
        return ch
    cid = panel_channel.get(guild.id)
    return guild.get_channel(cid) if cid else None


async def post_panel(guild, channel=None):
    """(Re)post the panel at the bottom of its channel, replacing the old one."""
    old = panels.pop(guild.id, None)
    if old:
        try:
            await old.delete()
        except discord.HTTPException:
            pass
    channel = channel or panel_home(guild)
    if not channel:
        return
    try:
        panels[guild.id] = await channel.send(
            embed=build_embed(guild), view=PanelView(guild), silent=True
        )
    except discord.HTTPException:
        log.exception("Couldn't post panel in #%s", channel)


async def refresh_panel(guild):
    msg = panels.get(guild.id)
    if not msg:
        return
    try:
        await msg.edit(embed=build_embed(guild), view=PanelView(guild))
    except discord.NotFound:
        panels.pop(guild.id, None)
    except discord.HTTPException:
        pass


@tasks.loop(seconds=PANEL_REFRESH)
async def panel_loop():
    for gid in list(panels):
        guild = bot.get_guild(gid)
        if guild and current_track.get(gid):
            await refresh_panel(guild)


async def set_presence(track):
    try:
        if track:
            await bot.change_presence(activity=discord.Activity(
                type=discord.ActivityType.listening, name=trim(track["title"], 120)))
        else:
            await bot.change_presence(activity=None)
    except Exception:
        pass


async def announce_in(channel, msg):
    try:
        await channel.send(msg, silent=True)
    except discord.HTTPException:
        pass


# ─── shared actions (used by slash commands and panel buttons) ───

def toggle_pause(guild):
    vc = guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return "Resumed."
    if vc and vc.is_playing():
        vc.pause()
        return "Paused."
    return "Nothing is playing."


def do_skip(guild):
    vc = guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return "Nothing is playing."
    vc.stop()  # 'after' callback starts the next track
    return "Skipped."


def do_skipartist(guild):
    vc = guild.voice_client
    now = current_track.get(guild.id)
    if not vc or not (vc.is_playing() or vc.is_paused()) or not now:
        return "Nothing is playing."
    batch = now.get("batch")
    if not batch:
        return "This song isn't part of an artist mix. Use `/skip`."
    queue = get_queue(guild.id)
    dropped = sum(1 for t in queue if t.get("batch") == batch)
    queue[:] = [t for t in queue if t.get("batch") != batch]
    msg = f"Skipped the rest of **{now.get('artist', 'this artist')}** ({dropped + 1} songs)."
    msg += f" Up next: {label(queue[0])}" if queue else " Queue is now empty."
    vc.stop()
    return msg


async def do_stop(guild):
    queues.pop(guild.id, None)
    artist_session.pop(guild.id, None)
    vc = guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect(force=True)
    await end_session(guild, "Stopped. Use `/play` to start again.")


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
    if not panel_loop.is_running():
        panel_loop.start()
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


@bot.listen("on_interaction")
async def remember_channel(interaction):
    # The panel lives in #boolbot if it exists, else wherever commands were last used.
    if interaction.guild and interaction.type == discord.InteractionType.application_command:
        panel_channel[interaction.guild.id] = interaction.channel_id


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    if member.id == bot.user.id and before.channel and not after.channel:
        # Bot got disconnected (kicked, or channel deleted).
        queues.pop(guild.id, None)
        await end_session(guild, "Disconnected from voice. Use `/play` to start again.")
        return
    if not vc or not vc.channel or member.bot:
        return
    if before.channel == vc.channel and not [m for m in vc.channel.members if not m.bot]:
        await do_stop(guild)
        if guild.id in panels or panel_home(guild):
            await announce(guild, "👋 Everyone left the voice channel, so I did too.")


@bot.tree.error
async def on_app_command_error(interaction, error):
    log.exception("Command error", exc_info=error)
    msg = f"Something broke: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def requester(interaction):
    return interaction.user.display_name


@bot.tree.command(name="play", description="Play a song or an artist's top songs (empty = resume)")
@app_commands.describe(query="Song, artist or YouTube URL (leave empty to resume)")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not query:
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("▶️ Resumed.")
            await refresh_panel(interaction.guild)
        elif vc and vc.is_playing():
            await interaction.followup.send("Already playing.")
        else:
            await interaction.followup.send("Give me a song, an artist or a URL.")
        return
    await handle_play(interaction, query)


def enqueue(guild_id, tracks):
    """Add requested songs ahead of any auto-filled radio songs. Returns 1-based position."""
    queue = get_queue(guild_id)
    idx = next((i for i, t in enumerate(queue) if t.get("radio")), len(queue))
    queue[idx:idx] = tracks
    return idx + 1


async def handle_play(interaction, query):
    """Shared by /play and the 'Play in voice' right-click command. Interaction must be deferred."""
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
    track["requester"] = requester(interaction)

    if vc.is_playing() or vc.is_paused() or interaction.guild_id in loading:
        pos = enqueue(interaction.guild_id, [track])
        await interaction.followup.send(f"➕ Queued #{pos}: {label(track)}")
        await refresh_panel(interaction.guild)
        return

    await interaction.followup.send(f"🎵 Loading {label(track)}…")
    try:
        await start_track(interaction.guild, vc, track)
    except Exception as e:
        log.exception("Failed to stream")
        await interaction.followup.send(f"Failed to stream {label(track)}: `{e}`")


async def play_artist(interaction, vc, query, tracks):
    artist_session[interaction.guild_id] = {"query": query, "seen": {t["title"].lower() for t in tracks}}
    batch = next(batch_ids)
    tracks = [dict(t, batch=batch, artist=query, requester=requester(interaction)) for t in tracks]
    busy = vc.is_playing() or vc.is_paused() or interaction.guild_id in loading
    enqueue(interaction.guild_id, tracks)
    verb = "Queued" if busy else "Playing"
    await interaction.followup.send(
        f"🎤 {verb} the top {len(tracks)} songs for **{query.title()}**. "
        "`/skipartist` jumps past them, `/more` gets the next 10."
    )
    if busy:
        await refresh_panel(interaction.guild)
    else:
        await play_next(interaction.guild)


@bot.tree.command(name="add", description="Add a song to the queue")
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
    track["requester"] = requester(interaction)

    queue = get_queue(interaction.guild_id)
    if position is not None:
        idx = max(0, min(position - 1, len(queue)))
        queue.insert(idx, track)
    else:
        idx = enqueue(interaction.guild_id, [track]) - 1
    await interaction.followup.send(f"➕ Queued #{idx + 1}: {label(track)}")

    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and not (vc.is_playing() or vc.is_paused()) and interaction.guild_id not in loading:
        await play_next(interaction.guild)
    else:
        await refresh_panel(interaction.guild)


@bot.tree.command(name="move", description="Move a song in the queue")
@app_commands.describe(current_position="Current position in queue", new_position="Desired position")
@app_commands.guild_only()
async def move(interaction: discord.Interaction, current_position: int, new_position: int):
    queue = get_queue(interaction.guild_id)
    cur = current_position - 1
    if not 0 <= cur < len(queue):
        await interaction.response.send_message(f"Invalid position. Queue has {len(queue)} songs.", ephemeral=True)
        return
    track = queue.pop(cur)
    new = max(0, min(new_position - 1, len(queue)))
    queue.insert(new, track)
    await interaction.response.send_message(f"↕️ Moved **{track['title']}** to #{new + 1}.")
    await refresh_panel(interaction.guild)


@bot.tree.command(name="remove", description="Remove a song from the queue")
@app_commands.describe(position="Position in queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: int):
    queue = get_queue(interaction.guild_id)
    if not 1 <= position <= len(queue):
        await interaction.response.send_message(f"Invalid position. Queue has {len(queue)} songs.", ephemeral=True)
        return
    track = queue.pop(position - 1)
    await interaction.response.send_message(f"🗑️ Removed {label(track)}.")
    await refresh_panel(interaction.guild)


@bot.tree.command(name="clear", description="Clear the queue (keeps the current song playing)")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    n = len(get_queue(interaction.guild_id))
    queues.pop(interaction.guild_id, None)
    await interaction.response.send_message(f"🧹 Cleared {n} queued song(s).")
    await refresh_panel(interaction.guild)


@bot.tree.command(name="more", description="Jump to the next 10 songs of the last artist you played")
@app_commands.guild_only()
async def more(interaction: discord.Interaction):
    session = artist_session.get(interaction.guild_id)
    if not session:
        await interaction.response.send_message("No artist on the go. Use `/play <artist name>` first.", ephemeral=True)
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
        await interaction.followup.send(f"That's everything I can find for **{session['query'].title()}**.")
        return
    session["seen"].update(t["title"].lower() for t in fresh)
    batch = next(batch_ids)
    queues[interaction.guild_id] = [
        dict(t, batch=batch, artist=session["query"], requester=requester(interaction)) for t in fresh
    ]
    await interaction.followup.send(f"🎤 Next {len(fresh)} songs for **{session['query'].title()}**.")
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # 'after' callback starts the first of the new batch
    else:
        await play_next(interaction.guild)


@bot.tree.command(name="skipartist", description="Skip the rest of the current artist and move on to what's queued next")
@app_commands.guild_only()
async def skipartist(interaction: discord.Interaction):
    await interaction.response.send_message("⏩ " + do_skipartist(interaction.guild))


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    await interaction.response.send_message("⏭️ " + do_skip(interaction.guild))


@bot.tree.command(name="pause", description="Pause or resume the current song")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    msg = toggle_pause(interaction.guild)
    await interaction.response.send_message(("⏸️ " if msg == "Paused." else "▶️ ") + msg)
    await refresh_panel(interaction.guild)


@bot.tree.command(name="stop", description="Stop playback, clear the queue and leave voice")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await do_stop(interaction.guild)
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@bot.tree.command(name="queue", description="Show the full queue")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    queue = get_queue(interaction.guild_id)
    now = current_track.get(interaction.guild_id)
    e = discord.Embed(title="Queue", color=ACCENT_PLAYING)
    if now:
        e.add_field(name="Now Playing", value=label(now), inline=False)
    lines = [f"`{i}.` {label(t)}" for i, t in enumerate(queue[:25], 1)]
    if len(queue) > 25:
        lines.append(f"*…and {len(queue) - 25} more*")
    e.description = "\n".join(lines)[:4000] if lines else "*Nothing queued.*"
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="radio", description="Turn endless radio on or off (plays similar songs when the queue runs out)")
@app_commands.guild_only()
async def radio(interaction: discord.Interaction):
    gid = interaction.guild_id
    if gid in radio_off:
        radio_off.discard(gid)
        msg = "📻 Radio **on**. When the queue runs out I'll keep playing similar songs."
    else:
        radio_off.add(gid)
        queue = get_queue(gid)
        queue[:] = [t for t in queue if not t.get("radio")]
        msg = "📻 Radio **off**. Music stops when the queue runs out."
    await interaction.response.send_message(msg)
    await refresh_panel(interaction.guild)


SPOTIFY_TRACK = re.compile(r"https?://open\.spotify\.com/(?:intl-\w+/)?track/\w+")
YOUTUBE_LINK = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/)|youtu\.be/)[\w-]{11}\S*")


def spotify_to_query(url):
    """Spotify track page -> 'Artist Title' to search on YouTube (no Spotify account needed)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    page = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    desc = re.search(r'<meta property="og:description" content="([^"]*)"', page)
    if not title:
        raise LookupError("no title on Spotify page")
    artist = desc.group(1).split(" · ")[0] if desc else ""
    return html.unescape(f"{artist} {title.group(1)}".strip())


async def query_from_message(message):
    texts = [message.content] + [e.url or "" for e in message.embeds]
    blob = " ".join(texts)
    yt = YOUTUBE_LINK.search(blob)
    if yt:
        return yt.group(0)
    sp = SPOTIFY_TRACK.search(blob)
    if sp:
        return await asyncio.to_thread(spotify_to_query, sp.group(0))
    text = re.sub(r"<[^>]+>|https?://\S+", "", message.content).strip()  # drop mentions/emoji/links
    if 0 < len(text) <= 100:
        return text  # treat short plain text ("gucci mane", "poppin") as a search
    return None


@app_commands.context_menu(name="Play in voice")
@app_commands.guild_only()
async def play_in_voice(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer()
    try:
        query = await query_from_message(message)
    except Exception:
        log.exception("Couldn't read song from message")
        query = None
    if not query:
        await interaction.followup.send("Couldn't find a song link or name in that message.")
        return
    await handle_play(interaction, query)


bot.tree.add_command(play_in_voice)


@bot.tree.command(name="nowplaying", description="Bring the live Now Playing panel to this channel")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    if not current_track.get(interaction.guild_id):
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    await interaction.response.send_message("📺 Panel moved here.", ephemeral=True)
    await post_panel(interaction.guild, channel=interaction.channel)


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
