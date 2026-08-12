"""_util.py — run Playwright's sync API safely, even inside Spyder / IPython.

Spyder executes code in an IPython kernel that already owns an asyncio event loop, and
Playwright's *sync* API refuses to run inside a running loop. Running the work in a fresh
worker thread (which has no loop) sidesteps that, and also works fine from a plain
`python script.py`.
"""
import threading


def run_in_thread(fn, *args, **kwargs):
    box = {}

    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the caller's thread below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
