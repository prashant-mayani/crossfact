"""Crossfact web app.

Production entrypoint is `python -m app.server` (adds graceful drain). The ASGI
app listens on :8080 for the UI (reached only through the Cloudflare tunnel)
and serves Prometheus metrics on a separate port that the tunnel never routes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, evalmetrics, exact, faults, orchestrator, providers
from . import telemetry as tm
from .config import VERSION, cfg, pipeline_setting
from .lifecycle import INSTANCE_ID, LIFECYCLE
from .orchestrator import run_pipeline
from .promotion import PROMOTIONS
from .providers import EFFORTS, enabled_providers, health

log = logging.getLogger("crossfact")
BASE = os.path.dirname(__file__)
MODES = ("auto", "consensus", "union", "single")  # auto: a route per question (orchestrator)
DRAINING_MSG = "Crossfact is restarting for an update. Your question wasn't sent; try again in a minute."

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    auth.ensure_owner()  # single-user: one account, created from the Vault-held bootstrap password
    tm.BUILD.info({"version": VERSION, "instance": INSTANCE_ID})
    orchestrator.prime_metrics()  # every bounded series at zero, so increase() sees the first event
    providers.prime_metrics()
    evalmetrics.register()  # the nightly answer-quality check's results, read at scrape time
    tm.init_tracing(cfg()["tracing"], VERSION)
    tm.start_metrics_server(int(cfg()["server"]["metrics_port"]))
    LIFECYCLE.start()
    PROMOTIONS.start()  # background checks of any side's preferred model (defaults.<side>.prefer)
    yield
    PROMOTIONS.stop()
    LIFECYCLE.stop()
    tm.shutdown_tracing()  # send the spans still buffered (the runs that finished while draining)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
# Static files are cached for an hour, by browsers and by Cloudflare's edge. A
# per-build query string makes each deploy's CSS and JS a new URL, so a new
# version shows up at once instead of up to an hour later (or half-styled).
templates.env.globals["asset_v"] = VERSION[:12]


@app.middleware("http")
async def observe_and_harden(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or ("/static" if request.url.path.startswith("/static") else "other")
        tm.HTTP_REQUESTS.labels(path, request.method, str(status)).inc()
        tm.HTTP_DURATION.labels(path, request.method).observe(time.perf_counter() - started)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Strict-Transport-Security"] = "max-age=15552000"
    response.headers.setdefault("Cache-Control", "no-store" if not request.url.path.startswith("/static")
                                else "public, max-age=3600")
    return response


# ---- helpers --------------------------------------------------------------------

def sess(request: Request, need_totp=True) -> dict | None:
    s = auth.read_session(request.cookies.get(auth.COOKIE))
    if not s or (need_totp and not s["totp_ok"]):
        return None
    u = db.get_user(uid=s["user_id"])
    if not u or u["disabled"]:
        return None
    s["user"] = u
    return s


def guard(request: Request) -> dict:
    s = sess(request)
    if not s:
        raise HTTPException(303, headers={"Location": "/login"})
    return s


def admin_guard(request: Request) -> dict:
    s = guard(request)
    if s["user"]["role"] != "admin":
        raise HTTPException(403)
    return s


def check_csrf(s: dict, token: str):
    if not token or token != s["csrf"]:
        raise HTTPException(403, "bad csrf token")


def page(request: Request, name: str, s=None, status_code=200, **ctx):
    ctx.update({"s": s, "user": s["user"] if s else None,
                "faults": faults.active() if s and s["user"]["role"] == "admin" else []})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def daily_quota() -> int:
    q = cfg()["quotas"]
    return int(q.get("daily_runs") or q.get("admin_daily_runs") or 200)


def ui_registry() -> list[dict]:
    out = []
    for pid, p in enabled_providers().items():
        h = health(pid)
        open_models = [m for m, c in h["circuits"].items() if c["state"] == "open"]
        out.append({"id": pid, "label": p.get("label", pid), "kind": p["kind"],
                    "trains": bool(p.get("trains_on_data")), "ok": h["configured"],
                    "open": open_models, "models": p.get("models", [])})
    return out


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


# ---- probes -------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    if LIFECYCLE.draining.is_set():
        return JSONResponse({"ready": False, "reason": "draining"}, status_code=503)
    try:
        db.get_user(username="__probe__")
    except Exception as e:  # database unreachable
        return JSONResponse({"ready": False, "reason": type(e).__name__}, status_code=503)
    return {"ready": True}


# ---- auth flow -------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return page(request, "login.html", error=None if auth.ensure_owner(quiet=True) else auth.NO_ACCOUNT)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip, uname = client_ip(request), username.strip().lower()
    for key in (f"u:{uname}", f"ip:{ip}"):
        if db.lockout_check(key):
            return page(request, "login.html", error="Too many attempts. Try again in 15 minutes.", status_code=429)
    if not auth.ensure_owner(quiet=True):  # the bootstrap password may have been set since startup
        return page(request, "login.html", error=auth.NO_ACCOUNT, status_code=503)
    u = db.get_user(username=uname)
    if not u or u["disabled"] or not auth.verify_pw(u["pw_hash"], password):
        db.lockout_fail(f"u:{uname}")
        db.lockout_fail(f"ip:{ip}")
        return page(request, "login.html", error="Wrong username or password.", status_code=401)
    db.lockout_clear(f"u:{uname}")
    cookie_val, _ = auth.issue_cookie(u["id"])
    resp = RedirectResponse("/totp", status_code=303)
    resp.set_cookie(auth.COOKIE, cookie_val, httponly=True, secure=True, samesite="lax",
                    max_age=cfg()["session_hours"] * 3600)
    return resp


@app.get("/totp", response_class=HTMLResponse)
def totp_form(request: Request):
    s = sess(request, need_totp=False)
    if not s:
        return RedirectResponse("/login", status_code=303)
    u = s["user"]
    if not u["totp_secret_enc"]:  # first sign-in: enroll
        secret = auth.new_totp_secret()
        db.update_user(u["id"], totp_secret_enc=auth.encrypt_totp(secret))
        qr = auth.totp_qr_svg(auth.totp_uri(u["username"], secret))
        return page(request, "enroll.html", s=s, qr=qr, secret=secret, error=None)
    return page(request, "totp.html", s=s, error=None)


@app.post("/totp")
def totp_check(request: Request, code: str = Form(...)):
    s = sess(request, need_totp=False)
    if not s:
        return RedirectResponse("/login", status_code=303)
    key = f"totp:{s['user_id']}"
    if db.lockout_check(key):
        return page(request, "totp.html", s=s, error="Too many wrong codes. Try again in 15 minutes.",
                    status_code=429)
    if not auth.totp_verify(auth.decrypt_totp(s["user"]["totp_secret_enc"]), code):
        db.lockout_fail(key)
        return page(request, "totp.html", s=s, error="That code didn't match. Check the time on your phone "
                                                      "and try the next one.", status_code=401)
    db.lockout_clear(key)
    db.mark_totp_ok(s["sid"])
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    s = sess(request, need_totp=False)
    if s:
        db.drop_session(s["sid"])
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---- chat ---------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return RedirectResponse("/chat" if sess(request) else "/login", status_code=303)


@app.get("/chat", response_class=HTMLResponse)
@app.get("/chat/{tid}", response_class=HTMLResponse)
def chat(request: Request, tid: str | None = None):
    s = guard(request)
    msgs, thread = [], None
    if tid:
        thread = db.get_thread(tid, s["user_id"])
        if not thread:
            raise HTTPException(404)
        msgs = db.thread_messages(tid)
        for m in msgs:
            m["pipeline"] = json.loads(m["pipeline_json"]) if m["pipeline_json"] else None
    boot = {"providers": ui_registry(), "defaults": PROMOTIONS.effective_defaults(), "csrf": s["csrf"],
            "efforts": EFFORTS}
    return page(request, "chat.html", s=s, threads=db.user_threads(s["user_id"]), thread=thread, msgs=msgs,
                efforts=EFFORTS, files=db.user_files(s["user_id"]), used=db.usage_today(s["user_id"]),
                quota=daily_quota(), boot=boot)


def _history_text(tid: str) -> str:
    budget = int(pipeline_setting("history_char_budget", 24000))
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
             for m in db.thread_messages(tid) if m["content"] and m["status"] == "done"]
    return "\n\n".join(lines)[-budget:]


_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}$")


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _asker_day(today: str, tz: str) -> tuple[date | None, str]:
    """The asker's own date and time-zone name, from their browser. The date is
    believed only within a day of the server's UTC date (every time zone is); the
    name is only a label for the models and the working."""
    try:
        day = date.fromisoformat(today) if today else None
    except ValueError:
        return None, ""
    if day is None or abs((day - _utc_today()).days) > 1:
        return None, ""
    return day, tz if len(tz) <= 64 and _TZ_NAME.match(tz) else ""


def _start_job(mid: str, uid: str, question: str, history: str, side_a: dict, side_b: dict, mode: str,
               file_paths: list[str], today: date | None = None, tz: str = "") -> None:
    lock = threading.Lock()

    def progress(stage: dict) -> None:
        with lock:
            m = db.get_message(mid)
            pl = json.loads(m["pipeline_json"]) if m and m["pipeline_json"] else {"stages": []}
            pl.setdefault("stages", []).append(stage)
            db.update_message(mid, pipeline=pl)

    def work() -> None:  # already counted in flight by the caller (LIFECYCLE.begin)
        try:
            out = run_pipeline(question, history, side_a, side_b, mode, file_paths, progress, today=today, tz=tz)
            record = {"stages": out["stages"], "outcome": out["outcome"], "degraded": out["degraded"],
                      "route": out.get("route"), "route_note": out.get("route_note"),
                      "duration_s": out["duration_s"], "sides": {"A": side_a, "B": side_b}, "mode": mode,
                      "files": [os.path.basename(p) for p in file_paths]}
            status = "error" if out["outcome"] == "error" else "done"
            db.update_message(mid, content=out["final"], status=status, pipeline=record)
            if out["outcome"] == "error":
                db.refund_usage(uid)
        except Exception as e:  # defensive: run_pipeline already catches provider failures
            log.exception("run %s crashed", mid)
            db.update_message(mid, content=f"Run failed: {type(e).__name__}", status="error")
            db.refund_usage(uid)
        finally:
            LIFECYCLE.end(mid)

    threading.Thread(target=work, name=f"run-{mid[:8]}", daemon=True).start()


@app.post("/ask")
def ask_route(request: Request,
              question: str = Form(...), thread_id: str = Form(""),
              a_provider: str = Form(...), a_model: str = Form(...), a_effort: str = Form("medium"),
              b_provider: str = Form(""), b_model: str = Form(""), b_effort: str = Form("medium"),
              mode: str = Form("auto"), file_ids: str = Form(""), csrf: str = Form(...),
              today: str = Form(""), tz: str = Form("")):
    s = guard(request)
    check_csrf(s, csrf)
    if LIFECYCLE.draining.is_set():
        raise HTTPException(503, DRAINING_MSG)
    if mode not in MODES or a_effort not in EFFORTS or b_effort not in EFFORTS:
        raise HTTPException(400, "bad mode or effort")
    if mode != "single" and not (b_provider and b_model):
        raise HTTPException(400, "side B is required for consensus and union modes")
    question = question.strip()[:20000]
    if not question:
        raise HTTPException(400, "empty question")
    day, zone = _asker_day(today, tz)
    free = mode == "auto" and exact.solve(question, day, zone) is not None  # computed in code: no model, no quota
    if not free and db.usage_today(s["user_id"]) >= daily_quota():
        raise HTTPException(429, "Daily run quota reached. It resets at midnight.")
    if thread_id and not db.get_thread(thread_id, s["user_id"]):
        raise HTTPException(404)
    tid = thread_id or db.create_thread(s["user_id"], question[:80])
    history = _history_text(tid) if thread_id else ""
    paths = []
    for fid in [f for f in file_ids.split(",") if f][:5]:
        row = db.get_file(fid, s["user_id"])
        if row and os.path.exists(row["path"]):
            paths.append(row["path"])
    side_a = {"provider": a_provider, "model": a_model, "effort": a_effort}
    side_b = {"provider": b_provider, "model": b_model, "effort": b_effort}
    db.add_message(tid, "user", question)
    mid = db.add_message(tid, "assistant", status="running", owner=INSTANCE_ID,
                         pipeline={"stages": [], "mode": mode, "sides": {"A": side_a, "B": side_b},
                                   "file_ids": file_ids})
    if not LIFECYCLE.begin(mid):  # a drain started since the check above: refuse, uncounted
        db.update_message(mid, content=DRAINING_MSG, status="error")
        raise HTTPException(503, DRAINING_MSG)
    if not free:
        db.bump_usage(s["user_id"])
    try:
        _start_job(mid, s["user_id"], question, history, side_a, side_b, mode, paths, day, zone)
    except BaseException:  # the run never started: don't leave a drain waiting on it
        LIFECYCLE.end(mid)
        db.update_message(mid, content="The run could not start. Try again.", status="error")
        if not free:
            db.refund_usage(s["user_id"])
        raise
    return JSONResponse({"thread_id": tid, "message_id": mid})


@app.get("/api/messages/{mid}")
def poll_message(request: Request, mid: str):
    s = guard(request)
    m = db.get_message(mid)
    if not m or db.message_owner_user(mid) != s["user_id"]:
        raise HTTPException(404)
    return {"status": m["status"], "content": m["content"],
            "pipeline": json.loads(m["pipeline_json"]) if m["pipeline_json"] else None}


@app.post("/threads/{tid}/delete")
def del_thread(request: Request, tid: str, csrf: str = Form(...), back: str = Form("")):
    """Delete a chat and all its messages (uploaded files stay on the Files page).
    Stays on the chat you were reading, unless that is the one deleted."""
    s = guard(request)
    check_csrf(s, csrf)
    db.delete_thread(tid, s["user_id"])
    if back and back != tid and db.get_thread(back, s["user_id"]):
        return RedirectResponse(f"/chat/{back}", status_code=303)
    return RedirectResponse("/chat", status_code=303)


# ---- files ------------------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))[:80] or "file"


@app.get("/files", response_class=HTMLResponse)
def files_page(request: Request):
    s = guard(request)
    up = cfg()["uploads"]
    return page(request, "files.html", s=s, files=db.user_files(s["user_id"]), retention=up["retention_days"],
                max_mb=up["max_mb"])


@app.post("/files/upload")
async def upload(request: Request, f: UploadFile = File(...), csrf: str = Form(...)):
    s = guard(request)
    check_csrf(s, csrf)
    up = cfg()["uploads"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in up["allowed_ext"]:
        raise HTTPException(400, f"file type {ext or '(none)'} is not allowed")
    data = await f.read(up["max_mb"] * 1024 * 1024 + 1)
    if len(data) > up["max_mb"] * 1024 * 1024:
        raise HTTPException(400, f"file is larger than {up['max_mb']} MB")
    udir = os.path.join(cfg()["data_dir"], "uploads", s["user_id"])
    os.makedirs(udir, exist_ok=True)
    path = os.path.join(udir, f"{db.new_id()[:8]}_{_safe_name(f.filename or 'file')}")
    with open(path, "wb") as fh:
        fh.write(data)
    fid = db.add_file(s["user_id"], f.filename or "file", path, len(data))
    return JSONResponse({"id": fid, "name": f.filename, "size": len(data)})


@app.post("/files/{fid}/delete")
def delete_file(request: Request, fid: str, csrf: str = Form(...)):
    s = guard(request)
    check_csrf(s, csrf)
    row = db.get_file(fid, s["user_id"])
    if row:
        try:
            os.remove(row["path"])
        except FileNotFoundError:
            pass
        db.delete_file_row(fid, s["user_id"])
    return RedirectResponse("/files", status_code=303)


@app.get("/files/{fid}/download")
def download(request: Request, fid: str):
    s = guard(request)
    row = db.get_file(fid, s["user_id"])
    if not row or not os.path.exists(row["path"]):
        raise HTTPException(404)
    return FileResponse(row["path"], filename=row["orig_name"])


# ---- settings & admin --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return page(request, "settings.html", s=guard(request), msg=None, error=None)


@app.post("/settings/password")
def change_pw(request: Request, current: str = Form(...), new: str = Form(...), csrf: str = Form(...)):
    s = guard(request)
    check_csrf(s, csrf)
    if not auth.verify_pw(s["user"]["pw_hash"], current):
        return page(request, "settings.html", s=s, msg=None, error="Current password is wrong.")
    if len(new) < 10:
        return page(request, "settings.html", s=s, msg=None, error="Use at least 10 characters.")
    db.update_user(s["user_id"], pw_hash=auth.hash_pw(new))
    return page(request, "settings.html", s=s, msg="Password changed.", error=None)


def _admin_page(request, s, msg=None, error=None):
    provider_targets = ["*"]
    for pid, p in enabled_providers().items():
        provider_targets.append(pid)
        provider_targets += [f"{pid}/{m['id']}" for m in p.get("models", [])]
    return page(request, "admin.html", s=s, msg=msg, error=error, providers=ui_registry(),
                fault_modes=faults.MODES, fault_targets=provider_targets, active_faults=db.active_faults())


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return _admin_page(request, admin_guard(request))


@app.post("/admin/faults")
def admin_add_fault(request: Request, target: str = Form(...), mode: str = Form(...),
                    probability: float = Form(1.0), latency_s: float = Form(0.0), minutes: int = Form(15),
                    csrf: str = Form(...)):
    s = admin_guard(request)
    check_csrf(s, csrf)
    if mode not in faults.MODES:
        raise HTTPException(400, "unknown fault mode")
    minutes = max(1, min(int(minutes), 240))  # chaos always expires, at most 4 hours
    probability = max(0.0, min(float(probability), 1.0))
    db.add_fault(target, mode, probability, max(0.0, float(latency_s)), time.time() + minutes * 60,
                 s["user"]["username"])
    faults.invalidate()
    log.warning("fault injected by %s: %s %s p=%.2f for %d min", s["user"]["username"], target, mode,
                probability, minutes)
    return RedirectResponse("/admin#faults", status_code=303)


@app.post("/admin/faults/{fid}/delete")
def admin_clear_fault(request: Request, fid: str, csrf: str = Form(...)):
    s = admin_guard(request)
    check_csrf(s, csrf)
    db.delete_fault(fid)
    faults.invalidate()
    return RedirectResponse("/admin#faults", status_code=303)
