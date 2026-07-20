import os
import sys
import time
import logging
import ctypes
import threading
import queue
from pathlib import Path
from io import BytesIO
import requests
import tkinter as tk
from dotenv import load_dotenv
from plexapi.server import PlexServer
import keyboard
from PIL import Image, ImageTk

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "plexamp_rater.log", encoding="utf-8")
    ]
)

# Load environment variables
env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent / ".env.example"
load_dotenv(dotenv_path=env_path)

PLEX_URL = os.getenv("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PLEX_USER = os.getenv("PLEX_USER", None)
PLAYER_NAME = os.getenv("PLAYER_NAME", "Plexamp")
REMINDER_SECONDS = int(os.getenv("REMINDER_SECONDS", "20"))
ONLY_UNRATED = os.getenv("ONLY_UNRATED", "True").lower() in ("true", "1", "yes")

# Hotkeys
HOTKEYS = {
    1: os.getenv("HOTKEY_1", "numpad 1"),
    2: os.getenv("HOTKEY_2", "numpad 2"),
    3: os.getenv("HOTKEY_3", "numpad 3"),
    4: os.getenv("HOTKEY_4", "numpad 4"),
    5: os.getenv("HOTKEY_5", "numpad 5"),
    0: os.getenv("HOTKEY_CLEAR", "numpad 0"),
}

NUMPAD_MAP = {
    "numpad 1": 79, "numpad 2": 80, "numpad 3": 81,
    "numpad 4": 75, "numpad 5": 76, "numpad 0": 82,
    "numpad 7": 71, "numpad 9": 73  # 7 = History Back, 9 = History Forward
}

# Win32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020

ui_queue = queue.Queue()

# Thread synchronization and state variables
history_lock = threading.Lock()
recent_tracks = []   # List of dicts: [{"id", "title", "artist", "stars", "album_art"}]
history_index = -1   # -1 means currently playing, 0 means most recent history, 1 older...

current_track_id = None
prompted_tracks = set()

root = None
hud_window = None
hide_timer_id = None

HUD_WIDTH = 460
HUD_HEIGHT = 120


def set_clickthrough(hwnd):
    """Add WS_EX_TRANSPARENT so clicks pass through."""
    try:
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)
    except Exception as e:
        logging.error(f"Failed to set clickthrough: {e}")


def fetch_album_art(plex, item):
    """Fetches the album art from the Plex server. Runs in the background thread."""
    if not plex or not item or not getattr(item, "thumb", None):
        return None
    try:
        thumb_url = plex.url(item.thumb)
        response = requests.get(thumb_url, headers={"X-Plex-Token": PLEX_TOKEN}, timeout=3)
        if response.status_code == 200:
            return Image.open(BytesIO(response.content))
    except Exception as e:
        logging.error(f"Error fetching album art: {e}")
    return None


def create_hud(title, artist, stars_str, instruction, border_color, img_obj=None, is_history=False, history_pos=None):
    """
    Create a brand-new Toplevel HUD window with the correct text/image baked in.
    """
    global root

    win = tk.Toplevel(root)
    win.title("Plexamp Rater HUD")
    win.overrideredirect(True)
    win.attributes("-topmost", True)

    screen_width = win.winfo_screenwidth()
    x = (screen_width - HUD_WIDTH) // 2
    y = 50
    win.geometry(f"{HUD_WIDTH}x{HUD_HEIGHT}+{x}+{y}")
    win.configure(bg="#000000")

    # Border frame
    border_frame = tk.Frame(win, bg=border_color, bd=2)
    border_frame.pack(fill="both", expand=True)

    # Inner panel
    inner = tk.Frame(border_frame, bg="#0f0f12")
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    # Status bar (Color changes when viewing history vs live track)
    bar = tk.Frame(inner, bg="#1a1a22")
    bar.pack(fill="x", side="top")
    
    if is_history:
        status_text = f"📜 RATER HISTORY (-{history_pos})"
        status_color = "#ffb000"  # Amber for history
    else:
        status_text = "🎵 PLEXAMP SONG RATER"
        status_color = "#00d2ff"  # Cyan for live
        
    tk.Label(bar, text=status_text,
             font=("Segoe UI", 8, "bold"), fg=status_color, bg="#1a1a22"
             ).pack(side="left", padx=10, pady=2)

    # Content area (horizontal split)
    content_area = tk.Frame(inner, bg="#0f0f12")
    content_area.pack(fill="both", expand=True, padx=10, pady=5)

    # Album Art Panel on the left (90x90)
    try:
        if img_obj:
            img_resized = img_obj.resize((90, 90), Image.Resampling.LANCZOS)
        else:
            # Fallback: a nice dark cassette/vinyl gray square
            img_resized = Image.new("RGB", (90, 90), "#1e1e24")
        
        photo_img = ImageTk.PhotoImage(img_resized)
        win.photo_img = photo_img  # Reference to prevent garbage collection
        
        art_label = tk.Label(content_area, image=photo_img, bg="#0f0f12", bd=0)
        art_label.pack(side="left", padx=(0, 10))
    except Exception as img_err:
        logging.error(f"Error building album art widget: {img_err}")

    # Text details on the right
    text_frame = tk.Frame(content_area, bg="#0f0f12")
    text_frame.pack(side="left", fill="both", expand=True)

    # Title
    tk.Label(text_frame, text=title,
             font=("Segoe UI", 11, "bold"), fg="#ffffff", bg="#0f0f12", anchor="w"
             ).pack(fill="x", pady=(2, 1))

    # Artist
    tk.Label(text_frame, text=artist,
             font=("Segoe UI", 9), fg="#a0a0b0", bg="#0f0f12", anchor="w"
             ).pack(fill="x", pady=(0, 2))

    # Bottom Row details
    bottom_row = tk.Frame(text_frame, bg="#0f0f12")
    bottom_row.pack(fill="x", side="bottom", pady=(0, 2))

    # Stars
    tk.Label(bottom_row, text=stars_str,
             font=("Segoe UI", 11), fg="#ffb000", bg="#0f0f12", anchor="w"
             ).pack(side="left")

    # Instruction
    tk.Label(bottom_row, text=instruction,
             font=("Segoe UI", 8, "italic"), fg="#707080", bg="#0f0f12"
             ).pack(side="right")

    # Force full render pass so all widget pixels are in the backing store
    win.update()

    # Apply click-through AFTER rendering
    set_clickthrough(int(win.wm_frame(), 16) if win.wm_frame() != "" else win.winfo_id())

    return win


def show_hud(title, artist, current_stars, highlight_rated=False, auto_hide_ms=None, album_art=None, is_history=False, history_pos=None):
    """
    Show HUD by destroying any existing Toplevel and creating a fresh one with album art and history mode support.
    """
    global hud_window, hide_timer_id

    if not root:
        return

    logging.info(
        f"show_hud: '{title}' by {artist} (Stars: {current_stars}, Hist: {is_history}, Art: {album_art is not None})"
    )

    # Cancel pending hide
    if hide_timer_id is not None:
        root.after_cancel(hide_timer_id)
        hide_timer_id = None

    # Destroy the old HUD window entirely
    if hud_window is not None:
        try:
            hud_window.destroy()
        except Exception:
            pass
        hud_window = None

    # Build stars string
    stars_str = ""
    for i in range(1, 6):
        stars_str += "★" if i <= current_stars else "☆"

    # Style
    if highlight_rated:
        border_color = "#00ff66"
        instruction = "Rating Updated Successfully!"
    else:
        if is_history:
            border_color = "#ffb000"  # Amber border in history mode
            instruction = f"Press Numpad 1-5 to rate history (-{history_pos})"
        else:
            border_color = "#00d2ff"  # Cyan border in normal mode
            instruction = "Press Numpad 1-5 to rate"

    # Create a brand new window with the correct content
    hud_window = create_hud(title, artist, stars_str, instruction, border_color, album_art, is_history, history_pos)

    # Set initial alpha
    hud_window.attributes("-alpha", 0.92)

    # Schedule auto-hide
    if highlight_rated:
        hide_timer_id = root.after(2500, hide_hud)
    elif auto_hide_ms:
        hide_timer_id = root.after(auto_hide_ms, hide_hud)


def hide_hud():
    """Destroy the HUD window to hide it."""
    global hud_window, hide_timer_id
    hide_timer_id = None
    if hud_window is not None:
        try:
            hud_window.destroy()
        except Exception:
            pass
        hud_window = None


def process_ui_queue():
    """Main-thread queue processor."""
    try:
        while True:
            msg = ui_queue.get_nowait()
            if msg.get("action") == "show":
                try:
                    show_hud(
                        msg["title"], msg["artist"], msg["stars"],
                        msg.get("highlight", False), msg.get("duration"),
                        msg.get("album_art", None),
                        msg.get("is_history", False),
                        msg.get("history_pos", None)
                    )
                except Exception as e:
                    logging.error(f"Error in show_hud: {e}", exc_info=True)
            ui_queue.task_done()
    except queue.Empty:
        pass
    except Exception as e:
        logging.error(f"Error in process_ui_queue: {e}", exc_info=True)

    if root:
        try:
            root.after(50, process_ui_queue)
        except Exception:
            pass


def get_plex_connection():
    if not PLEX_TOKEN:
        logging.error("PLEX_TOKEN is missing!")
        sys.exit(1)
    try:
        logging.info(f"Connecting to Plex at {PLEX_URL}...")
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        logging.info("Connected to Plex!")
        return plex
    except Exception as e:
        logging.error(f"Plex connection failed: {e}")
        return None


def find_active_plexamp_session(plex):
    try:
        for session in plex.sessions():
            for player in session.players:
                is_plexamp = (
                    player.product.lower() == "plexamp"
                    or player.title.lower() == PLAYER_NAME.lower()
                )
                user_ok = True
                if PLEX_USER:
                    user_ok = any(u.lower() == PLEX_USER.lower() for u in session.usernames)
                if is_plexamp and user_ok:
                    return session, player
    except Exception as e:
        logging.debug(f"Session check error: {e}")
    return None, None


def navigate_history(direction):
    """
    direction: 1 for older (back/numpad 7), -1 for newer (forward/numpad 9)
    """
    global history_index, recent_tracks, current_track_id, ui_queue

    with history_lock:
        if not recent_tracks and history_index == -1:
            logging.info("History is empty.")
            return

        if direction == 1:  # Go older
            if history_index == -1:
                history_index = 0
            else:
                history_index = min(len(recent_tracks) - 1, history_index + 1)
        elif direction == -1:  # Go newer
            if history_index == -1:
                return
            elif history_index == 0:
                history_index = -1
            else:
                history_index -= 1

        # Trigger HUD view
        if history_index == -1:
            # Reverted back to the currently playing song
            if current_track_id:
                try:
                    plex = get_plex_connection()
                    track = plex.fetchItem(current_track_id)
                    title = track.title
                    artist = track.originalTitle or track.grandparentTitle or "Unknown Artist"
                    rating = getattr(track, "userRating", 0) or 0
                    stars = rating / 2.0
                    album_art = fetch_album_art(plex, track)
                    ui_queue.put({
                        "action": "show",
                        "title": title,
                        "artist": artist,
                        "stars": stars,
                        "album_art": album_art,
                        "duration": 5000,
                        "is_history": False
                    })
                except Exception as e:
                    logging.error(f"Error restoring current track display: {e}")
        else:
            # Display history track
            track_info = recent_tracks[history_index]
            ui_queue.put({
                "action": "show",
                "title": track_info["title"],
                "artist": track_info["artist"],
                "stars": track_info["stars"],
                "album_art": track_info["album_art"],
                "duration": 5000,
                "is_history": True,
                "history_pos": history_index + 1
            })


def rate_song(stars):
    global current_track_id, history_index, recent_tracks

    target_id = None
    is_history_rating = False
    hist_idx = -1

    with history_lock:
        if history_index == -1:
            target_id = current_track_id
        else:
            if history_index < len(recent_tracks):
                track_info = recent_tracks[history_index]
                target_id = track_info["id"]
                is_history_rating = True
                hist_idx = history_index

    if not target_id:
        logging.info("No active or historical track to rate.")
        return

    try:
        plex = get_plex_connection()
        if not plex:
            return

        track = plex.fetchItem(target_id)
        if not track:
            return

        rating_value = float(stars * 2.0) if stars > 0 else None
        track.rate(rating_value)

        rating_str = f"{stars} stars" if stars > 0 else "Cleared rating"
        logging.info(f"Rated '{track.title}' (ID: {target_id}): {rating_str}")

        # Update cache in recent history
        if is_history_rating:
            with history_lock:
                if hist_idx < len(recent_tracks):
                    recent_tracks[hist_idx]["stars"] = stars

        # Fetch album art
        album_art = fetch_album_art(plex, track)

        title = track.title
        artist = track.originalTitle or track.grandparentTitle or "Unknown Artist"
        ui_queue.put({
            "action": "show",
            "title": title,
            "artist": artist,
            "stars": stars,
            "highlight": True,
            "album_art": album_art,
            "is_history": is_history_rating,
            "history_pos": hist_idx + 1 if is_history_rating else None
        })

    except Exception as e:
        logging.error(f"Failed to rate song: {e}")


def setup_hotkeys():
    logging.info("Setting up global hotkeys...")
    # 1. Setup rating keys
    for stars, key in HOTKEYS.items():
        if not key:
            continue
        key_lower = key.lower().strip()
        if key_lower in NUMPAD_MAP:
            sc = NUMPAD_MAP[key_lower]
            try:
                keyboard.hook_key(sc, lambda e, s=stars: rate_song(s) if (e.event_type == "down" and getattr(e, "is_keypad", False)) else None)
                logging.info(f"Hooked scan code {sc} ({key}) -> {stars} stars (keypad-only)")
            except Exception as e:
                logging.error(f"Hook failed for {sc}: {e}")
        else:
            try:
                keyboard.add_hotkey(key, lambda s=stars: rate_song(s), suppress=False)
                logging.info(f"Hotkey '{key}' -> {stars} stars")
            except Exception as e:
                logging.error(f"Hotkey failed for '{key}': {e}")

    # 2. Setup history navigation keys
    try:
        sc_back = NUMPAD_MAP["numpad 7"]
        keyboard.hook_key(sc_back, lambda e: navigate_history(1) if (e.event_type == "down" and getattr(e, "is_keypad", False)) else None)
        logging.info("Hooked scan code 71 (numpad 7) -> History Back")
    except Exception as e:
        logging.error(f"Hook failed for numpad 7: {e}")

    try:
        sc_fwd = NUMPAD_MAP["numpad 9"]
        keyboard.hook_key(sc_fwd, lambda e: navigate_history(-1) if (e.event_type == "down" and getattr(e, "is_keypad", False)) else None)
        logging.info("Hooked scan code 73 (numpad 9) -> History Forward")
    except Exception as e:
        logging.error(f"Hook failed for numpad 9: {e}")


def is_plexamp_running():
    """Checks if Plexamp.exe is currently running on Windows."""
    import subprocess
    try:
        output = subprocess.check_output(
            'tasklist /NH /FI "IMAGENAME eq Plexamp.exe"',
            shell=True,
            creationflags=0x08000000  # CREATE_NO_WINDOW to prevent flash
        ).decode('utf-8', errors='ignore')
        return "Plexamp.exe" in output
    except Exception:
        return False


def monitor_loop():
    global current_track_id, prompted_tracks, history_index, recent_tracks
    logging.info("Watching player state...")
    errors = 0
    booted = False
    plex = None
    hotkeys_active = False

    while True:
        try:
            # 1. Check if Plexamp is running
            if not is_plexamp_running():
                if hotkeys_active:
                    logging.info("Plexamp closed. Unhooking hotkeys and cleaning up...")
                    try:
                        keyboard.unhook_all()
                    except Exception:
                        pass
                    hotkeys_active = False
                    booted = False
                    current_track_id = None
                    plex = None
                    with history_lock:
                        recent_tracks.clear()
                        history_index = -1
                
                time.sleep(5)
                continue

            # 2. Plexamp is running. Ensure we are connected and hotkeys are active.
            if not hotkeys_active:
                logging.info("Plexamp detected running! Connecting and setting up hotkeys...")
                plex = get_plex_connection()
                if plex:
                    setup_hotkeys()
                    hotkeys_active = True
                else:
                    time.sleep(5)
                    continue

            # 3. Poll player session
            session, player = find_active_plexamp_session(plex)
            errors = 0

            if session:
                tid = session.ratingKey
                duration = session.duration
                offset = session.viewOffset

                # Check if the active track changed to save the previous track to history
                if current_track_id and current_track_id != tid:
                    old_id = current_track_id
                    with history_lock:
                        history_index = -1  # Reset navigation to live song

                    # Save finished track to history asynchronously
                    def save_to_history(track_id):
                        try:
                            t_plex = get_plex_connection()
                            if t_plex:
                                old_track = t_plex.fetchItem(track_id)
                                old_title = old_track.title
                                old_artist = old_track.originalTitle or old_track.grandparentTitle or "Unknown Artist"
                                old_rating = getattr(old_track, "userRating", 0) or 0
                                old_stars = old_rating / 2.0
                                old_art = fetch_album_art(t_plex, old_track)

                                with history_lock:
                                    # Avoid adding duplicate entries for consecutive events
                                    if not recent_tracks or recent_tracks[0]["id"] != track_id:
                                        recent_tracks.insert(0, {
                                            "id": track_id,
                                            "title": old_title,
                                            "artist": old_artist,
                                            "stars": old_stars,
                                            "album_art": old_art
                                        })
                                        if len(recent_tracks) > 10:
                                            recent_tracks.pop()
                                        logging.info(f"Added to history: '{old_title}' (ID: {track_id})")
                        except Exception as hist_err:
                            logging.error(f"Error adding track {track_id} to history: {hist_err}")

                    threading.Thread(target=save_to_history, args=(old_id,), daemon=True).start()

                current_track_id = tid

                if duration and offset:
                    remaining = (duration - offset) / 1000.0

                    if not booted:
                        logging.info(f"Boot: '{session.title}' by {session.grandparentTitle}")
                        try:
                            track = plex.fetchItem(tid)
                            rating = getattr(track, "userRating", 0) or 0
                        except Exception as track_err:
                            logging.error(f"Error fetching track rating on boot: {track_err}")
                            rating = 0
                        stars = rating / 2.0
                        
                        album_art = fetch_album_art(plex, session)
                        ui_queue.put({
                            "action": "show", 
                            "title": session.title, 
                            "artist": getattr(session, "originalTitle", None) or getattr(session, "grandparentTitle", "Unknown Artist"), 
                            "stars": stars, 
                            "duration": 5000,
                            "album_art": album_art
                        })
                        booted = True

                    if tid not in prompted_tracks:
                        if len(prompted_tracks) > 50:
                            prompted_tracks.clear()
                        if 1 < remaining <= REMINDER_SECONDS:
                            try:
                                track = plex.fetchItem(tid)
                                rating = getattr(track, "userRating", 0) or 0
                            except Exception as track_err:
                                logging.error(f"Error fetching track rating for prompt: {track_err}")
                                rating = 0
                            stars = rating / 2.0
                            
                            is_rated = stars > 0
                            if not ONLY_UNRATED or not is_rated:
                                logging.info(f"OSD: '{session.title}' by {getattr(session, 'originalTitle', None) or getattr(session, 'grandparentTitle', 'Unknown Artist')} ({remaining:.1f}s left, stars={stars})")
                                album_art = fetch_album_art(plex, session)
                                ui_queue.put({
                                    "action": "show", 
                                    "title": session.title, 
                                    "artist": getattr(session, "originalTitle", None) or getattr(session, "grandparentTitle", "Unknown Artist"), 
                                    "stars": stars,
                                    "album_art": album_art
                                })
                                prompted_tracks.add(tid)
            else:
                current_track_id = None

            time.sleep(1.5)
        except Exception as e:
            errors += 1
            logging.error(f"Monitor error: {e}")
            if errors > 5:
                plex = None
                hotkeys_active = False
                try:
                    keyboard.unhook_all()
                except Exception:
                    pass
            time.sleep(3)


def main():
    global root

    # Hidden root
    root = tk.Tk()
    root.withdraw()

    # Start queue processor
    root.after(50, process_ui_queue)

    # Background thread
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

    root.mainloop()


if __name__ == "__main__":
    main()
