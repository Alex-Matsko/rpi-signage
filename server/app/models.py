"""Модели данных. Все даты/время — наивные, в часовом поясе сервера (TZ)."""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    media: Mapped[MediaFile] = relationship(back_populates="posters")
    playlist_items: Mapped[list["PlaylistItem"]] = relationship(
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


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    items: Mapped[list["PlaylistItem"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"))
    poster_id: Mapped[int] = mapped_column(ForeignKey("posters.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    poster: Mapped[Poster] = relationship(back_populates="playlist_items")


class DeviceGroup(Base):
    __tablename__ = "device_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlists.id"), nullable=True
    )

    playlist: Mapped[Playlist | None] = relationship()
    devices: Mapped[list["Device"]] = relationship(back_populates="group")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_groups.id"), nullable=True
    )
    playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlists.id"), nullable=True
    )
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

    group: Mapped[DeviceGroup | None] = relationship(back_populates="devices")
    playlist: Mapped[Playlist | None] = relationship()

    @property
    def effective_playlist(self) -> Playlist | None:
        """Плейлист устройства; если не задан — плейлист его группы."""
        if self.playlist is not None:
            return self.playlist
        if self.group is not None:
            return self.group.playlist
        return None

    def is_online(self, offline_after_sec: int) -> bool:
        if self.last_seen_at is None:
            return False
        return (now() - self.last_seen_at).total_seconds() < offline_after_sec
