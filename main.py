import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPermissions, ChatMemberUpdated, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

import database as db
from filters import (
    is_admin, is_owner, admin_only, group_only,
    contains_link, contains_arabic, get_target_user, parse_time, bot_is_admin
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Хранилище флуда в памяти {chat_id: {user_id: [timestamps]}}
flood_storage: dict = {}

# ─── FSM STATES ───────────────────────────────────────────

class SettingsState(StatesGroup):
    waiting_welcome = State()
    waiting_rules = State()
    waiting_bad_word = State()
    waiting_note_name = State()
    waiting_note_content = State()


# ─── УТИЛИТЫ ──────────────────────────────────────────────

def mention(user) -> str:
    name = user.full_name or user.first_name or "User"
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def build_settings_keyboard(settings: db.GroupSettings) -> InlineKeyboardMarkup:
    def status(val): return "✅" if val else "❌"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{status(settings.welcome_enabled)} Welcome", callback_data="toggle_welcome"),
            InlineKeyboardButton(text=f"{status(settings.goodbye_enabled)} Goodbye", callback_data="toggle_goodbye"),
        ],
        [
            InlineKeyboardButton(text=f"{status(settings.antiflood)} Антифлуд", callback_data="toggle_antiflood"),
            InlineKeyboardButton(text=f"{status(settings.antilink)} Антилинк", callback_data="toggle_antilink"),
        ],
        [
            InlineKeyboardButton(text=f"{status(settings.antispam)} Антиспам", callback_data="toggle_antispam"),
            InlineKeyboardButton(text=f"{status(settings.antiarab)} Антиараб", callback_data="toggle_antiarab"),
        ],
        [
            InlineKeyboardButton(text=f"{status(settings.captcha_enabled)} Капча", callback_data="toggle_captcha"),
            InlineKeyboardButton(text=f"{status(settings.ro_mode)} Только чтение", callback_data="toggle_ro"),
        ],
        [
            InlineKeyboardButton(text="✏️ Welcome текст", callback_data="set_welcome"),
            InlineKeyboardButton(text="📜 Правила", callback_data="set_rules"),
        ],
        [
            InlineKeyboardButton(text="⚠️ Варн лимит", callback_data="set_warn_limit"),
            InlineKeyboardButton(text="🚫 Плохие слова", callback_data="bad_words_menu"),
        ],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_settings")],
    ])


def build_warn_actions_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔇 Мут", callback_data=f"action_mute_{chat_id}_{user_id}"),
            InlineKeyboardButton(text="👢 Кик", callback_data=f"action_kick_{chat_id}_{user_id}"),
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"action_ban_{chat_id}_{user_id}"),
        ],
        [InlineKeyboardButton(text="↩️ Снять варн", callback_data=f"action_unwarn_{chat_id}_{user_id}")],
    ])


async def log_action(chat_id: int, text: str):
    """Отправить лог в канал если настроен"""
    settings = await db.get_group(chat_id)
    if settings.log_channel:
        try:
            await bot.send_message(settings.log_channel, f"📋 <b>Лог</b>\n{text}")
        except Exception:
            pass


async def mute_user(chat_id: int, user_id: int, duration: int = None):
    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None
    await bot.restrict_chat_member(
        chat_id, user_id,
        permissions=ChatPermissions(
            can_send_messages=False,
            can_send_media_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
        ),
        until_date=until
    )


async def unmute_user(chat_id: int, user_id: int):
    await bot.restrict_chat_member(
        chat_id, user_id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
    )


# ─── START / HELP ──────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type == "private":
        text = (
            "👋 <b>Привет! Я крутой бот для управления чатами.</b>\n\n"
            "📋 <b>Что я умею:</b>\n"
            "• Welcome / Goodbye сообщения\n"
            "• Система предупреждений (варны)\n"
            "• Антифлуд, антиспам, антилинк\n"
            "• Заметки и правила чата\n"
            "• Капча для новых участников\n"
            "• Бан плохих слов\n"
            "• Мут, кик, бан пользователей\n"
            "• Логирование действий\n\n"
            "➕ <b>Добавь меня в группу и дай права администратора!</b>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить в группу", url=f"https://t.me/{(await bot.get_me()).username}?startgroup=true")],
            [InlineKeyboardButton(text="📚 Помощь", callback_data="help_main")],
        ])
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(
            f"👋 Привет, {mention(message.from_user)}! Я здесь, чтобы помочь с управлением чатом.\n"
            f"Напиши /help для списка команд."
        )


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📚 <b>Список команд:</b>\n\n"
        "<b>👮 Модерация:</b>\n"
        "/ban - Забанить пользователя\n"
        "/unban - Разбанить\n"
        "/kick - Кикнуть\n"
        "/mute [время] - Замутить (пример: /mute 1h)\n"
        "/unmute - Размутить\n"
        "/warn [причина] - Предупредить\n"
        "/unwarn - Снять предупреждение\n"
        "/warns - Список предупреждений\n"
        "/resetwarns - Сбросить все варны\n\n"
        "<b>📝 Информация:</b>\n"
        "/rules - Правила чата\n"
        "/note <имя> - Показать заметку\n"
        "/notes - Список заметок\n"
        "/savenote <имя> - Сохранить заметку\n"
        "/delnote <имя> - Удалить заметку\n\n"
        "<b>⚙️ Настройки (только в ЛС):</b>\n"
        "/settings - Настройки группы\n"
        "/setwelcome - Установить приветствие\n"
        "/setrules - Установить правила\n\n"
        "<b>🔧 Утилиты:</b>\n"
        "/info - Информация о пользователе\n"
        "/pin - Закрепить сообщение\n"
        "/unpin - Открепить\n"
        "/ro - Режим только чтение\n"
        "/cleanbot - Удалить сообщения ботов\n"
        "/id - Узнать ID\n"
    )
    await message.answer(text)


# ─── НАСТРОЙКИ ─────────────────────────────────────────────

@router.message(Command("settings"))
@admin_only
async def cmd_settings(message: Message):
    if message.chat.type != "private":
        # В чате — отправляем ссылку в ЛС
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⚙️ Открыть настройки",
                url=f"https://t.me/{(await bot.get_me()).username}?start=settings_{message.chat.id}"
            )]
        ])
        await message.reply("⚙️ Настройки доступны в личных сообщениях:", reply_markup=kb)
        return

    # В ЛС — показываем настройки
    # Получаем chat_id из команды (если передан через deep link)
    args = message.text.split()
    chat_id = None

    if len(args) > 1 and args[1].startswith("settings_"):
        try:
            chat_id = int(args[1].split("_")[1])
        except (ValueError, IndexError):
            pass

    if not chat_id:
        await message.answer("ℹ️ Используй /settings в чате, чтобы получить ссылку на настройки.")
        return

    # Проверяем, что пользователь — админ этого чата
    if not await is_admin(bot, chat_id, message.from_user.id):
        await message.answer("❌ Ты не являешься администратором этого чата!")
        return

    settings = await db.get_group(chat_id)
    chat = await bot.get_chat(chat_id)

    text = f"⚙️ <b>Настройки чата: {chat.title}</b>\n\nВыбери параметр для изменения:"
    await message.answer(text, reply_markup=build_settings_keyboard(settings))


@router.callback_query(F.data.startswith("toggle_"))
async def toggle_setting(callback: CallbackQuery):
    # Получаем настройки из текущего сообщения
    # Для простоты — храним chat_id в callback data
    action = callback.data.replace("toggle_", "")

    # Определяем chat_id (нужен из контекста)
    # В реальном проекте — можно хранить в FSM или в callback_data
    # Здесь упрощённый вариант: пользователь должен был открыть через /settings в чате
    msg_text = callback.message.text or ""

    import re
    chat_match = re.search(r"chat_id:(\-?\d+)", callback.message.text or "")

    # Более надёжный способ — искать в тексте
    if callback.message.text and "chat_id:" in callback.message.text:
        chat_id = int(re.search(r"chat_id:(\-?\d+)", callback.message.text).group(1))
    else:
        await callback.answer("❌ Ошибка: не найден чат", show_alert=True)
        return

    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("❌ Ты не администратор!", show_alert=True)
        return

    settings = await db.get_group(chat_id)

    toggle_map = {
        "welcome": "welcome_enabled",
        "goodbye": "goodbye_enabled",
        "antiflood": "antiflood",
        "antilink": "antilink",
        "antispam": "antispam",
        "antiarab": "antiarab",
        "captcha": "captcha_enabled",
        "ro": "ro_mode",
    }

    field = toggle_map.get(action)
    if field:
        current = getattr(settings, field)
        await db.update_group(chat_id, **{field: not current})
        settings = await db.get_group(chat_id)
        chat = await bot.get_chat(chat_id)
        await callback.message.edit_text(
            f"⚙️ <b>Настройки чата: {chat.title}</b>\nchat_id:{chat_id}\n\nВыбери параметр:",
            reply_markup=build_settings_keyboard(settings)
        )
        await callback.answer(f"{'✅ Включено' if not current else '❌ Выключено'}")


@router.callback_query(F.data == "close_settings")
async def close_settings(callback: CallbackQuery):
    await callback.message.delete()


@router.callback_query(F.data == "help_main")
async def help_callback(callback: CallbackQuery):
    await cmd_help(callback.message)
    await callback.answer()


# ─── WELCOME / GOODBYE ─────────────────────────────────────

@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated):
    settings = await db.get_group(event.chat.id)
    user = event.new_chat_member.user

    # Капча
    if settings.captcha_enabled:
        await handle_captcha(event.chat.id, user)
        return

    if settings.welcome_enabled:
        # Заменяем плейсхолдеры
        text = settings.welcome_text or "👋 Добро пожаловать, {mention}!\nТы {count}-й участник нашего чата."
        chat = await bot.get_chat(event.chat.id)

        text = text.replace("{mention}", mention(user))
        text = text.replace("{name}", user.full_name or user.first_name)
        text = text.replace("{username}", f"@{user.username}" if user.username else user.first_name)
        text = text.replace("{chat}", chat.title)
        text = text.replace("{count}", str(chat.member_count or "?"))
        text = text.replace("{id}", str(user.id))

        kb = None
        if settings.rules:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Правила чата", callback_data=f"show_rules_{event.chat.id}")]
            ])

        msg = await bot.send_message(event.chat.id, text, reply_markup=kb)

        # Автоудаление через 5 минут
        await asyncio.sleep(300)
        try:
            await msg.delete()
        except Exception:
            pass


@router.callback_query(F.data.startswith("show_rules_"))
async def show_rules_callback(callback: CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    settings = await db.get_group(chat_id)
    if settings.rules:
        await callback.answer(settings.rules[:200], show_alert=True)
    else:
        await callback.answer("📜 Правила не установлены", show_alert=True)


async def handle_captcha(chat_id: int, user):
    """Простая математическая капча"""
    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    answer = str(num1 + num2)
    wrong_answers = [str(random.randint(1, 20)) for _ in range(3)]
    wrong_answers = [x for x in wrong_answers if x != answer][:3]

    all_answers = [answer] + wrong_answers
    random.shuffle(all_answers)

    # Мутим пользователя до прохождения капчи
    await mute_user(chat_id, user.id)

    buttons = [[InlineKeyboardButton(text=a, callback_data=f"captcha_{user.id}_{a}_{answer}")]
               for a in all_answers]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    settings = await db.get_group(chat_id)
    timeout = settings.captcha_timeout

    msg = await bot.send_message(
        chat_id,
        f"🤖 {mention(user)}, реши пример для подтверждения:\n\n"
        f"<b>{num1} + {num2} = ?</b>\n\n"
        f"⏱ У тебя {timeout} секунд.",
        reply_markup=kb
    )

    # Сохраняем в БД
    async with db.async_session() as session:
        pending = db.CaptchaPending(
            chat_id=chat_id, user_id=user.id,
            message_id=msg.message_id, answer=answer
        )
        session.add(pending)
        await session.commit()

    # Таймаут
    await asyncio.sleep(timeout)
    # Проверяем, не прошёл ли уже
    async with db.async_session() as session:
        from sqlalchemy.future import select
        result = await session.execute(
            select(db.CaptchaPending).where(
                db.CaptchaPending.chat_id == chat_id,
                db.CaptchaPending.user_id == user.id
            )
        )
        still_pending = result.scalar_one_or_none()

    if still_pending:
        try:
            await bot.kick_chat_member(chat_id, user.id)
            await bot.unban_chat_member(chat_id, user.id)
            await msg.delete()
        except Exception:
            pass


@router.callback_query(F.data.startswith("captcha_"))
async def captcha_answer(callback: CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[1])
    given = parts[2]
    correct = parts[3]

    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не твоя капча!", show_alert=True)
        return

    if given == correct:
        await unmute_user(callback.message.chat.id, user_id)
        await callback.message.edit_text(f"✅ {mention(callback.from_user)}, капча пройдена! Добро пожаловать!")

        # Удаляем из pending
        async with db.async_session() as session:
            from sqlalchemy import delete
            await session.execute(
                delete(db.CaptchaPending).where(
                    db.CaptchaPending.chat_id == callback.message.chat.id,
                    db.CaptchaPending.user_id == user_id
                )
            )
            await session.commit()
    else:
        await callback.answer("❌ Неверно! Попробуй ещё раз.", show_alert=True)


# ─── МОДЕРАЦИЯ ─────────────────────────────────────────────

@router.message(Command("ban"))
@admin_only
@group_only
async def cmd_ban(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя (реплай или @username)!")
        return

    if await is_admin(bot, message.chat.id, target.id):
        await message.reply("❌ Нельзя забанить администратора!")
        return

    args = message.text.split(maxsplit=2)
    reason = args[2] if len(args) > 2 else "Без причины"

    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(
            f"🚫 {mention(target)} забанен!\n"
            f"👮 Администратор: {mention(message.from_user)}\n"
            f"📝 Причина: {reason}"
        )
        await log_action(
            message.chat.id,
            f"🚫 Бан\nПользователь: {mention(target)} ({target.id})\n"
            f"Админ: {mention(message.from_user)}\nПричина: {reason}"
        )
        try:
            await message.delete()
        except Exception:
            pass
    except Exception as e:
        await message.reply(f"❌ Не удалось забанить: {e}")


@router.message(Command("unban"))
@admin_only
@group_only
async def cmd_unban(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {mention(target)} разбанен!")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("kick"))
@admin_only
@group_only
async def cmd_kick(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return

    if await is_admin(bot, message.chat.id, target.id):
        await message.reply("❌ Нельзя кикнуть администратора!")
        return

    args = message.text.split(maxsplit=2)
    reason = args[2] if len(args) > 2 else "Без причины"

    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(
            f"👢 {mention(target)} кикнут!\n"
            f"👮 Администратор: {mention(message.from_user)}\n"
            f"📝 Причина: {reason}"
        )
        await log_action(message.chat.id, f"👢 Кик\nПользователь: {mention(target)}\nПричина: {reason}")
        try:
            await message.delete()
        except Exception:
            pass
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("mute"))
@admin_only
@group_only
async def cmd_mute(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return

    if await is_admin(bot, message.chat.id, target.id):
        await message.reply("❌ Нельзя замутить администратора!")
        return

    args = message.text.split()
    duration = None
    reason = "Без причины"

    if len(args) > 2:
        duration = parse_time(args[2])
        if duration:
            reason = " ".join(args[3:]) if len(args) > 3 else "Без причины"
        else:
            reason = " ".join(args[2:])

    if len(args) == 2 and len(args[1:]) > 0:
        duration = parse_time(args[1]) if len(args) > 1 else None

    try:
        await mute_user(message.chat.id, target.id, duration)
        time_text = f" на {args[2]}" if duration else " навсегда"
        await message.reply(
            f"🔇 {mention(target)} замучен{time_text}!\n"
            f"👮 Администратор: {mention(message.from_user)}\n"
            f"📝 Причина: {reason}"
        )
        await log_action(
            message.chat.id,
            f"🔇 Мут\nПользователь: {mention(target)}\nВремя: {time_text}\nПричина: {reason}"
        )
        try:
            await message.delete()
        except Exception:
            pass
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("unmute"))
@admin_only
@group_only
async def cmd_unmute(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return
    try:
        await unmute_user(message.chat.id, target.id)
        await message.reply(f"✅ {mention(target)} размучен!")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ─── ВАРНЫ ─────────────────────────────────────────────────

@router.message(Command("warn"))
@admin_only
@group_only
async def cmd_warn(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return

    if await is_admin(bot, message.chat.id, target.id):
        await message.reply("❌ Нельзя предупредить администратора!")
        return

    args = message.text.split(maxsplit=2)
    reason = args[2] if len(args) > 2 else "Без причины"

    settings = await db.get_group(message.chat.id)
    warn_count = await db.add_warn(message.chat.id, target.id, reason, message.from_user.id)

    if warn_count >= settings.warn_limit:
        # Применяем действие
        action_text = ""
        try:
            if settings.warn_action == "ban":
                await bot.ban_chat_member(message.chat.id, target.id)
                action_text = "🚫 Пользователь забанен!"
            elif settings.warn_action == "kick":
                await bot.ban_chat_member(message.chat.id, target.id)
                await asyncio.sleep(1)
                await bot.unban_chat_member(message.chat.id, target.id)
                action_text = "👢 Пользователь кикнут!"
            elif settings.warn_action == "mute":
                await mute_user(message.chat.id, target.id, settings.warn_mute_time)
                action_text = f"🔇 Пользователь замучен на {settings.warn_mute_time // 60} мин!"
        except Exception as e:
            action_text = f"❌ Не удалось применить действие: {e}"

        await db.reset_warns(message.chat.id, target.id)
        await message.reply(
            f"⚠️ {mention(target)} получил предупреждение {warn_count}/{settings.warn_limit}!\n"
            f"📝 Причина: {reason}\n\n{action_text}"
        )
    else:
        keyboard = build_warn_actions_keyboard(message.chat.id, target.id)
        await message.reply(
            f"⚠️ {mention(target)} получил предупреждение {warn_count}/{settings.warn_limit}!\n"
            f"📝 Причина: {reason}",
            reply_markup=keyboard
        )

    await log_action(
        message.chat.id,
        f"⚠️ Предупреждение {warn_count}/{settings.warn_limit}\n"
        f"Пользователь: {mention(target)}\nПричина: {reason}"
    )


@router.callback_query(F.data.startswith("action_"))
async def warn_action_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    chat_id = int(parts[2])
    user_id = int(parts[3])

    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("❌ Только для администраторов!", show_alert=True)
        return

    try:
        target = await bot.get_chat_member(chat_id, user_id)
        user = target.user
    except Exception:
        await callback.answer("❌ Пользователь не найден!", show_alert=True)
        return

    if action == "ban":
        await bot.ban_chat_member(chat_id, user_id)
        await callback.message.edit_text(f"🚫 {mention(user)} забанен администратором {mention(callback.from_user)}")
    elif action == "kick":
        await bot.ban_chat_member(chat_id, user_id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(chat_id, user_id)
        await callback.message.edit_text(f"👢 {mention(user)} кикнут администратором {mention(callback.from_user)}")
    elif action == "mute":
        await mute_user(chat_id, user_id, 3600)
        await callback.message.edit_text(f"🔇 {mention(user)} замучен на 1 час администратором {mention(callback.from_user)}")
    elif action == "unwarn":
        removed = await db.remove_warn(chat_id, user_id)
        if removed:
            await callback.answer("✅ Предупреждение снято!")
        else:
            await callback.answer("❌ Предупреждений нет!", show_alert=True)

    await callback.answer()


@router.message(Command("warns"))
@group_only
async def cmd_warns(message: Message):
    target = await get_target_user(message) or message.from_user
    warns = await db.get_warns(message.chat.id, target.id)
    settings = await db.get_group(message.chat.id)

    if not warns:
        await message.reply(f"✅ У {mention(target)} нет предупреждений!")
        return

    text = f"⚠️ <b>Предупреждения {mention(target)}: {len(warns)}/{settings.warn_limit}</b>\n\n"
    for i, w in enumerate(warns, 1):
        text += f"{i}. {w.reason or 'Без причины'} — {w.created_at.strftime('%d.%m.%Y %H:%M')}\n"

    await message.reply(text)


@router.message(Command("unwarn"))
@admin_only
@group_only
async def cmd_unwarn(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return
    removed = await db.remove_warn(message.chat.id, target.id)
    if removed:
        await message.reply(f"✅ Последнее предупреждение у {mention(target)} снято!")
    else:
        await message.reply(f"ℹ️ У {mention(target)} нет предупреждений!")


@router.message(Command("resetwarns"))
@admin_only
@group_only
async def cmd_resetwarns(message: Message):
    target = await get_target_user(message)
    if not target:
        await message.reply("❌ Укажи пользователя!")
        return
    await db.reset_warns(message.chat.id, target.id)
    await message.reply(f"✅ Все предупреждения {mention(target)} сброшены!")


# ─── ЗАМЕТКИ ───────────────────────────────────────────────

@router.message(Command("note"))
@group_only
async def cmd_note(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /note <имя>")
        return

    note = await db.get_note(message.chat.id, args[1])
    if note:
        await message.reply(f"📝 <b>{note.name}</b>\n\n{note.content}")
    else:
        await message.reply(f"❌ Заметка <b>{args[1]}</b> не найдена!")


@router.message(Command("notes"))
@group_only
async def cmd_notes(message: Message):
    notes = await db.get_all_notes(message.chat.id)
    if not notes:
        await message.reply("📝 Заметок нет. Создай с помощью /savenote")
        return

    text = "📝 <b>Список заметок:</b>\n\n"
    for note in notes:
        text += f"• <code>{note.name}</code>\n"
    text += "\nИспользуй /note <имя> для просмотра"
    await message.reply(text)


@router.message(Command("savenote"))
@admin_only
@group_only
async def cmd_savenote(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /savenote <имя>\n(отправь содержимое следующим сообщением)")
        return

    await state.update_data(note_name=args[1], chat_id=message.chat.id)
    await state.set_state(SettingsState.waiting_note_content)
    await message.reply(f"📝 Отправь содержимое заметки <b>{args[1]}</b>:")


@router.message(SettingsState.waiting_note_content)
async def save_note_content(message: Message, state: FSMContext):
    data = await state.get_data()
    await db.save_note(data["chat_id"], data["note_name"], message.text, message.from_user.id)
    await state.clear()
    await message.reply(f"✅ Заметка <b>{data['note_name']}</b> сохранена!")


@router.message(Command("delnote"))
@admin_only
@group_only
async def cmd_delnote(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /delnote <имя>")
        return
    deleted = await db.delete_note(message.chat.id, args[1])
    if deleted:
        await message.reply(f"✅ Заметка <b>{args[1]}</b> удалена!")
    else:
        await message.reply(f"❌ Заметка <b>{args[1]}</b> не найдена!")


# ─── ПРАВИЛА ───────────────────────────────────────────────

@router.message(Command("rules"))
async def cmd_rules(message: Message):
    settings = await db.get_group(message.chat.id)
    if settings.rules:
        await message.reply(f"📜 <b>Правила чата:</b>\n\n{settings.rules}")
    else:
        await message.reply("📜 Правила не установлены. Используй /setrules (в ЛС через /settings)")


@router.message(Command("setrules"))
@admin_only
async def cmd_setrules(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        chat_id = message.chat.id if message.chat.type != "private" else None
        if chat_id:
            await db.update_group(chat_id, rules=args[1])
            await message.reply("✅ Правила установлены!")
    else:
        await state.set_state(SettingsState.waiting_rules)
        await message.reply("📜 Отправь текст правил:")


@router.message(SettingsState.waiting_rules)
async def save_rules(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("chat_id")
    if chat_id:
        await db.update_group(chat_id, rules=message.text)
        await state.clear()
        await message.reply("✅ Правила сохранены!")
    else:
        await state.clear()
        await message.reply("❌ Ошибка: не указан чат")


# ─── УТИЛИТЫ ───────────────────────────────────────────────

@router.message(Command("info"))
async def cmd_info(message: Message):
    target = await get_target_user(message) or message.from_user

    try:
        member = await bot.get_chat_member(message.chat.id, target.id)
        status_map = {
            "creator": "👑 Владелец",
            "administrator": "⭐ Администратор",
            "member": "👤 Участник",
            "restricted": "🔇 Ограничен",
            "left": "🚪 Покинул",
            "kicked": "🚫 Заблокирован"
        }
        status = status_map.get(member.status, member.status)
    except Exception:
        status = "❓ Неизвестно"

    text = (
        f"👤 <b>Информация о пользователе</b>\n\n"
        f"📛 Имя: {target.full_name}\n"
        f"🆔 ID: <code>{target.id}</code>\n"
        f"📱 Username: @{target.username}\n" if target.username else
        f"📛 Имя: {target.full_name}\n"
        f"🆔 ID: <code>{target.id}</code>\n"
        f"👑 Статус: {status}\n"
    )
    # Warns
    warns = await db.get_warns(message.chat.id, target.id)
    settings = await db.get_group(message.chat.id)
    text += f"⚠️ Варны: {len(warns)}/{settings.warn_limit}"

    await message.reply(text)


@router.message(Command("id"))
async def cmd_id(message: Message):
    if message.reply_to_message:
        user = message.reply_to_message.from_user
        await message.reply(f"🆔 ID {mention(user)}: <code>{user.id}</code>")
    else:
        await message.reply(
            f"🆔 Твой ID: <code>{message.from_user.id}</code>\n"
            f"💬 ID чата: <code>{message.chat.id}</code>"
        )


@router.message(Command("pin"))
@admin_only
@group_only
async def cmd_pin(message: Message):
    if not message.reply_to_message:
        await message.reply("❌ Ответь на сообщение, которое нужно закрепить!")
        return
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.reply("📌 Сообщение закреплено!")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("unpin"))
@admin_only
@group_only
async def cmd_unpin(message: Message):
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.reply("📌 Сообщение откреплено!")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("ro"))
@admin_only
@group_only
async def cmd_ro(message: Message):
    settings = await db.get_group(message.chat.id)
    new_state = not settings.ro_mode
    await db.update_group(message.chat.id, ro_mode=new_state)

    if new_state:
        await bot.set_chat_permissions(
            message.chat.id,
            ChatPermissions(can_send_messages=False)
        )
        await message.reply("🔕 Режим только чтение включён! Никто не может писать.")
    else:
        await bot.set_chat_permissions(
            message.chat.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
            )
        )
        await message.reply("✅ Режим только чтение выключен!")


@router.message(Command("cleanbot"))
@admin_only
@group_only
async def cmd_cleanbot(message: Message):
    # Удаляем команды ботов за последние N сообщений
    await message.reply("🧹 Очистка сообщений ботов... (функция в разработке)")


# ─── АВТОМОДЕРАЦИЯ ─────────────────────────────────────────

@router.message(F.text)
async def auto_moderation(message: Message):
    if not message.from_user:
        return
    if message.chat.type == "private":
        return

    # Пропускаем админов
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return

    settings = await db.get_group(message.chat.id)

    # Только чтение
    if settings.ro_mode:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Антифлуд
    if settings.antiflood:
        now = datetime.utcnow()
        chat_flood = flood_storage.setdefault(message.chat.id, {})
        user_times = chat_flood.setdefault(message.from_user.id, [])

        # Чистим старые записи
        user_times = [t for t in user_times if (now - t).seconds < settings.antiflood_time]
        user_times.append(now)
        chat_flood[message.from_user.id] = user_times

        if len(user_times) > settings.antiflood_limit:
            try:
                await message.delete()
            except Exception:
                pass

            action = settings.antiflood_action
            if action == "mute":
                await mute_user(message.chat.id, message.from_user.id, 300)
                await message.answer(f"🔇 {mention(message.from_user)}, не флуди! Мут на 5 минут.")
            elif action == "kick":
                await bot.ban_chat_member(message.chat.id, message.from_user.id)
                await asyncio.sleep(1)
                await bot.unban_chat_member(message.chat.id, message.from_user.id)
                await message.answer(f"👢 {mention(message.from_user)} кикнут за флуд!")
            elif action == "ban":
                await bot.ban_chat_member(message.chat.id, message.from_user.id)
                await message.answer(f"🚫 {mention(message.from_user)} забанен за флуд!")

            flood_storage[message.chat.id][message.from_user.id] = []
            return

    # Антилинк
    if settings.antilink and contains_link(message.text or ""):
        try:
            await message.delete()
        except Exception:
            pass
        action = settings.antilink_action
        if action == "warn":
            warn_count = await db.add_warn(
                message.chat.id, message.from_user.id,
                "Отправка ссылок", bot.id
            )
            await message.answer(f"🔗 {mention(message.from_user)}, ссылки запрещены! Предупреждение {warn_count}.")
        elif action == "mute":
            await mute_user(message.chat.id, message.from_user.id, 600)
            await message.answer(f"🔗 {mention(message.from_user)}, ссылки запрещены! Мут на 10 минут.")
        else:
            await message.answer(f"🔗 {mention(message.from_user)}, ссылки запрещены!")
        return

    # Антиарабский
    if settings.antiarab and contains_arabic(message.text or ""):
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(f"🌍 {mention(message.from_user)}, сообщения на арабском запрещены!")
        return

    # Плохие слова
    bad_words = await db.get_bad_words(message.chat.id)
    msg_lower = (message.text or "").lower()
    for bw in bad_words:
        if bw.word in msg_lower:
            try:
                await message.delete()
            except Exception:
                pass
            if bw.action == "warn":
                warn_count = await db.add_warn(
                    message.chat.id, message.from_user.id,
                    f"Запрещённое слово: {bw.word}", 0
                )
                await message.answer(
                    f"🤬 {mention(message.from_user)}, запрещённые слова! Предупреждение {warn_count}."
                )
            elif bw.action == "mute":
                await mute_user(message.chat.id, message.from_user.id, 300)
                await message.answer(f"🤬 {mention(message.from_user)}, запрещённые слова! Мут на 5 минут.")
            else:
                await message.answer(f"🤬 {mention(message.from_user)}, запрещённые слова в чате!")
            return


# ─── КОМАНДЫ ПЛОХИХ СЛОВ ───────────────────────────────────

@router.message(Command("addword"))
@admin_only
@group_only
async def cmd_addword(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.reply("❌ Использование: /addword <слово> [действие: delete/warn/mute]")
        return

    word = args[1].lower()
    action = args[2] if len(args) > 2 and args[2] in ["delete", "warn", "mute"] else "delete"
    await db.add_bad_word(message.chat.id, word, action)
    await message.reply(f"✅ Слово <b>{word}</b> добавлено в фильтр (действие: {action})")


@router.message(Command("delword"))
@admin_only
@group_only
async def cmd_delword(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /delword <слово>")
        return
    removed = await db.remove_bad_word(message.chat.id, args[1].lower())
    if removed:
        await message.reply(f"✅ Слово <b>{args[1]}</b> удалено из фильтра!")
    else:
        await message.reply(f"❌ Слово <b>{args[1]}</b> не найдено!")


@router.message(Command("words"))
@group_only
async def cmd_words(message: Message):
    words = await db.get_bad_words(message.chat.id)
    if not words:
        await message.reply("📋 Список запрещённых слов пуст.")
        return
    text = "🚫 <b>Запрещённые слова:</b>\n\n"
    for w in words:
        text += f"• <code>{w.word}</code> → {w.action}\n"
    await message.reply(text)


# ─── ЗАПУСК ────────────────────────────────────────────────

async def main():
    await db.init_db()
    dp.include_router(router)

    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
