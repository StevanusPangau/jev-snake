#!/usr/bin/env python3
"""jev-snake — Jev (TypeSafe System One) plays Snake.

The client (browser canvas) owns the game loop, geometry, and legal-move
safeguards. This server acts as the decision bridge: it serializes the board
state, prompts TypeSafe Jev via the Choice API (~100ms), records token usage
persistently in SQLite, and falls back to a deterministic safe action if Jev
is unreachable.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from anyio import to_thread

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import db

# ---- Load .env if present (fallback for local non-systemd execution) ----
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(ENV_FILE):
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except Exception:
        pass

# ---- Configuration ----
API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
REQUEST_TIMEOUT_S = float(os.environ.get("JEV_TIMEOUT_S", "1.5"))

# Official Jev pricing: $42 / 1B input tokens = $0.042 / 1M input tokens. Output tokens free.
PRICE_PER_MTOK_INPUT = 0.042
PRICE_PER_MTOK_OUTPUT = 0.0

# Rate limiting: 120 requests per 30-second window per IP
BUCKET_CAP = 120
BUCKET_WINDOW_S = 30.0
_buckets: dict[str, deque[float]] = {}
_bucket_lock = threading.Lock()

# Daily token budget: bounds worst-case upstream cost from distributed abuse.
# When the budget is exhausted, the server transparently switches to the
# deterministic fallback (the game keeps working; only Jev calls stop until
# the next UTC day). Default 2M input tokens/day ≈ $0.084 at official pricing.
DAILY_TOKEN_BUDGET = int(os.environ.get("JEV_DAILY_TOKEN_BUDGET", "2000000"))
_daily_usage = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "input": 0}
_daily_lock = threading.Lock()


def _roll_daily_window() -> None:
    """Reset the daily counter when the UTC day changes. Caller holds the lock."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _daily_usage["date"] != today:
        _daily_usage["date"] = today
        _daily_usage["input"] = 0


def daily_budget_ok() -> bool:
    with _daily_lock:
        _roll_daily_window()
        return _daily_usage["input"] < DAILY_TOKEN_BUDGET


def daily_tokens_add(n: int) -> None:
    with _daily_lock:
        _roll_daily_window()
        _daily_usage["input"] += n


def daily_tokens_used() -> int:
    with _daily_lock:
        _roll_daily_window()
        return _daily_usage["input"]

_jev_down = False
_last_note: str | None = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("jev-snake")


def get_client_ip(req: Request) -> str:
    """Extract real client IP prioritizing Cloudflare and reverse proxy headers."""
    cf_ip = req.headers.get("cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()
    xff = req.headers.get("x-forwarded-for")
    if xff and xff.strip():
        return xff.split(",")[0].strip()
    return req.client.host if req.client and req.client.host else "127.0.0.1"


def rate_ok(ip: str) -> bool:
    """Token bucket rate limiter with automatic stale IP bucket pruning."""
    now = time.monotonic()
    with _bucket_lock:
        # Periodic pruning if bucket collection grows large
        if len(_buckets) > 1000:
            stale_keys = [k for k, dq in _buckets.items() if not dq or now - dq[-1] > BUCKET_WINDOW_S]
            for k in stale_keys:
                del _buckets[k]

        dq = _buckets.setdefault(ip, deque())
        while dq and now - dq[0] > BUCKET_WINDOW_S:
            dq.popleft()
        if len(dq) >= BUCKET_CAP:
            return False
        dq.append(now)
        return True


# ---- Request / Response Models ----
class DecideRequest(BaseModel):
    legal: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Candidate waypoints or moves",
    )
    state: dict[str, Any] | None = Field(
        default=None,
        description="Game snapshot (head, food, length, waypoints)",
    )
    instruction: str | None = Field(
        default=None,
        max_length=120,
        description="Player coaching directive (e.g. 'play safe')",
    )


class DecideResponse(BaseModel):
    action: str
    confidence: float
    probabilities: dict[str, float]
    source: str  # "jev" | "fallback"
    elapsed_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


# ---- State sanitize helpers (CWE-770: cap what gets forwarded into the LLM prompt) ----
_MAX_STATE_DEPTH = 4
_MAX_STR_LEN = 96
_MAX_ITEMS = 8


def _sanitize_state(value: Any, depth: int = 0) -> Any:
    """Recursively trim user-supplied state so it stays prompt-sized.

    - strings clamped to _MAX_STR_LEN chars
    - lists/dicts clamped to _MAX_ITEMS entries
    - nesting clamped to _MAX_STATE_DEPTH levels (deeper content dropped)
    """
    if depth > _MAX_STATE_DEPTH:
        return None
    if isinstance(value, str):
        return value[:_MAX_STR_LEN]
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                break
            out[str(k)[:_MAX_STR_LEN]] = _sanitize_state(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize_state(v, depth + 1) for v in value[:_MAX_ITEMS]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return None


# ---- FastAPI Lifespan ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync endpoints (blocking Jev HTTP call + SQLite) run in the anyio
    # threadpool. Default is 40 tokens; raise it so ~100 concurrent players
    # can hold an in-flight upstream call each without queueing.
    to_thread.current_default_thread_limiter().total_tokens = 80
    db.init_db()
    logger.info("jev-snake server initialized with persistent SQLite telemetry.")
    yield


app = FastAPI(title="jev-snake", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ---- Security Headers Middleware ----
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Same-origin only: the SPA has inline JS but fetches no external resources
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )
    return response


def build_request(payload: DecideRequest) -> dict[str, Any]:
    """Construct the structured JSON payload for TypeSafe System One Choice API."""
    style = (payload.instruction or "").strip()
    st = payload.state or {}
    head = st.get("head") or {}
    hx, hy = head.get("x", 0), head.get("y", 0)
    food = st.get("food") or {}
    fx, fy = food.get("x", 0), food.get("y", 0)
    waypoints = st.get("waypoints") or {}

    criteria: dict[str, str] = {}
    for raw_name in payload.legal:
        name = str(raw_name).strip()[:32]
        wp = waypoints.get(name)
        if wp is None:
            criteria[name] = f"option '{name}'"
            continue

        if isinstance(wp, dict):
            wx, wy = wp.get("x", 0), wp.get("y", 0)
        elif isinstance(wp, (list, tuple)) and len(wp) >= 2:
            wx, wy = wp[0], wp[1]
        else:
            wx, wy = 0, 0

        dist = abs(wx - hx) + abs(wy - hy)
        note = ", this IS the food" if name == "food" else (
            f", {abs(wx - fx) + abs(wy - fy)} cells from the food" if st.get("food") else ""
        )
        criteria[name] = f"waypoint '{name}' at ({wx},{wy}): {dist} cells from snake head{note}"

    state_text: dict[str, Any] = {
        "game": "Snake. Deterministic game code handles safety stepping; you choose the strategic waypoint.",
        "snapshot": st,
    }
    if style:
        state_text["player_style_instruction"] = (
            f"The player asked you to play with this style, let it guide the choice: \"{style[:120]}\""
        )

    return {
        "state": state_text,
        "model": MODEL,
        "questions": {
            "next": {
                "type": "choice",
                "instructions": (
                    "Which waypoint should the snake head toward for the next several ticks? "
                    "Default to the food waypoint; pick a different one only when the player style "
                    "instruction suggests it or the food is clearly unsafe."
                ),
                "criteria": criteria,
            }
        },
    }


def call_jev(payload: DecideRequest) -> tuple[str, float, dict[str, float], int, dict[str, Any]]:
    """Execute HTTP POST to TypeSafe Choice endpoint and return structured answer."""
    global _jev_down
    body = json.dumps(build_request(payload)).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "jev-snake/1.0",
        },
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _jev_down = False
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            _jev_down = True
            raise RuntimeError(f"TypeSafe auth failed ({exc.code})") from exc
        raise RuntimeError(f"TypeSafe HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"TypeSafe unreachable: {exc}") from exc

    ans = data.get("answers", {}).get("next", {})
    action = ans.get("choice")
    if action not in payload.legal:
        raise RuntimeError(f"Jev selected non-legal action: {action!r}")

    conf = float(ans.get("confidence", 0.0))
    raw_probs = ans.get("probabilities", {})
    probs = {str(k): float(v) for k, v in raw_probs.items()}
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    usage = data.get("usage", {})

    return action, conf, probs, elapsed_ms, usage


# ---- Routes ----
# NOTE: deliberately a sync def — FastAPI runs it in the threadpool so the
# blocking Jev HTTP call never stalls the event loop. Making this async again
# would serialize ALL players behind one upstream call (~0.9s each).
@app.post("/api/decide", response_model=DecideResponse)
def decide(req: Request, payload: DecideRequest):
    global _last_note
    client_ip = get_client_ip(req)
    if not rate_ok(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please slow down.")

    # Filter legal moves
    clean_legal = [str(x).strip() for x in payload.legal if x and len(str(x).strip()) <= 32]
    if not clean_legal:
        raise HTTPException(status_code=422, detail="Valid legal actions required.")
    payload.legal = clean_legal

    # Sanitize + cap the free-form state blob forwarded into the paid LLM prompt
    # (CWE-770 cost amplification: an unauthenticated client must not be able to
    # multiply upstream token spend by inflating request size).
    MAX_STATE_JSON_BYTES = 4096
    if payload.state is not None:
        try:
            st_size = len(json.dumps(payload.state, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="state must be JSON-serializable.")
        if st_size > MAX_STATE_JSON_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"state payload too large ({st_size} bytes; max {MAX_STATE_JSON_BYTES}).",
            )
        payload.state = _sanitize_state(payload.state)

    budget_exhausted = API_KEY and not daily_budget_ok()
    if API_KEY and not budget_exhausted:
        try:
            action, conf, probs, ms, usage = call_jev(payload)
            inp_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            daily_tokens_add(inp_tok)

            # Persist to SQLite atomically
            db.record_usage(inp_tok, out_tok, is_jev_request=True)

            return DecideResponse(
                action=action,
                confidence=round(conf, 3),
                probabilities={k: round(v, 3) for k, v in probs.items()},
                source="jev",
                elapsed_ms=ms,
                input_tokens=inp_tok,
                output_tokens=out_tok,
            )
        except Exception as exc:
            _last_note = str(exc)
            logger.warning("Falling back to deterministic pathing: %s", exc)
    elif budget_exhausted:
        _last_note = "Daily token budget exhausted; running in fallback mode"
        logger.warning("Daily token budget (%d) exhausted; falling back to deterministic pathing", DAILY_TOKEN_BUDGET)
    else:
        _last_note = "TYPESAFE_API_KEY is not configured"

    # Deterministic safe fallback: prioritize forward/food or default rotation
    order = {"food": 0, "open_area": 1, "border_loop": 2, "up": 3, "right": 4, "down": 5, "left": 6}
    fallback_action = sorted(payload.legal, key=lambda a: order.get(a, 99))[0]

    return DecideResponse(
        action=fallback_action,
        confidence=0.0,
        probabilities={},
        source="fallback",
        elapsed_ms=0,
        input_tokens=0,
        output_tokens=0,
    )


@app.get("/api/status")
async def status():
    # Generic note only — internal exception details are logged, never echoed (CWE-209)
    note = None
    if _last_note:
        note = "Jev upstream error; running in fallback mode" if _jev_down else "operational"
    return {
        "jev_key_configured": bool(API_KEY),
        "jev_down": _jev_down,
        "daily_input_tokens": daily_tokens_used(),
        "daily_budget": DAILY_TOKEN_BUDGET,
        "daily_budget_exhausted": not daily_budget_ok(),
        "note": note,
    }


@app.get("/api/usage")
async def usage():
    inp_tok, out_tok, requests = db.get_usage()
    cost = (inp_tok / 1e6 * PRICE_PER_MTOK_INPUT) + (out_tok / 1e6 * PRICE_PER_MTOK_OUTPUT)
    return {
        "input_tokens": inp_tok,
        "output_tokens": out_tok,
        "cost_usd": round(cost, 6),
        "price_note": "Jev official: $42 per 1B input tokens ($0.042 / 1M); output tokens are free",
        "requests": requests,
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8915"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
