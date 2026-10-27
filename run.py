#!/usr/bin/env python3
"""One entry point. Python 3.8+, standard library only, nothing to install.

    python run.py demo            end-to-end walkthrough on a fresh, flaky mock (starts and stops it); --live for today's date
    python run.py day             one business day: five orders across all stores, end to end, with day-end totals
    python run.py race            two tills, one last frame: holds, returns, declined and stuck payments, second by second
    python run.py bench           checkout timing per step, retries, many tills at once, Wi-Fi trouble (simulated network)
    python run.py test            unit + integration tests
    python run.py serve           till screen + API on http://127.0.0.1:8000 with its own flaky mock (--no-flake: 429/503 off)
    python run.py mock            just the mock (flaky), like starter-kit/README
    python run.py pos <args>      the CLI, same as `python -m eluno_pos <args>`
"""
import os
import subprocess
import sys
import unittest

if sys.version_info < (3, 8):
    sys.exit("Eluno POS needs Python 3.8 or newer (found %d.%d). Install a newer Python and run again." % sys.version_info[:2])

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MOCK = os.path.join(HERE, "starter-kit", "mock-apis", "mock_server.py")


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
        # Hosts like Render and Railway hand us PORT and expect us on all interfaces.
        port = int(os.environ.get("PORT") or os.environ.get("ELUNO_POS_API_PORT") or "8000")
        host = os.environ.get("ELUNO_POS_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        if os.environ.get("ELUNO_POS_BASE_URL"):  # a backend already exists (another mock, or the real systems)
            serve(settings, port, host)
        else:  # the hosted demo: own mock, 429/503 on, resettable from the screen
            serve(settings, port, host, sandbox=True, flaky="--no-flake" not in argv and os.environ.get("ELUNO_POS_MOCK_FLAKY", "1") != "0")
        return 0
    if cmd == "pos":
        from eluno_pos.cli import main as cli
        return cli(argv[1:])
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
