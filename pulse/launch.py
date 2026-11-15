"""Start a complete PyPulse installation in a single container."""
import argparse
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    python = sys.executable
    for command in (["migrate", "--noinput"], ["bootstrap_workspace"], ["collectstatic", "--noinput"]):
        subprocess.run([python, "manage.py", *command], check=True)

    children = []
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        children.append(subprocess.Popen([python, "manage.py", "pulseworker"]))
        children.append(subprocess.Popen([python, "-m", "uvicorn", "pulse.asgi:application",
                                          "--host", args.host, "--port", str(args.port),
                                          "--workers", str(args.workers), "--no-proxy-headers"]))
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    return child.returncode or 1
            time.sleep(.25)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
