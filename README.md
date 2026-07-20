# Plexamp Rater

A Windows background utility that shows an overlay HUD when a song is ending in Plexamp, letting you rate it globally using your numeric keypad.

## Features

- **End-of-song HUD**: Automatically pops up a HUD showing the title, artist, album art, and current rating when a song has less than 20 seconds remaining.
- **Global Numpad Hotkeys**: Press `Numpad 1-5` to rate 1-5 stars, or `Numpad 0` to clear. Works globally (even inside full-screen games).
- **Auto-Lifecycle**: Detects when Plexamp starts or stops. Hooks hotkeys only while Plexamp is active and goes to sleep when it closes.
- **UAC Bypass**: Designed to run elevated at Windows logon via Task Scheduler so keys are captured globally without UAC prompts.

## Setup

1. Copy `.env.example` to `.env` and fill in your Plex URL, token, and username.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the daemon:
   ```bash
   python plexamp_rater.py
   ```
