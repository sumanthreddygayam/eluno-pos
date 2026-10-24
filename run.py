#!/usr/bin/env python3
"""One entry point. Python 3.8+, standard library only, nothing to install.

    python run.py demo            end-to-end walkthrough on a fresh, flaky mock (starts and stops it); --live for today's date
    python run.py day             one business day: five orders across all stores, end to end, with day-end totals
    python run.py race            two tills, one last frame: holds, returns, declined and stuck payments, second by second
    python run.py bench           checkout timing per step, retries, many tills at once, Wi-Fi trouble (simulated network)
    python run.py test            unit + integration tests
    python run.py serve           mock on :8787 (if not already up) + POS HTTP API on :8000
    python run.py mock            just the mock (flaky), like starter-kit/README
    python run.py pos <args>      the CLI, same as `python -m eluno_pos <args>`
"""
import os
import subprocess
import sys
import time
import unittest
import urllib.request

if sys.version_info < (3, 8):
    sys.exit("Eluno POS needs Python 3.8 or newer (found %d.%d). Install a newer Python and run again." % sys.version_info[:2])

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MOCK = os.path.join(HERE, "starter-kit", "mock-apis", "mock_server.py")


def _mock_up(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/health", timeout=1).read()
        return True
    except OSError:
        return False


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "demo":
        from eluno_pos.demo import run
        return run(argv[1:])
    if cmd == "day":
        from eluno_pos.day import run as run_day
        return run_day(argv[1:])
    if cmd == "race":
        from eluno_pos.race import run as run_race
        return run_race(argv[1:])
    if cmd == "bench":
        from eluno_pos.bench import run as run_bench
        return run_bench(argv[1:])
    if cmd == "test":
        suite = unittest.defaultTestLoader.discover(os.path.join(HERE, "tests"), top_level_dir=HERE)
        result = unittest.TextTestRunner(verbosity=1 if "-q" in argv else 2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if cmd == "mock":
        return subprocess.call([sys.executable, MOCK] + argv[1:])
    if cmd == "serve":
        from eluno_pos.config import Settings
        from eluno_pos.api import serve
        settings = Settings.from_env()
        child = None
        if not _mock_up(settings.base_url):
            port = settings.base_url.rsplit(":", 1)[-1]
            child = subprocess.Popen([sys.executable, MOCK, "--port", port])
            for _ in range(50):
                if _mock_up(settings.base_url):
                    break
                time.sleep(0.1)
        try:
            serve(settings, int(os.environ.get("ELUNO_POS_API_PORT", "8000")))
        finally:
            if child:
                child.terminate()
        return 0
    if cmd == "pos":
        from eluno_pos.cli import main as cli
        return cli(argv[1:])
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
