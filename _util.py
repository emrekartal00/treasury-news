"""_util.py - run Playwright's sync API safely, even inside Spyder / IPython on Windows.

Two problems this solves:
  1. Spyder's IPython console already runs an asyncio loop, and Playwright's SYNC API
     refuses to run inside a running loop. Running the work in a fresh worker thread
     (which has no loop) sidesteps that.
  2. On Windows, Spyder forces the SelectorEventLoop policy (Tornado needs it), but that
     loop CANNOT spawn subprocesses -> Playwright raises NotImplementedError when it
     launches the browser. Playwright needs the ProactorEventLoop. We switch the policy to
     Proactor for the duration of the run, then restore it so Spyder's console keeps working.
"""
import asyncio
import sys
import threading

_IS_WIN = sys.platform.startswith("win")


def run_in_thread(fn, *args, **kwargs):
    box = {}

    prev_policy = None
    if _IS_WIN:
        prev_policy = asyncio.get_event_loop_policy()
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the caller's thread below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()

    if _IS_WIN and prev_policy is not None:
        try:
            asyncio.set_event_loop_policy(prev_policy)
        except Exception:
            pass

    if "error" in box:
        raise box["error"]
    return box.get("value")
