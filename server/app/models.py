"""Модели данных. Все даты/время — наивные, в часовом поясе сервера (TZ).

С v0.3 структура строится вокруг городов: экраны (кассы) принадлежат городу,
афиши назначаются напрямую на города и/или отдельные экраны (PosterTarget).
С v0.4 плейлисты возвращены как дополнительный, необязательный способ
назначения (Playlist/PlaylistItem/PlaylistTarget) поверх прямого — оба
работают независимо; экраны можно группировать вне привязки к городу
(DeviceGroup) для назначения плейлиста сразу на группу. Пользователи:
администраторы (видят всё) и менеджеры (обслуживают один или несколько
городов через UserCity).
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
    # Многие-ко-многим: города, доступные менеджеру (city_id выше остаётся
    # как подсказка основного города, но проверки доступа используют это)
    city_links: Mapped[list["UserCity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def cities(self) -> list["City"]:
        return sorted((link.city for link in self.city_links), key=lambda c: c.name)


class UserCity(Base):
    """Многие-ко-многим: города, обслуживаемые менеджером."""

    __tablename__ = "user_cities"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), primary_key=True)

    user: Mapped["User"] = relationship(back_populates="city_links")
    city: Mapped[City] = relationship()


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
    # Кто загрузил файл (для видимости в медиатеке менеджера, если файл ещё
    # не привязан ни к одной афише/плейлисту)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                     nullable=True)
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
    # Последний скриншот экрана
    screenshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Адрес локальной веб-панели устройства (из heartbeat)
    local_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    web_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ориентация физического экрана и раскладка одновременного показа афиш
    orientation: Mapped[str] = mapped_column(
        String(10), default="landscape", server_default="landscape"
    )
    # Сколько афиш показывается на экране одновременно: 1/2/3/4/6/8
    grid_layout: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Сеткой показываются только картинки; если включено, видео на этом
    # экране не показывается вовсе (см. build_grid_steps в агенте)
    grid_images_only: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )

    city: Mapped[City | None] = relationship(back_populates="devices")
    commands: Mapped[list["DeviceCommand"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )

    def is_online(self, offline_after_sec: int) -> bool:
        if self.last_seen_at is None:
            return False
        return (now() - self.last_seen_at).total_seconds() < offline_after_sec


class DeviceCommand(Base):
    """Команда для агента: агент забирает её при опросе и отчитывается о результате.

    Типы: restart_agent | reboot | screenshot.
    """

    __tablename__ = "device_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    # Доп. параметр (для shell — id сессии терминала)
    param: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|done|failed
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    device: Mapped[Device] = relationship(back_populates="commands")


class DeviceGroup(Base):
    """Операционная группа экранов (например: «только статика», «видео-кассы»).

    Не привязана к городу — устройство любого города может входить в любую
    группу; состав группы редактирует только администратор (см. deps.py:
    group_in_scope запрещает менеджеру назначать в плейлисте группу,
    содержащую устройства вне его городов).
    """

    __tablename__ = "device_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    members: Mapped[list["DeviceGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    @property
    def devices(self) -> list["Device"]:
        return sorted((m.device for m in self.members), key=lambda d: d.name)


class DeviceGroupMember(Base):
    __tablename__ = "device_group_members"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"),
                                           primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("device_groups.id"),
                                          primary_key=True)

    device: Mapped["Device"] = relationship()
    group: Mapped[DeviceGroup] = relationship(back_populates="members")


class Playlist(Base):
    """Именованная упорядоченная коллекция существующих афиш (Poster).

    Расписание/длительность/медиа не дублируются — берутся из самой афиши.
    Привязан к ровно одному городу — этим автоматически задаётся видимость
    в UI менеджера. Дополнительный, необязательный способ назначения контента
    поверх прямого PosterTarget (который продолжает работать независимо).
    """

    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    city: Mapped[City] = relationship()
    items: Mapped[list["PlaylistItem"]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )
    targets: Mapped[list["PlaylistTarget"]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan"
    )


class PlaylistItem(Base):
    """Афиша внутри плейлиста, в заданном порядке показа."""

    __tablename__ = "playlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"),
                                             index=True)
    poster_id: Mapped[int] = mapped_column(ForeignKey("posters.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0,
                                          server_default="0")

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    poster: Mapped[Poster] = relationship()


class PlaylistTarget(Base):
    """Назначение плейлиста: на город целиком, на конкретный экран,
    или на группу устройств (все экраны, входящие в неё сейчас)."""

    __tablename__ = "playlist_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"),
                                             index=True)
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"),
                                                nullable=True, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"),
                                                  nullable=True, index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("device_groups.id"),
                                                 nullable=True, index=True)

    playlist: Mapped[Playlist] = relationship(back_populates="targets")
    city: Mapped[City | None] = relationship()
    device: Mapped["Device"] = relationship()
    group: Mapped[DeviceGroup | None] = relationship()
