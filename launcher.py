"""Start the loopback server in the background and open the user's dashboard."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"


def status():
    try:
        with urllib.request.urlopen(URL + "/api/state", timeout=2) as response:
            data = json.load(response)
        if data.get("app") != "xmeta-local-pricing":
            raise RuntimeError("Port 8765 belongs to another service.")
        return data
    except urllib.error.URLError:
        return None


def main():
    current = status()
    if "--stop" in sys.argv:
        if current:
            request = urllib.request.Request(URL + "/api/shutdown", data=b"{}", headers={"Content-Type": "application/json", "Origin": URL, "X-CSRF-Token": current["csrf"]}, method="POST")
            with urllib.request.urlopen(request, timeout=10):
                pass
        print("Xmeta helper stopped.")
        return
    if not current:
        (ROOT / ".local").mkdir(exist_ok=True)
        with (ROOT / ".local" / "server.log").open("ab") as log:
            subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=str(ROOT), stdout=log, stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        for _ in range(30):
            time.sleep(.3)
            if status():
                break
        else:
            raise RuntimeError("Cannot start local server. See .local/server.log.")
    webbrowser.open(URL)
    print("Xmeta helper: " + URL)


if __name__ == "__main__":
    main()
