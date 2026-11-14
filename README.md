# BoolBot

Discord voice radio bot. Joins your voice channel, plays audio through yt-dlp
and FFmpeg, and is controlled with slash commands. Runs on your own Windows PC
in the background, with no terminal window.

## Setup (once)

1. Install Python 3.10+, [FFmpeg](https://ffmpeg.org/) (`winget install Gyan.FFmpeg`)
   and a JavaScript runtime for yt-dlp (`winget install DenoLand.Deno`, or Node.js).
2. In this folder:

   ```bat
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   copy .env.example .env
   ```

3. Put the bot token in `.env` (`DISCORD_TOKEN=...`). Never commit `.env`.

## Running

- `start.bat` starts the bot hidden (via `pythonw.exe`). No window stays open.
- `stop.bat` stops it.
- Output and errors go to `boolbot.log`.
- To start with Windows: press Win+R, run `shell:startup`, and put a shortcut
  to `start.bat` in that folder.
- To debug in a console instead: `.venv\Scripts\python bot.py`.

A second copy refuses to start while one is already running.

## Commands

| Command | Action |
| --- | --- |
| `/play <query or URL>` | Join your voice channel and play, or queue if something is playing |
| `/play` | Resume if paused, or start the queue |
| `/add <query> [position]` | Add to the queue |
| `/move <from> <to>` | Reorder the queue |
| `/skip` | Skip the current track |
| `/pause` | Pause |
| `/stop` | Stop, clear the queue and leave voice |
| `/queue` | Show the queue |
| `/nowplaying` | Show the current track |

Status messages are also posted to a text channel named `#boolbot` if one exists.

## Updating

YouTube changes often. If tracks stop loading:

```bat
.venv\Scripts\pip install -U "yt-dlp[default]"
```
