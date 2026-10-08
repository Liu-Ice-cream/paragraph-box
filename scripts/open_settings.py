"""Open the installed portable plugin's settings, or source settings otherwise."""
from pathlib import Path
import subprocess
import sys

base = Path(__file__).resolve().parents[1]
installed = base / '.runtime/FlowLauncher-2.1.4/FlowLauncher/app-2.1.4/UserData/Plugins/ParagraphSearch-0.1.0'
target = installed if (installed / 'settings_ui.py').is_file() else base
subprocess.Popen([sys.executable, str(target / 'settings_ui.py')],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
