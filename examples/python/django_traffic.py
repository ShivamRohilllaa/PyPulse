"""Send three rounds of requests to the local Django demo."""

from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def hit(path, expected_status):
    started = perf_counter()
    try:
        with urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
            response.read()
            status = response.status
    except HTTPError as error:
        with error:
            status = error.code
    except URLError as error:
        raise SystemExit(f"Cannot reach the local Django demo: {error.reason}") from error
    if status != expected_status:
        raise SystemExit(f"GET {path} returned {status}, expected {expected_status}")
    print(f"GET {path:<12} {status}  {(perf_counter() - started) * 1000:.0f} ms", flush=True)


if __name__ == "__main__":
    for round_index in range(3):
        if round_index:
            sleep(12)
        for order_id in range(1, 8):
            hit(f"/orders/{round_index * 7 + order_id}", 200)
        hit("/demo/slow", 200)
        hit("/demo/slow", 200)
        hit("/demo/error", 500)
