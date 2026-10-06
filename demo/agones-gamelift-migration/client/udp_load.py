#!/usr/bin/env python3
"""UDP load generator for the Agones simple-game-server (and, after cutover,
the GameLift fleet endpoint — the client doesn't care which backend answers).

Simulates N concurrent "players" that send periodic gameplay pings and read the
server's echo, printing how many players are getting responses. Used on camera
to show a live, continuously-connected player load before/during/after the
migration.

simple-game-server echoes any datagram as "ACK: <msg>". It treats a few control
words specially (e.g. EXIT, UNHEALTHY); this generator never sends those — only
benign "playerN-ping-M" strings — so it won't shut a server down.

Usage:
    python3 udp_load.py <host> <port> [players=10] [seconds=120]
"""
import socket
import sys
import threading
import time


def player(idx: int, host: str, port: int, stop: threading.Event, stats: dict) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    seq = 0
    while not stop.is_set():
        try:
            sock.sendto(f"player{idx}-ping-{seq}".encode(), (host, port))
            sock.recvfrom(1024)
            stats[idx] = stats.get(idx, 0) + 1
        except socket.timeout:
            pass
        except OSError:
            break
        seq += 1
        time.sleep(1.0)
    sock.close()


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: udp_load.py <host> <port> [players=10] [seconds=120]")
        return 1
    host = sys.argv[1]
    port = int(sys.argv[2])
    players = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    seconds = int(sys.argv[4]) if len(sys.argv) > 4 else 120

    stop = threading.Event()
    stats: dict = {}
    threads = [
        threading.Thread(target=player, args=(i, host, port, stop, stats), daemon=True)
        for i in range(players)
    ]
    for t in threads:
        t.start()

    print(f"{players} players connecting to {host}:{port} for {seconds}s (Ctrl-C to stop)")
    try:
        for _ in range(seconds):
            time.sleep(1)
            active = sum(1 for v in stats.values() if v)
            print(f"  active players (receiving echoes): {active}/{players}", end="\r", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        time.sleep(0.5)
    print(f"\nTotal echoes received across all players: {sum(stats.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
