#!/usr/bin/env python3
"""Headless simulation v4: waypoint architecture — Jev is the strategist
(waypoint Choice every few seconds), code is the executor + seatbelt.
Mirrors the browser engine exactly."""
import json
import os
import threading
import time
import urllib.request

URL = os.environ.get("SNAKE_API_URL", "http://127.0.0.1:8915/api/decide")
N = 24
TICKS = 150
TICK_SLEEP = 0.16
STRAT_EVERY = 16          # ticks between Jev strategy calls (~2.6s)
GAMES = (1, 2, 3)
OPTIONS = ("food", "open_area", "border_loop")
DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def flood(walls, sx, sy, cap=80):
    seen = {(sx, sy)}
    q = [(sx, sy)]
    c = 0
    while q and c < cap:
        x, y = q.pop(0)
        c += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if nx < 0 or ny < 0 or nx >= N or ny >= N or (nx, ny) in seen or (nx, ny) in walls:
                continue
            seen.add((nx, ny))
            q.append((nx, ny))
    return c


def legal_moves(snake, d):
    head = snake[0]
    out = []
    for name, dd in DELTA.items():
        if dd == (-d[0], -d[1]):
            continue
        nx, ny = head[0] + dd[0], head[1] + dd[1]
        if nx < 0 or ny < 0 or nx >= N or ny >= N:
            continue
        walls = set(snake[:-1])
        if (nx, ny) in walls:
            continue
        out.append({"name": name, "nx": nx, "ny": ny, "space": flood(walls, nx, ny)})
    return out


def open_area_point(snake):
    walls = set(snake)
    head = snake[0]
    seen = {head}
    q = [(head[0], head[1], 0)]
    far = (head[0], head[1], 0)
    while q:
        x, y, dd = q.pop(0)
        if dd > far[2]:
            far = (x, y, dd)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if nx < 0 or ny < 0 or nx >= N or ny >= N or (nx, ny) in seen or (nx, ny) in walls:
                continue
            seen.add((nx, ny))
            q.append((nx, ny, dd + 1))
    return {"x": far[0], "y": far[1]}


def border_point(snake):
    head = snake[0]
    taken = set(snake)
    edge = [(x, y) for x in range(N) for y in range(N)
            if (x in (0, N - 1) or y in (0, N - 1)) and (x, y) not in taken]
    if not edge:
        return open_area_point(snake)
    x, y = min(edge, key=lambda c: abs(c[0] - head[0]) + abs(c[1] - head[1]))
    return {"x": x, "y": y}


def waypoints_for(snake, food):
    return {"food": (food[0], food[1]),
            "open_area": (open_area_point(snake)["x"], open_area_point(snake)["y"]),
            "border_loop": (border_point(snake)["x"], border_point(snake)["y"])}


class Inflight:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.pending = None
        self.pending_ms = 0

    def fire(self, body):
        req = urllib.request.Request(
            URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "jev-snake-sim/4.0"})

        def worker():
            t0 = time.time()
            res = None
            try:
                with urllib.request.urlopen(req, timeout=6) as r:
                    res = json.loads(r.read())
            except Exception:
                res = None
            ms = int((time.time() - t0) * 1000)
            with self.lock:
                self.pending, self.pending_ms, self.busy = res, ms, False

        with self.lock:
            self.busy = True
        threading.Thread(target=worker, daemon=True).start()

    def has_pending(self):
        with self.lock:
            return self.pending is not None

    def idle(self):
        with self.lock:
            return not self.busy

    def consume(self):
        with self.lock:
            res, ms = self.pending, self.pending_ms
            self.pending, self.pending_ms = None, 0
        return res, ms


def play_game(gid):
    snake = [(8, 12), (7, 12), (6, 12)]
    d = (1, 0)
    free = [(x, y) for x in range(N) for y in range(N) if (x, y) not in set(snake)]
    food = free[(gid * 37 + 11) % len(free)]
    wps = waypoints_for(snake, food)
    waypoint, wp_name = None, "—"
    score = ticks = deaths = 0
    jev = fb = 0
    lat = []
    inf = Inflight()

    def request_strategy():
        body = {
            "legal": list(OPTIONS),
            "state": {
                "head": {"x": snake[0][0], "y": snake[0][1]},
                "food": {"x": food[0], "y": food[1]},
                "snake_length": len(snake), "tick": ticks, "grid": f"{N}x{N}",
                "waypoints": wps,
            },
            "instruction": "",
        }
        inf.fire(body)

    while ticks < TICKS:
        # consume a landed Jev decision
        if inf.has_pending():
            res, ms = inf.consume()
            if res and res.get("source") == "jev" and res.get("action") in OPTIONS:
                waypoint = wps[res["action"]]
                wp_name = res["action"]
                jev += 1
                lat.append(ms)
            elif res is not None:
                fb += 1

        # strategy cadence: re-plan every STRAT_EVERY ticks, or when idle target
        if waypoint is None and inf.idle():
            request_strategy()
        elif ticks % STRAT_EVERY == 0 and inf.idle():
            wps = waypoints_for(snake, food)
            request_strategy()

        # steer: greedy toward waypoint + seatbelt
        legal = legal_moves(snake, d)
        if not legal:
            deaths += 1
            break
        head = snake[0]
        tgt = waypoint if waypoint else food
        dead = set()
        for m in legal:
            nx, ny = head[0] + DELTA[m["name"]][0], head[1] + DELTA[m["name"]][1]
            fake = list(snake)
            fake.insert(0, (nx, ny))
            if (nx, ny) != (food[0], food[1]):
                fake.pop()
            # only veto genuinely trapped pockets; walls are allowed (food can be there)
            if flood(set(fake[:-1]), nx, ny) < 8:
                dead.add(m["name"])
        pool = [m for m in legal if m["name"] not in dead] or legal
        best = None
        bd = 10 ** 9
        for m in pool:
            dist = abs(tgt[0] - m["nx"]) + abs(tgt[1] - m["ny"])
            if dist < bd or (dist == bd and best and m["space"] > best["space"]):
                best, bd = m, dist
        d = DELTA[best["name"]]

        # move
        nh = (head[0] + d[0], head[1] + d[1])
        if nh[0] < 0 or nh[1] < 0 or nh[0] >= N or nh[1] >= N:
            deaths += 1
            break
        eat = nh == food
        body = set(snake) if eat else set(snake[:-1])
        if nh in body:
            deaths += 1
            break
        snake.insert(0, nh)
        if eat:
            score += 10
            if wp_name == "food":
                waypoint = None
            free = [(x, y) for x in range(N) for y in range(N) if (x, y) not in set(snake)]
            food = free[(ticks * 13 + 5) % len(free)] if free else (0, 0)
        else:
            snake.pop()
        ticks += 1
        time.sleep(TICK_SLEEP)

    avg = sum(lat) // len(lat) if lat else 0
    print(f"game {gid}: ticks={ticks}/{TICKS} score={score} len={len(snake)} deaths={deaths} "
          f"jev={jev} fallback={fb} avg_jev_ms={avg}")


for g in GAMES:
    play_game(g)
