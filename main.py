import os
import logging
import json
import time
import threading
from websocket import create_connection, WebSocketConnectionClosedException
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException
from functools import partial, wraps

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("CLIENT_ID and CLIENT_SECRET must be set as environment variables.")
print(f"Using CLIENT_ID: {CLIENT_ID}")

REDIRECT_URI = 'https://example.org'
WS_URL = "ws://host.docker.internal:6672/ws"
# WS_URL = "ws://localhost:6672/ws"
SCOPE = "user-read-playback-state,user-modify-playback-state"
WS_UPDATE = "spotify_update"


def handle_spotify_exception(msg=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except SpotifyException as e:
                error_msg = f"{msg}: {e}" or f"SpotifyException: {e}"
                logger.error(error_msg)
        return wrapper
    return decorator


class SpotifyClient:
    def __init__(self, client_id, client_secret, redirect_uri, scope):
        self.auth_manager = SpotifyOAuth(client_id, client_secret, redirect_uri, scope=scope)
        self.lock = threading.Lock()
        self.sp = None
        self._initialize_from_cache()

    def _initialize_from_cache(self):
        """Check if a valid token exists in the cache and initialize the Spotify client."""
        try:
            token_info = self.auth_manager.get_cached_token()
            if token_info and not self.auth_manager.is_token_expired(token_info):
                self.sp = spotipy.Spotify(auth=token_info['access_token'])
                logger.info("Initialized Spotify client from cached token.")
            else:
                logger.info("No valid token in cache. Authentication required.")
        except Exception as e:
            logger.error(f"Error initializing from cache: {e}")

    @handle_spotify_exception("Error authenticating Spotify")
    def authenticate(self, callback_url):
        """Authenticate using the Spotify OAuth callback URL."""
        code = self.auth_manager.parse_response_code(callback_url)
        if code:
            token_info = self.auth_manager.get_access_token(code)
            with self.lock:
                self.sp = spotipy.Spotify(auth=token_info['access_token'])
            logger.info("Spotify authentication successful.")
            return True
        logger.warning("Spotify authentication failed.")  # NOTE: We might never reach this line

    def is_authenticated(self):
        """Check if the Spotify client is authenticated."""
        return self.sp is not None

    @handle_spotify_exception("Error refreshing Spotify token")
    def refresh_token(self):
        """Refresh the Spotify access token if expired."""
        token_info = self.auth_manager.cache_handler.get_cached_token()
        if self.auth_manager.is_token_expired(token_info):
            token_info = self.auth_manager.refresh_access_token(token_info['refresh_token'])
            with self.lock:
                self.sp = spotipy.Spotify(auth=token_info['access_token'])
            logger.info("Spotify token refreshed successfully.")

    @handle_spotify_exception("Error retrieving current playback data")
    def get_current_playback_data(self):
        """Retrieve current playback details."""
        with self.lock:
            playback = self.sp.current_playback()
            if playback and playback.get('is_playing'):
                track = playback['item']
                return {
                    "song_name": track['name'],
                    "artists": ", ".join(artist['name'] for artist in track['artists']),
                    "cover_image": track['album']['images'][0]['url'],
                    "track_length": track['duration_ms'],
                    "track_progress": playback['progress_ms']
                }
            logger.info("No track is currently playing.")
            return None

    @property
    def ctrl_map(self):  # TODO: Name could be improved
        """A dictionary of playback control functions."""
        return {
            "play": partial(self.sp.start_playback),
            "pause": partial(self.sp.pause_playback),
            "next": partial(self.sp.next_track),
            "previous": partial(self.sp.previous_track),
            "mute": partial(self.sp.volume, 0),
            "default": lambda: logger.warning("Unknown control command.")
        }

    @handle_spotify_exception("Error executing Spotify command")
    def control(self, command):
        """Execute playback control commands."""
        if not self.sp:
            logger.error("Spotify client is not authenticated. Cannot execute control commands.")
            return
        with self.lock:
            self.ctrl_map.get(command, self.ctrl_map["default"])()
            logger.info(f"Spotify command executed: {command}")

    @handle_spotify_exception("Error fetching Spotify devices")
    def get_active_device(self):
        """Retrieve the currently active Spotify device."""
        devices = self.sp.devices()
        active_device = next((device for device in devices['devices'] if device['is_active']), None)
        if not active_device:
            logger.warning("No active Spotify device found.")
            return None
        return active_device['id']

    @handle_spotify_exception("Error setting Spotify volume")
    def set_volume(self, volume):
        """Set the volume of the active Spotify device."""
        if not volume:
            logger.warning("Volume change was requested, but no volume provided.")
            return
        device_id = self.get_active_device()
        if not device_id:
            logger.warning("Cannot set volume without an active device.")
            return
        self.sp.volume(volume, device_id=device_id)
        logger.info(f"Volume set to {volume}%")


class WebSocketClient:
    """Handles WebSocket communication."""
    def __init__(self, url):
        self.url = url
        self.ws = None

    def connect(self):
        """Establish a WebSocket connection."""
        while True:
            try:
                self.ws = create_connection(self.url)
                logger.info(f"Connected to WebSocket server at {self.url}")
                return
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}. Retrying in 5 seconds...")
                time.sleep(5)

    def send(self, message):
        """Send a message through the WebSocket."""
        try:
            self.ws.send(json.dumps(message))
            logger.info(f"Sent message: {message}")
        except WebSocketConnectionClosedException as e:
            logger.error(f"WebSocket connection closed: {e}")
            self.connect()
        except Exception as e:
            logger.error(f"Error sending WebSocket message: {e}")

    def receive(self):
        """Receive a message from the WebSocket."""
        try:
            result = self.ws.recv()
            return json.loads(result)
        except WebSocketConnectionClosedException as e:
            logger.error(f"WebSocket connection closed: {e}")
            self.connect()
        except Exception as e:
            logger.error(f"Error receiving WebSocket message: {e}")
            return None


class SpotifyWebSocketHandler:
    def __init__(self, spotify_client, ws_client):
        self.authenticated = False
        self.spotify_client = spotify_client
        self.ws_client = ws_client
        self._unauthenticated_logged = False
        self.update_thread = None

    @property
    def msg_type_map(self):  # TODO: Name is not very descriptive
        """Returns a dictionary of message type handlers."""
        return {
            "spotify_auth_url": self.send_auth_url,
            "spotify_auth": self.authenticate_spotify,
            "spotify_play": partial(self.spotify_client.control, "play"),
            "spotify_pause": partial(self.spotify_client.control, "pause"),
            "spotify_next": partial(self.spotify_client.control, "next"),
            "spotify_previous": partial(self.spotify_client.control, "previous"),
            "spotify_mute": partial(self.spotify_client.control, "mute"),
            "spotify_request_update": self.send_current_data,
            "spotify_volume": lambda data: self.spotify_client.set_volume(data.get("volume")),
            "spotify_unmute": lambda: self.spotify_client.set_volume(50),  # TODO: arbitrary value?
        }

    def send_current_data_on_msg(self, msg_type):  # TODO: Name could be improved
        """Not all messages require us to send the current data, this fn will check if the message requires it"""
        # NOTE: It might be better to list messages that don't require sending the current data
        msg_types = ["spotify_play",
                     "spotify_pause",
                     "spotify_next",
                     "spotify_previous",
                     "spotify_mute",
                     "spotify_request_update"]
        if msg_type in msg_types:
            self.send_current_data()

    def handle_message(self, message):
        """Process a WebSocket message."""
        msg_type = message.get("type")
        data = message.get("data", {})

        if not self.spotify_client.is_authenticated():
            if not self._unauthenticated_logged:
                logger.warning("Spotify client is not authenticated. Ignoring message.")
                self._unauthenticated_logged = True
                self.ws_client.send({"type": "error", "message": "Spotify is not authenticated."})
            return

        self._unauthenticated_logged = False

        # Check if the message type is known before calling the handler
        if not msg_type in self.msg_type_map:
            logger.warning(f"Unknown message type: {message.get('type')}")
            return

        self.msg_type_map.get(msg_type)(data)
        self.send_current_data_on_msg(msg_type)

    def send_auth_url(self):
        """Send the Spotify authentication URL."""
        auth_url = self.spotify_client.auth_manager.get_authorize_url()
        print(auth_url)
        self.ws_client.send({"type": "spotify_auth_url_response", "data": {"auth_url": auth_url}})

    def authenticate_spotify(self, callback_url):
        """Authenticate Spotify using the callback URL."""
        self.authenticated = self.spotify_client.authenticate(callback_url)
        if self.authenticated:
            self.start_update_thread()

    def send_current_data(self):
        """Send current playback data to the WebSocket."""
        data = self.spotify_client.get_current_playback_data()
        self.ws_client.send({"type": WS_UPDATE, "data": data})

    def start_update_thread(self):
        """Start a thread to send periodic updates."""
        if self.update_thread is None or not self.update_thread.is_alive():
            self.update_thread = threading.Thread(target=self.update_loop, daemon=True)
            self.update_thread.start()

    def update_loop(self):
        """Periodically refresh Spotify tokens and send playback updates."""
        while True:
            try:
                self.spotify_client.refresh_token()
                self.send_current_data()
                time.sleep(10)
            except Exception as e:
                logger.error(f"Error in update loop: {e}")


def main():
    spotify_client = SpotifyClient(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPE)
    ws_client = WebSocketClient(WS_URL)

    ws_client.connect()
    handler = SpotifyWebSocketHandler(spotify_client, ws_client)

    # Log the authentication status
    logger.info("Waiting for Spotify authentication...")

    while True:
        message = ws_client.receive()
        if message:
            handler.handle_message(message)


if __name__ == "__main__":
    main()
