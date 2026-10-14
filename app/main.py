import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .crypto import decrypt, encrypt, mask
from .db import Database
from .mailer import MailNotConfigured, send_mail
from .monitor import Monitor
from .probes import build_url, deployment_from_endpoint

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC_DIR = Path(__file__).parent / "static"

INT_SETTINGS = {
    "default_interval_minutes": (1, 1440),
    "baseline_window": (3, 500),
    "baseline_min_samples": (1, 500),
    "retry_count": (0, 10),
    "retry_gap_seconds": (0, 600),
    "retention_days": (1, 3650),
    "smtp_port": (1, 65535),
}
FLOAT_SETTINGS = {"ttft_threshold_pct": (1, 10000)}
TEXT_SETTINGS = ("smtp_host", "smtp_username", "smtp_from", "smtp_to")


class ModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "image"]
    endpoint: str = Field(min_length=1, max_length=1000)
    deployment: str = Field("", max_length=200)
    api_key: str | None = Field(None, max_length=500)
    enabled: bool = True
    interval_minutes: int = Field(30, ge=1, le=1440)
    ttft_threshold_pct: float | None = Field(None, gt=0, le=10000)
    baseline_manual_ms: float | None = Field(None, gt=0)

    @field_validator("name", "endpoint", "deployment", "api_key")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        u = urlsplit(v if "://" in v else "https://" + v)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("endpoint 必须是 http(s) 地址")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


class SettingsIn(BaseModel):
    values: dict[str, Any] = {}
    smtp_password: str | None = None
    smtp_password_clear: bool = False


class _LoginThrottle:
    def __init__(self, max_fails: int = 5, lock_seconds: int = 300):
        self.max_fails, self.lock_seconds = max_fails, lock_seconds
        self._state: dict[str, tuple[int, float]] = {}

    def check(self, ip: str) -> None:
        fails, until = self._state.get(ip, (0, 0))
        if until > time.time():
            raise HTTPException(429, f"登录失败次数过多，请 {int(until - time.time())} 秒后再试")

    def fail(self, ip: str) -> None:
        fails, _ = self._state.get(ip, (0, 0))
        fails += 1
        until = time.time() + self.lock_seconds if fails >= self.max_fails else 0
        self._state[ip] = (0 if until else fails, until)

    def ok(self, ip: str) -> None:
        self._state.pop(ip, None)


def create_app(
    db_path: Path | None = None,
    monitor_factory: Callable[[Database], Monitor] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.validate()
        db = Database(db_path or config.DB_PATH)
        monitor = (monitor_factory or Monitor)(db)
        app.state.db, app.state.monitor = db, monitor
        await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            db.close()

    app = FastAPI(title="模型监控平台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SECRET_KEY or "unset",
        session_cookie="mm_session",
        max_age=7 * 86400,
        same_site="lax",
        https_only=config.COOKIE_SECURE,
    )
    throttle = _LoginThrottle()

    def db_dep(request: Request) -> Database:
        return request.app.state.db

    def mon_dep(request: Request) -> Monitor:
        return request.app.state.monitor

    def require_login(request: Request) -> str:
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "未登录")
        return user

    # ---- auth ----
    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        ip = request.client.host if request.client else "?"
        throttle.check(ip)
        ok_user = hmac.compare_digest(body.username.encode(), config.ADMIN_USERNAME.encode())
        ok_pass = hmac.compare_digest(body.password.encode(), config.ADMIN_PASSWORD.encode())
        if not (ok_user and ok_pass):
            throttle.fail(ip)
            raise HTTPException(401, "用户名或密码错误")
        throttle.ok(ip)
        request.session["user"] = body.username
        return {"user": body.username}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user: str = Depends(require_login)):
        return {"user": user}

    # ---- models ----
    def serialize(m: dict, db: Database, monitor: Monitor, settings: dict[str, str]) -> dict:
        now = time.time()
        auto, samples = db.auto_baseline(m, int(settings["baseline_window"]), int(settings["baseline_min_samples"]))
        if m["baseline_manual_ms"]:
            baseline, source = m["baseline_manual_ms"], "manual"
        else:
            baseline, source = auto, ("auto" if auto else "learning")
        try:
            key_mask = mask(decrypt(m["api_key_enc"]))
        except RuntimeError:
            key_mask = "（无法解密）"
        latest = db.latest_check(m["id"])
        a24, n24 = db.availability(m["id"], now - 86400)
        a7, n7 = db.availability(m["id"], now - 7 * 86400)
        return {
            "id": m["id"],
            "name": m["name"],
            "kind": m["kind"],
            "endpoint": m["endpoint"],
            "deployment": m["deployment"],
            "request_url": build_url(m["endpoint"], m["kind"]),
            "api_key_masked": key_mask,
            "enabled": bool(m["enabled"]),
            "interval_minutes": m["interval_minutes"],
            "ttft_threshold_pct": m["ttft_threshold_pct"],
            "effective_threshold_pct": Monitor.threshold_pct(m, settings),
            "baseline_manual_ms": m["baseline_manual_ms"],
            "baseline_ms": baseline,
            "baseline_source": source,
            "baseline_samples": samples,
            "baseline_min_samples": int(settings["baseline_min_samples"]),
            "token_param": m["token_param"],
            "status": m["status"],
            "running": monitor.is_running(m["id"]),
            "last_check_at": m["last_check_at"],
            "next_run_at": m["next_run_at"] if m["enabled"] else None,
            "latest": latest,
            "availability_24h": a24,
            "cycles_24h": n24,
            "availability_7d": a7,
            "cycles_7d": n7,
        }

    def get_or_404(db: Database, model_id: int) -> dict:
        m = db.get_model(model_id)
        if not m:
            raise HTTPException(404, "模型不存在")
        return m

    def resolve_deployment(body: ModelIn) -> str:
        dep = body.deployment or deployment_from_endpoint(body.endpoint) or ""
        if not dep:
            raise HTTPException(422, "请填写部署名称（Foundry 中的 Deployment name）")
        return dep

    @app.get("/api/models")
    def list_models(_: str = Depends(require_login), db: Database = Depends(db_dep), mon: Monitor = Depends(mon_dep)):
        s = db.get_settings()
        return [serialize(m, db, mon, s) for m in db.list_models()]

    @app.post("/api/models")
    def create_model(
        body: ModelIn, _: str = Depends(require_login), db: Database = Depends(db_dep), mon: Monitor = Depends(mon_dep)
    ):
        if not body.api_key:
            raise HTTPException(422, "请填写 API Key")
        model_id = db.insert_model(
            {
                "name": body.name,
                "kind": body.kind,
                "endpoint": body.endpoint,
                "deployment": resolve_deployment(body),
                "api_key_enc": encrypt(body.api_key),
                "enabled": int(body.enabled),
                "interval_minutes": body.interval_minutes,
                "ttft_threshold_pct": body.ttft_threshold_pct,
                "baseline_manual_ms": body.baseline_manual_ms,
                "next_run_at": time.time(),
            }
        )
        return serialize(db.get_model(model_id), db, mon, db.get_settings())

    @app.put("/api/models/{model_id}")
    def update_model(
        model_id: int,
        body: ModelIn,
        _: str = Depends(require_login),
        db: Database = Depends(db_dep),
        mon: Monitor = Depends(mon_dep),
    ):
        old = get_or_404(db, model_id)
        fields: dict[str, Any] = {
            "name": body.name,
            "kind": body.kind,
            "endpoint": body.endpoint,
            "deployment": resolve_deployment(body),
            "enabled": int(body.enabled),
            "interval_minutes": body.interval_minutes,
            "ttft_threshold_pct": body.ttft_threshold_pct,
            "baseline_manual_ms": body.baseline_manual_ms,
        }
        if body.api_key:
            fields["api_key_enc"] = encrypt(body.api_key)
        target_changed = any(old[k] != fields[k] for k in ("kind", "endpoint", "deployment"))
        if target_changed:
            # A different target makes the learned baseline and token-param detection meaningless.
            fields.update(baseline_reset_at=time.time(), token_param="max_completion_tokens", status="unknown")
        if target_changed or body.api_key or (body.enabled and not old["enabled"]):
            fields["next_run_at"] = time.time()
        elif body.interval_minutes != old["interval_minutes"]:
            fields["next_run_at"] = min(
                old["next_run_at"] or time.time(), (old["last_check_at"] or time.time()) + body.interval_minutes * 60
            )
        db.update_model(model_id, fields)
        return serialize(db.get_model(model_id), db, mon, db.get_settings())

    @app.delete("/api/models/{model_id}")
    def delete_model(model_id: int, _: str = Depends(require_login), db: Database = Depends(db_dep)):
        get_or_404(db, model_id)
        db.delete_model(model_id)
        return {"ok": True}

    @app.post("/api/models/{model_id}/run")
    async def run_now(model_id: int, _: str = Depends(require_login), db: Database = Depends(db_dep), mon: Monitor = Depends(mon_dep)):
        get_or_404(db, model_id)
        if not mon.trigger(model_id, "manual"):
            raise HTTPException(409, "该模型正在检测中")
        return {"ok": True}

    @app.post("/api/models/{model_id}/reset-baseline")
    def reset_baseline(model_id: int, _: str = Depends(require_login), db: Database = Depends(db_dep)):
        get_or_404(db, model_id)
        db.update_model(model_id, {"baseline_reset_at": time.time()})
        return {"ok": True}

    @app.get("/api/models/{model_id}/checks")
    def list_checks(
        model_id: int, hours: float = 24, _: str = Depends(require_login), db: Database = Depends(db_dep)
    ):
        get_or_404(db, model_id)
        hours = min(max(hours, 1), 24 * 90)
        return list(reversed(db.list_checks(model_id, time.time() - hours * 3600)))

    # ---- alerts ----
    @app.get("/api/alerts")
    def list_alerts(limit: int = 200, _: str = Depends(require_login), db: Database = Depends(db_dep)):
        return db.list_alerts(min(max(limit, 1), 1000))

    # ---- settings ----
    def public_settings(db: Database) -> dict:
        s = db.get_settings()
        pw = s.pop("smtp_password_enc", "")
        return {**s, "smtp_password_set": bool(pw)}

    @app.get("/api/settings")
    def get_settings(_: str = Depends(require_login), db: Database = Depends(db_dep)):
        return public_settings(db)

    @app.put("/api/settings")
    def put_settings(body: SettingsIn, _: str = Depends(require_login), db: Database = Depends(db_dep)):
        out: dict[str, str] = {}
        for k, v in body.values.items():
            if k in INT_SETTINGS:
                lo, hi = INT_SETTINGS[k]
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(422, f"{k} 必须是整数")
                if not lo <= iv <= hi:
                    raise HTTPException(422, f"{k} 必须在 {lo}~{hi} 之间")
                out[k] = str(iv)
            elif k in FLOAT_SETTINGS:
                lo, hi = FLOAT_SETTINGS[k]
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    raise HTTPException(422, f"{k} 必须是数字")
                if not lo <= fv <= hi:
                    raise HTTPException(422, f"{k} 必须在 {lo}~{hi} 之间")
                out[k] = f"{fv:g}"
            elif k in TEXT_SETTINGS:
                out[k] = str(v or "").strip()
            elif k == "smtp_security":
                if v not in ("ssl", "starttls", "none"):
                    raise HTTPException(422, "smtp_security 必须是 ssl / starttls / none")
                out[k] = v
            else:
                raise HTTPException(422, f"未知设置项: {k}")
        if body.smtp_password_clear:
            out["smtp_password_enc"] = ""
        elif body.smtp_password:
            out["smtp_password_enc"] = encrypt(body.smtp_password)
        db.set_settings(out)
        return public_settings(db)

    @app.post("/api/settings/test-email")
    async def test_email(_: str = Depends(require_login), db: Database = Depends(db_dep)):
        try:
            await send_mail(
                db.get_settings(),
                "[模型监控] 测试邮件",
                "这是一封来自模型监控平台的测试邮件。收到说明 SMTP 配置正确。",
            )
        except MailNotConfigured as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"发送失败：{type(e).__name__}: {e}")
        return {"ok": True}

    # ---- frontend ----
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    return app


app = create_app()
