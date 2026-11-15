"""GET /api/token -> LiveKit access token for a new room (the agent joins it).

Env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, optional ACCESS_CODE
(then the page must be opened with ?code=<ACCESS_CODE>).
"""

from __future__ import annotations

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from livekit import api


def issue_token(code: str = "", name: str = "speaker") -> tuple[int, dict[str, str]]:
    required = os.getenv("ACCESS_CODE", "")
    if required and not secrets.compare_digest(code, required):
        return 401, {"detail": "Access code required: open the link with ?code=… that you were given"}
    url, key, secret = (os.getenv(k, "") for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    if not (url and key and secret):
        return 500, {"detail": "Server is missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET"}
    room = f"translate-{secrets.token_hex(4)}"
    identity = f"{name[:24]}-{secrets.token_hex(3)}"
    jwt = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name[:24])
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .to_jwt()
    )
    return 200, {"url": url, "token": jwt, "room": room, "identity": identity}


class handler(BaseHTTPRequestHandler):  # Vercel's Python runtime looks for this name
    def do_GET(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        status, body = issue_token(q.get("code", [""])[0], q.get("name", ["speaker"])[0])
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
