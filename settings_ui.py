"""Open the local visual settings page; Flow launches this as a separate process."""

from pathlib import Path
import sys

from paragraph_search.settings_server import start_ui


if __name__ == "__main__":
    try:
        start_ui(Path(__file__).resolve().parent, open_browser="--no-browser" not in sys.argv)
    except Exception as exc:
        from main import write_log
        write_log(exc)
