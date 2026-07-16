"""Модели данных. Все даты/время — наивные, в часовом поясе сервера (TZ).

С v0.3 структура строится вокруг городов: экраны (кассы) принадлежат городу,
афиши назначаются напрямую на города и/или отдельные экраны (PosterTarget),
плейлисты как сущность убраны. Пользователи: администраторы (всё) и
менеджеры города (только свой город).
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    devices: Mapped[list["Device"]] = relationship(back_populates="city")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_ADMIN,
                                      server_default=ROLE_ADMIN)
    # Для менеджера — его город; у администратора NULL
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"),
                                                nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    city: Mapped[City | None] = relationship()

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    orig_name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8))  # image | video
    mime: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    compatible: Mapped[bool] = mapped_column(Boolean, default=True)
    compat_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none | pending | running | done | failed
    transcode_status: Mapped[str] = mapped_column(
        String(8), default="none", server_default="none"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    posters: Mapped[list["Poster"]] = relationship(back_populates="media")


class Poster(Base):
    __tablename__ = "posters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"))
    display_seconds: Mapped[int] = mapped_column(Integer, default=10)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Ежедневное окно показа "HH:MM" (окно через полночь допустимо: 20:00–02:00)
    daily_from: Mapped[str | None] = mapped_column(String(5), nullable=True)
    daily_until: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Битовая маска дней недели (бит 0 = понедельник); NULL/0 = все дни
    weekdays_mask: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Порядок в ротации экрана (меньше — раньше)
    sort_order: Mapped[int] = mapped_column(Integer, default=0,
                                            server_default="0")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    media: Mapped[MediaFile] = relationship(back_populates="posters")
    targets: Mapped[list["PosterTarget"]] = relationship(
        back_populates="poster", cascade="all, delete-orphan"
    )

    @property
    def status(self) -> str:
        """Текущий статус: active | disabled | expired | scheduled."""
        if not self.enabled:
            return "disabled"
        t = now()
        if self.expires_at and self.expires_at <= t:
            return "expired"
        if self.starts_at and self.starts_at > t:
            return "scheduled"
        return "active"

    def targets_city(self, city_id: int | None) -> bool:
        return any(t.city_id == city_id for t in self.targets if t.city_id)


class PosterTarget(Base):
    """Назначение афиши: на город (все его экраны, включая будущие)
    или на конкретный экран."""

    __tablename__ = "poster_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    poster_id: Mapped[int] = mapped_column(ForeignKey("posters.id"),
                                           index=True)
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"),
                                                nullable=True, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"),
                                                  nullable=True, index=True)

    poster: Mapped[Poster] = relationship(back_populates="targets")
    city: Mapped[City | None] = relationship()
    device: Mapped["Device"] = relationship()


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"),
                                                nullable=True)
    # Одноразовый код подключения; после регистрации агента обнуляется
    pairing_code: Mapped[str | None] = mapped_column(
        String(16), unique=True, nullable=True
    )
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    # Телеметрия из heartbeat
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    uptime_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    city: Mapped[City | None] = relationship(back_populates="devices")

    def is_online(self, offline_after_sec: int) -> bool:
        if self.last_seen_at is None:
            return False
        return (now() - self.last_seen_at).total_seconds() < offline_after_sec
