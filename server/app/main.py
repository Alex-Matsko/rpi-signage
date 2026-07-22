import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, media, security, worker
from .db import Base, SessionLocal, engine
from .deps import AuthRedirect
from .migrate import rename_legacy_group_playlist_tables, run_migrations
from .models import User
from .routers import (
    agent, auth, dashboard, devices, groups, media_lib, playlists, posters,
    publish, users,
)

APP_VERSION = "0.17.1"


def _bootstrap_admin() -> None:
    """Создаёт первого администратора, если БД пуста."""
    with SessionLocal() as db:
        if db.query(User).count() > 0:
            return
        password = config.ADMIN_PASSWORD
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True
        db.add(User(
            username=config.ADMIN_USER,
            password_hash=security.hash_password(password),
        ))
        db.commit()
        if generated:
            print(
                f"[signage] Создан администратор «{config.ADMIN_USER}» "
                f"со случайным паролем: {password}\n"
                "[signage] Задайте ADMIN_PASSWORD в окружении, чтобы "
                "использовать свой пароль при первом запуске.",
                file=sys.stderr,
            )


def create_app() -> FastAPI:
    config.ensure_dirs()
    rename_legacy_group_playlist_tables(engine)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    _bootstrap_admin()
    worker.start()
    media.cleanup_grid_composites()

    app = FastAPI(title="RPi Signage", version=APP_VERSION,
                  docs_url=None, redoc_url=None)

    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request: Request, exc: AuthRedirect):
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": APP_VERSION}

    # Раздача файлов агента: установка на RPi одной командой с этого сервера
    agent_dir = Path(
        os.environ.get(
            "SIGNAGE_AGENT_DIR",
            Path(__file__).resolve().parents[2] / "agent",
        )
    )

    @app.get("/install.sh", include_in_schema=False)
    def install_sh():
        path = agent_dir / "install.sh"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/x-shellscript")

    @app.get("/agent.py", include_in_schema=False)
    def agent_py():
        path = agent_dir / "agent.py"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/x-python")

    @app.get("/placeholder.png", include_in_schema=False)
    def placeholder_png():
        path = agent_dir / "placeholder.png"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    @app.get("/waiting_bg.png", include_in_schema=False)
    def waiting_bg_png():
        path = agent_dir / "waiting_bg.png"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(publish.router)
    app.include_router(posters.router)
    app.include_router(playlists.router)
    app.include_router(media_lib.router)
    app.include_router(devices.router)
    app.include_router(groups.router)
    app.include_router(users.router)
    app.include_router(agent.router)
    return app


app = create_app()
