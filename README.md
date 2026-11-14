# BoolBot

Discord voice radio bot. Joins your voice channel, plays audio through yt-dlp
and FFmpeg, and is controlled with slash commands. Runs on your own Windows PC
in the background, with no terminal window.

## Setup (once)

Double-click `setup.bat`. It installs FFmpeg, Deno and the Python packages if
they are missing, asks for the bot token (saved to `.env`, which is never
committed), and starts the bot. Python 3.10+ must already be installed.

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
