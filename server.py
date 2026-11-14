"""Local web server: serves the built React UI (web/dist) and the token endpoint.

Run:  python -m uvicorn server:app --port 8000   then open http://localhost:8000

In production the same app is hosted on Vercel (web/ folder): the React app is built
there, and web/api/token.py runs as a serverless function.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# reuse the exact token logic Vercel runs (loaded by path: "token" would clash with a stdlib module)
_spec = importlib.util.spec_from_file_location("lk_token_api", ROOT / "web" / "api" / "token.py")
_token_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_token_api)

app = FastAPI(title="English to Hindi live voice translator")


@app.get("/api/token")
def token(name: str = "speaker", code: str = "") -> JSONResponse:
    status, body = _token_api.issue_token(code, name)
    return JSONResponse(body, status_code=status)


# the React app, built with `npm run build` in web/ (for live editing use `npm run dev` there,
# which proxies /api to this server)
DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
