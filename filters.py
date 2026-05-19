from aiogram import Bot
from aiogram.types import Message, ChatMemberAdministrator, ChatMemberOwner
from functools import wraps
import re


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
    except Exception:
        return False


async def is_owner(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return isinstance(member, ChatMemberOwner)
    except Exception:
        return False


async def bot_is_admin(bot: Bot, chat_id: int) -> bool:
    me = await bot.get_me()
    return await is_admin(bot, chat_id, me.id)


def admin_only(func):
    """Декоратор: только для админов чата"""
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if message.chat.type == "private":
            return await func(message, *args, **kwargs)
        if not await is_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("❌ Эта команда только для администраторов!")
            return
        return await func(message, *args, **kwargs)
    return wrapper


def group_only(func):
    """Декоратор: только в группах"""
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if message.chat.type == "private":
            await message.reply("❌ Эта команда работает только в группах!")
            return
        return await func(message, *args, **kwargs)
    return wrapper


def contains_link(text: str) -> bool:
    pattern = r'(https?://|t\.me/|@\w+|bit\.ly|tinyurl\.com)'
    return bool(re.search(pattern, text, re.IGNORECASE))


def contains_arabic(text: str) -> bool:
    arabic_pattern = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+')
    return bool(arabic_pattern.search(text))


async def get_target_user(message: Message):
    """Получить цель команды (реплай или упоминание)"""
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()[1:] if message.text else []
    if args:
        # Попробовать найти по @username или ID
        target = args[0].lstrip("@")
        try:
            user_id = int(target)
            chat_member = await message.bot.get_chat_member(message.chat.id, user_id)
            return chat_member.user
        except (ValueError, Exception):
            try:
                chat_member = await message.bot.get_chat_member(message.chat.id, f"@{target}")
                return chat_member.user
            except Exception:
                pass
    return None


def parse_time(time_str: str) -> Optional[int]:
    """Парсит время: 1h, 30m, 1d -> секунды"""
    import re
    from typing import Optional
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    match = re.match(r"^(\d+)([smhdw]?)$", time_str.lower())
    if match:
        value, unit = match.groups()
        return int(value) * units.get(unit, 60)
    return None
