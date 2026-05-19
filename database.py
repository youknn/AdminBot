from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, Boolean, BigInteger, Text, DateTime, JSON
from sqlalchemy.future import select
from sqlalchemy import update, delete
from datetime import datetime
from typing import Optional
import os

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ─── МОДЕЛИ ───────────────────────────────────────────────

class GroupSettings(Base):
    __tablename__ = "group_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Welcome
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    welcome_buttons: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON строка
    # Goodbye
    goodbye_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    goodbye_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Moderation
    antiflood: Mapped[bool] = mapped_column(Boolean, default=False)
    antiflood_limit: Mapped[int] = mapped_column(Integer, default=5)
    antiflood_action: Mapped[str] = mapped_column(String(20), default="mute")  # mute/kick/ban
    antiflood_time: Mapped[int] = mapped_column(Integer, default=10)  # секунд
    antispam: Mapped[bool] = mapped_column(Boolean, default=False)
    antilink: Mapped[bool] = mapped_column(Boolean, default=False)
    antilink_action: Mapped[str] = mapped_column(String(20), default="delete")
    antiarab: Mapped[bool] = mapped_column(Boolean, default=False)  # антиарабский текст
    # Captcha
    captcha_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_timeout: Mapped[int] = mapped_column(Integer, default=60)
    # Warn system
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    warn_action: Mapped[str] = mapped_column(String(20), default="ban")  # ban/kick/mute
    warn_mute_time: Mapped[int] = mapped_column(Integer, default=3600)
    # Misc
    rules: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_channel: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="ru")
    ro_mode: Mapped[bool] = mapped_column(Boolean, default=False)  # только чтение


class UserWarn(Base):
    __tablename__ = "user_warns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    warned_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(BigInteger)


class BadWord(Base):
    __tablename__ = "bad_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    word: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(20), default="delete")  # delete/warn/mute


class FloodControl(Base):
    """Временная таблица для отслеживания флуда (лучше через Redis, но используем БД)"""
    __tablename__ = "flood_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    message_count: Mapped[int] = mapped_column(Integer, default=1)
    last_message: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CaptchaPending(Base):
    __tablename__ = "captcha_pending"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str] = mapped_column(String(20))
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── ФУНКЦИИ БД ────────────────────────────────────────────

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_group(chat_id: int) -> GroupSettings:
    async with async_session() as session:
        result = await session.get(GroupSettings, chat_id)
        if not result:
            result = GroupSettings(chat_id=chat_id)
            session.add(result)
            await session.commit()
            await session.refresh(result)
        return result


async def update_group(chat_id: int, **kwargs):
    async with async_session() as session:
        await session.execute(
            update(GroupSettings).where(GroupSettings.chat_id == chat_id).values(**kwargs)
        )
        await session.commit()


async def get_warns(chat_id: int, user_id: int) -> list[UserWarn]:
    async with async_session() as session:
        result = await session.execute(
            select(UserWarn).where(
                UserWarn.chat_id == chat_id,
                UserWarn.user_id == user_id
            )
        )
        return result.scalars().all()


async def add_warn(chat_id: int, user_id: int, reason: str, warned_by: int) -> int:
    async with async_session() as session:
        warn = UserWarn(chat_id=chat_id, user_id=user_id, reason=reason, warned_by=warned_by)
        session.add(warn)
        await session.commit()
    warns = await get_warns(chat_id, user_id)
    return len(warns)


async def remove_warn(chat_id: int, user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(UserWarn).where(
                UserWarn.chat_id == chat_id,
                UserWarn.user_id == user_id
            ).order_by(UserWarn.created_at.desc()).limit(1)
        )
        warn = result.scalar_one_or_none()
        if warn:
            await session.delete(warn)
            await session.commit()
            return True
        return False


async def reset_warns(chat_id: int, user_id: int):
    async with async_session() as session:
        await session.execute(
            delete(UserWarn).where(
                UserWarn.chat_id == chat_id,
                UserWarn.user_id == user_id
            )
        )
        await session.commit()


async def get_note(chat_id: int, name: str) -> Optional[Note]:
    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.chat_id == chat_id, Note.name == name.lower())
        )
        return result.scalar_one_or_none()


async def get_all_notes(chat_id: int) -> list[Note]:
    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.chat_id == chat_id)
        )
        return result.scalars().all()


async def save_note(chat_id: int, name: str, content: str, created_by: int):
    async with async_session() as session:
        existing = await session.execute(
            select(Note).where(Note.chat_id == chat_id, Note.name == name.lower())
        )
        note = existing.scalar_one_or_none()
        if note:
            note.content = content
        else:
            note = Note(chat_id=chat_id, name=name.lower(), content=content, created_by=created_by)
            session.add(note)
        await session.commit()


async def delete_note(chat_id: int, name: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            delete(Note).where(Note.chat_id == chat_id, Note.name == name.lower())
        )
        await session.commit()
        return result.rowcount > 0


async def get_bad_words(chat_id: int) -> list[BadWord]:
    async with async_session() as session:
        result = await session.execute(
            select(BadWord).where(BadWord.chat_id == chat_id)
        )
        return result.scalars().all()


async def add_bad_word(chat_id: int, word: str, action: str = "delete"):
    async with async_session() as session:
        existing = await session.execute(
            select(BadWord).where(BadWord.chat_id == chat_id, BadWord.word == word.lower())
        )
        if not existing.scalar_one_or_none():
            bw = BadWord(chat_id=chat_id, word=word.lower(), action=action)
            session.add(bw)
            await session.commit()


async def remove_bad_word(chat_id: int, word: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            delete(BadWord).where(BadWord.chat_id == chat_id, BadWord.word == word.lower())
        )
        await session.commit()
        return result.rowcount > 0
