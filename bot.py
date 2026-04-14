import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
import subprocess
import shlex

TOKEN = "MTQ4NDQyOTM1ODI0MzI1MDIyNg.G13VUL.ZzFZTISfYQM8qgaqqzA-QptzzbbORNyBBpS3wM"
GUILD_ID = 1481526858783916064
BOOLBOT_CHANNEL_NAME = "boolbot"

FFMPEG_PATH = r"C:\Users\Kaan\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild state
queues = {}  # guild_id -> list of track dicts
current_track = {}  # guild_id -> track dict
bot_channel = {}  # guild_id -> text channel


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


async def get_bot_channel(guild):
    if guild.id in bot_channel:
        return bot_channel[guild.id]
    for ch in guild.text_channels:
        if ch.name == BOOLBOT_CHANNEL_NAME:
            bot_channel[guild.id] = ch
            return ch
    return None


def search_track(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {
            "title": info.get("title", "Unknown"),
            "webpage_url": info.get("webpage_url", query),
            "duration": info.get("duration", 0),
        }


class YTDLSource(discord.AudioSource):
    """Audio source that pipes yt-dlp output through FFmpeg into Discord."""

    def __init__(self, webpage_url):
        self.process = subprocess.Popen(
            [
                "yt-dlp",
                "-f", "bestaudio/best",
                "--no-playlist",
                "-o", "-",
                "--quiet",
                webpage_url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.ffmpeg = subprocess.Popen(
            [
                FFMPEG_PATH,
                "-i", "pipe:0",
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "-loglevel", "warning",
                "pipe:1",
            ],
            stdin=self.process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.process.stdout.close()  # allow yt-dlp to receive SIGPIPE

    def read(self):
        data = self.ffmpeg.stdout.read(3840)  # 20ms of 48kHz 16-bit stereo
        if len(data) != 3840:
            return b""
        return data

    def is_opus(self):
        return False

    def cleanup(self):
        try:
            self.ffmpeg.kill()
        except Exception:
            pass
        try:
            self.process.kill()
        except Exception:
            pass


def format_duration(seconds):
    if not seconds:
        return "??:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def play_next(guild, error=None):
    ch = await get_bot_channel(guild)
    if error and ch:
        await ch.send(f"Playback error: {error}")

    queue = get_queue(guild.id)
    vc = guild.voice_client

    if not vc or not vc.is_connected():
        current_track.pop(guild.id, None)
        return

    if not queue:
        current_track.pop(guild.id, None)
        if ch:
            await ch.send("Queue empty. Use `/play` or `/add` to keep the music going.")
        return

    track = queue.pop(0)
    current_track[guild.id] = track

    try:
        source = await asyncio.to_thread(YTDLSource, track["webpage_url"])
    except Exception:
        if ch:
            await ch.send(f"Failed to load **{track['title']}**, skipping.")
        await play_next(guild)
        return

    def after_play(err):
        asyncio.run_coroutine_threadsafe(play_next(guild, err), bot.loop)

    vc.play(discord.PCMVolumeTransformer(source, volume=0.5), after=after_play)
    if ch:
        await ch.send(f"Now playing: **{track['title']}** [{format_duration(track['duration'])}]")


async def ensure_voice(interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("You need to be in a voice channel.")
        return None

    voice_channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    else:
        vc = await voice_channel.connect()

    return vc


@bot.event
async def on_ready():
    print(f"BoolBot online as {bot.user}")
    for guild in bot.guilds:
        ch = await get_bot_channel(guild)
        if ch:
            print(f"  Output channel: #{ch.name} in {guild.name}")
    try:
        synced = await bot.tree.sync()
        print(f"  Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"  Failed to sync commands: {e}")


# ─── /play ───────────────────────────────────────────────

@bot.tree.command(name="play", description="Play a track by name/URL, or resume if paused")
@app_commands.describe(query="Song name or YouTube URL (leave empty to resume)")
async def play(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()

    vc = interaction.guild.voice_client

    # No query: try to resume
    if not query:
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("Resumed.")
            return
        elif vc and vc.is_playing():
            await interaction.followup.send("Already playing.")
            return
        else:
            await interaction.followup.send(
                "`/play` must be followed by a query/URL, or have media which is actively queued/paused."
            )
            return

    vc = await ensure_voice(interaction)
    if not vc:
        return

    await interaction.followup.send(f"Searching: **{query}**...")

    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception:
        await interaction.followup.send(f"Could not find anything for: {query}")
        return

    queue = get_queue(interaction.guild_id)

    # If something is already playing, add to queue
    if vc.is_playing() or vc.is_paused():
        queue.append(track)
        pos = len(queue)
        ch = await get_bot_channel(interaction.guild)
        if ch and ch.id != interaction.channel_id:
            await ch.send(f"Queued #{pos}: **{track['title']}** [{format_duration(track['duration'])}]")
        await interaction.followup.send(
            f"Queued #{pos}: **{track['title']}** [{format_duration(track['duration'])}]"
        )
        return

    # Nothing playing: play immediately
    current_track[interaction.guild_id] = track

    try:
        source = await asyncio.to_thread(YTDLSource, track["webpage_url"])
    except Exception:
        await interaction.followup.send(f"Failed to stream: {track['title']}")
        return

    def after_play(err):
        asyncio.run_coroutine_threadsafe(play_next(interaction.guild, err), bot.loop)

    vc.play(discord.PCMVolumeTransformer(source, volume=0.5), after=after_play)
    ch = await get_bot_channel(interaction.guild)
    msg = f"Now playing: **{track['title']}** [{format_duration(track['duration'])}]"
    if ch and ch.id != interaction.channel_id:
        await ch.send(msg)
    await interaction.followup.send(msg)


# ─── /add ────────────────────────────────────────────────

@bot.tree.command(name="add", description="Add a track to the queue")
@app_commands.describe(query="Song name or YouTube URL", position="Queue position (optional)")
async def add(interaction: discord.Interaction, query: str, position: int = None):
    await interaction.response.defer()

    try:
        track = await asyncio.to_thread(search_track, query)
    except Exception:
        await interaction.followup.send(f"Could not find anything for: {query}")
        return

    queue = get_queue(interaction.guild_id)

    if position is not None:
        idx = max(0, min(position - 1, len(queue)))
        queue.insert(idx, track)
        pos_label = idx + 1
    else:
        queue.append(track)
        pos_label = len(queue)

    ch = await get_bot_channel(interaction.guild)
    msg = f"Queued #{pos_label}: **{track['title']}** [{format_duration(track['duration'])}]"
    if ch and ch.id != interaction.channel_id:
        await ch.send(msg)
    await interaction.followup.send(msg)


# ─── /move ───────────────────────────────────────────────

@bot.tree.command(name="move", description="Move a track in the queue")
@app_commands.describe(
    current_position="Current position in queue",
    new_position="Desired position in queue",
)
async def move(interaction: discord.Interaction, current_position: int, new_position: int):
    await interaction.response.defer()
    queue = get_queue(interaction.guild_id)

    if not queue:
        await interaction.followup.send("Queue is empty.")
        return

    cur = current_position - 1
    new = new_position - 1

    if cur < 0 or cur >= len(queue):
        await interaction.followup.send(f"Invalid current position. Queue has {len(queue)} tracks.")
        return

    track = queue.pop(cur)
    new = max(0, min(new, len(queue)))
    queue.insert(new, track)

    msg = f"Moved **{track['title']}** from #{current_position} to #{new + 1}."
    ch = await get_bot_channel(interaction.guild)
    if ch and ch.id != interaction.channel_id:
        await ch.send(msg)
    await interaction.followup.send(msg)


# ─── /skip ───────────────────────────────────────────────

@bot.tree.command(name="skip", description="Skip the current track")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not vc or (not vc.is_playing() and not vc.is_paused()):
        await interaction.followup.send("Nothing is playing.")
        return

    vc.stop()  # triggers after_play → play_next
    await interaction.followup.send("Skipped.")


# ─── /pause ──────────────────────────────────────────────

@bot.tree.command(name="pause", description="Pause the current track")
async def pause(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    if not vc or not vc.is_playing():
        await interaction.followup.send("Nothing is playing.")
        return

    vc.pause()
    await interaction.followup.send("Paused.")


# ─── /stop ───────────────────────────────────────────────

@bot.tree.command(name="stop", description="Stop playback, clear queue, and leave voice")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = interaction.guild.voice_client

    queues.pop(interaction.guild_id, None)
    current_track.pop(interaction.guild_id, None)

    if vc:
        vc.stop()
        await vc.disconnect()

    await interaction.followup.send("Stopped and disconnected.")


# ─── /queue ──────────────────────────────────────────────

@bot.tree.command(name="queue", description="Show the current queue")
async def show_queue(interaction: discord.Interaction):
    await interaction.response.defer()
    queue = get_queue(interaction.guild_id)
    now = current_track.get(interaction.guild_id)

    lines = []
    if now:
        lines.append(f"**Now playing:** {now['title']} [{format_duration(now['duration'])}]")

    if queue:
        lines.append("")
        for i, t in enumerate(queue, 1):
            lines.append(f"**{i}.** {t['title']} [{format_duration(t['duration'])}]")
    elif not now:
        lines.append("Queue is empty.")

    await interaction.followup.send("\n".join(lines))


# ─── /nowplaying ─────────────────────────────────────────

@bot.tree.command(name="nowplaying", description="Show the currently playing track")
async def nowplaying(interaction: discord.Interaction):
    await interaction.response.defer()
    now = current_track.get(interaction.guild_id)

    if not now:
        await interaction.followup.send("Nothing is playing.")
        return

    await interaction.followup.send(
        f"Now playing: **{now['title']}** [{format_duration(now['duration'])}]\n{now['webpage_url']}"
    )


bot.run(TOKEN)
