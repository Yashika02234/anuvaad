"""Local web server: the built page (web/dist) and the token endpoint.

Run:  python -m uvicorn server:app --port 8000
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

# same token logic as Vercel (loaded by path: "token" clashes with a stdlib module)
_spec = importlib.util.spec_from_file_location("lk_token_api", ROOT / "web" / "api" / "token.py")
_token_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_token_api)

app = FastAPI(title="English to Hindi live voice translator")


@app.get("/api/token")
def token(name: str = "speaker", code: str = "") -> JSONResponse:
    status, body = _token_api.issue_token(code, name)
    return JSONResponse(body, status_code=status)


# the built page (npm run build in web/)
DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
