import base64
import io
import json
import os
import random
import sys
import zipfile
from collections import deque
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

# Премиум-эмодзи (кастомные). Ключ — юникод-плейсхолдер, значение — custom_emoji_id.
PREMIUM_EMOJI = {
    "🔥": "5454182246250474905",  # огонь
    "💬": "5453952087543015968",  # сообщения
    "💵": "5456343997779831665",  # деньги
    "💲": "5456122837028857707",  # доллар
    "⚡": "5458746443571421160",  # молния
    "➕": "5456360443209609671",  # плюс (валид)
    "➖": "5456173826880593418",  # минус (ошибка)
    "🪐": "5454102570312166471",  # планета
    "❗": "5453960484204083655",  # восклицательный знак
    "🔗": "5454068128969417666",  # ссылка (цепь)
    "🖥️": "5454209184285356042",  # сайт
    "💰": "5454134554933619492",  # сундук с монетами
    "🔽": "5454172127307523969",  # стрелка вниз
    "🔒": "5453904641039298473",  # замок
    "👌": "5458604082585427959",  # ОК
    "🖤": "5456557633748100084",  # пылающее сердце
}


# Замена обычных эмодзи на премиум-плейсхолдеры в тексте сообщений.
EMOJI_MAP = {
    "🎫": "💵",  # токен → деньги
    "✅": "➕",  # встал/валид → плюс
    "⚠️": "➖",  # ошибка → минус
    "🎉": "🔥",  # праздник/огонь
    "🪙": "💲",  # монета → доллар
    "📦": "💰",  # архив/сундук с монетами
    "🔓": "➖",  # отвязка → минус
    "📭": "➖",  # нет файлов → минус
    "✨": "🔥",  # искра → огонь
    "📈": "💬",  # график → сообщения
    "👇": "🔽",  # палец вниз → стрелка вниз
}


def fmt(text: str) -> str:
    """Заменяет обычные эмодзи на премиум и оборачивает их в <tg-emoji>.

    Возвращает HTML-строку, готовую для отправки с parse_mode='HTML'.
    """
    for old, new in EMOJI_MAP.items():
        text = text.replace(old, new)
    # Оборачиваем каждый премиум-плейсхолдер в тег <tg-emoji emoji-id="...">.
    for char, custom_id in PREMIUM_EMOJI.items():
        tag = f'<tg-emoji emoji-id="{custom_id}">{char}</tg-emoji>'
        text = text.replace(char, tag)
    return text


def fmt_html(text: str) -> str:
    """Алиас fmt: возвращает HTML для сообщений."""
    return fmt(text)


# Укажите токен через переменную окружения BOT_TOKEN.
BOT_TOKEN = "9228778536:AAF9CQJoul0fPULpQYDN8ctf0Qi_afdONho"
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите токен бота в переменной окружения."
    )

# ID единственного администратора.
ADMIN_ID = 8708484648

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

TOKEN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Получить токен",
                callback_data="get_token",
                style="primary",
                icon_custom_emoji_id="5456343997779831665",
            )
        ],
        [
            InlineKeyboardButton(
                text="Статистика",
                callback_data="menu_stats",
                style="primary",
                icon_custom_emoji_id="5453952087543015968",
            )
        ],
    ]
)


# chat_id -> message_id последнего отправленного меню (чтобы удалять старое перед новым).
_last_menu: dict[int, int] = {}


class AdminStates(StatesGroup):
    waiting_reset_chat = State()
    waiting_upload_pool = State()
    waiting_add_moderator = State()
    waiting_mod_chat_id = State()
    waiting_remove_moderator = State()


async def send_menu(chat_id: int, extra_text: str = "") -> None:
    """Удаляет предыдущее меню в чате (если есть) и отправляет новое.

    Вызывается из асинхронных обработчиков через await.
    """
    prev_id = _last_menu.pop(chat_id, None)
    if prev_id is not None:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception:
            pass

    html_menu = fmt(MENU_TEXT + extra_text)
    try:
        sent = await bot.send_message(
            chat_id,
            html_menu,
            parse_mode="HTML",
            reply_markup=TOKEN_MENU,
        )
        _last_menu[chat_id] = sent.message_id
    except Exception as exc:
        print(f"send_menu error: {exc}")

# Текст, сопровождающий меню токенов.
MENU_TEXT = (
    "✨ <b>Чтобы взять токен</b> — нажми кнопку ниже 👇\n"
    "📈 <b>Чтобы посмотреть актуальную статистику</b> — нажми «Статистика»."
)

MARK_UP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Встал", callback_data="stood", style="success",
                icon_custom_emoji_id="5456360443209609671",
            ),
            InlineKeyboardButton(
                text="Ошибка", callback_data="error", style="danger",
                icon_custom_emoji_id="5456173826880593418",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Посмотреть токен", callback_data="view_token",
                style="primary",
                icon_custom_emoji_id="5453952087543015968",
            ),
        ],
    ]
)

# Общий пул файлов: (имя файла, содержимое).
chat_pools: deque[tuple[str, bytes]] = deque()

# Чаты, привязанные командой /connect.
connected_chats: set[int] = set()

# Статистика: chat_id -> {date, tokens, stood, error, users}.
chat_stats: dict[int, dict] = {}

# Лимит выданных токенов на чат за день: chat_id -> int (0 или отсутствие = без лимита).
chat_limits: dict[int, int] = {}

# Модераторы: (chat_id, user_id) -> True. Могут использовать бота в назначенном чате.
moderators: dict[tuple[int, int], bool] = {}

# (chat_id, message_id) -> user_id.
token_owner: dict[tuple[int, int], int] = {}

# (chat_id, message_id) -> (имя файла, содержимое) для кнопки «Посмотреть токен».
token_content: dict[tuple[int, int], tuple[str, bytes]] = {}

# Уже обработанные «chat_id:menu_message_id» (защита от дублей и повторной доставки колбэка).
_processed_callbacks: set[str] = set()

STATE_FILE = Path(__file__).resolve().with_name("bot_state.json")


def get_stats(chat_id: int, today: date) -> dict:
    """Возвращает статистику за день, создавая её при необходимости."""
    st = chat_stats.get(chat_id)
    if st is None or st.get("date") != today:
        st = {
            "date": today,
            "tokens": 0,
            "stood": 0,
            "error": 0,
            "users": {},
        }
        chat_stats[chat_id] = st
    return st


def save_state() -> None:
    """Сохраняет пул, привязки, статистику и ожидающие отметки."""
    stats_data = {}
    for chat_id, st in chat_stats.items():
        stats_data[str(chat_id)] = {
            "date": st["date"].isoformat(),
            "tokens": int(st.get("tokens", 0)),
            "stood": int(st.get("stood", 0)),
            "error": int(st.get("error", 0)),
            "users": {
                str(uid): {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                }
                for uid, user in st.get("users", {}).items()
            },
        }

    data = {
        "connected": sorted(connected_chats),
        "pool": [
            [name, base64.b64encode(blob).decode("ascii")]
            for name, blob in chat_pools
        ],
        "stats": stats_data,
        "limits": {str(k): int(v) for k, v in chat_limits.items()},
        "moderators": [
            [chat_id, uid] for (chat_id, uid) in moderators.keys()
        ],
        "token_owner": [
            [chat_id, message_id, uid]
            for (chat_id, message_id), uid in token_owner.items()
        ],
    }

    temp_file = STATE_FILE.with_suffix(".tmp")
    try:
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_file.replace(STATE_FILE)
    except Exception as exc:
        print(f"save_state error: {exc}")
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass


def load_state() -> None:
    """Загружает сохранённое состояние; поддерживает старый формат файла."""
    if not STATE_FILE.exists():
        return

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        connected_chats.clear()
        connected_chats.update(int(chat_id) for chat_id in data.get("connected", []))

        chat_pools.clear()
        for name, encoded_blob in data.get("pool", []):
            chat_pools.append((str(name), base64.b64decode(encoded_blob)))

        chat_stats.clear()
        for chat_id, raw in data.get("stats", {}).items():
            users = {}
            for uid, user in raw.get("users", {}).items():
                users[int(uid)] = {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                }

            chat_stats[int(chat_id)] = {
                "date": date.fromisoformat(raw["date"]),
                "tokens": int(raw.get("tokens", 0)),
                "stood": int(raw.get("stood", 0)),
                "error": int(raw.get("error", 0)),
                "users": users,
            }

        token_owner.clear()
        for chat_id, message_id, uid in data.get("token_owner", []):
            token_owner[(int(chat_id), int(message_id))] = int(uid)

        chat_limits.clear()
        for chat_id, limit in data.get("limits", {}).items():
            chat_limits[int(chat_id)] = int(limit)

        moderators.clear()
        for chat_id, uid in data.get("moderators", []):
            moderators[(int(chat_id), int(uid))] = True

    except Exception as exc:
        print(f"load_state error: {exc}")


load_state()


def extract_txt(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".txt") and not name.endswith("/"):
                files.append((name.rsplit("/", 1)[-1], archive.read(name)))
    return files


async def log_to_admin(text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, fmt(text), parse_mode="HTML")
    except Exception as exc:
        print(f"log_to_admin error: {exc}")


async def issue_token(chat_id: int, uid: int, username: str) -> str:
    """Выдаёт один токен из общего пула в чат и учитывает его в статистике.

    Возвращает статус: "ok", "empty" (пул пуст) или "limit" (достигнут лимит чата).
    """
    if not chat_pools:
        return "empty"

    # Проверка дневного лимита чата.
    limit = chat_limits.get(chat_id)
    if limit:
        st_check = get_stats(chat_id, date.today())
        if st_check["tokens"] >= limit:
            return "limit"

    name, data = chat_pools.popleft()
    caption_text = f"{name}\n➕ Готов к приёму · 💵 токен для @{username}"
    sent = await bot.send_document(
        chat_id,
        types.BufferedInputFile(data, filename=name),
        caption=fmt(caption_text),
        parse_mode="HTML",
        reply_markup=MARK_UP,
    )
    token_owner[(chat_id, sent.message_id)] = uid
    token_content[(chat_id, sent.message_id)] = (name, data)

    st = get_stats(chat_id, date.today())
    st["tokens"] += 1
    user = st["users"].setdefault(
        uid,
        {"name": username, "tokens": 0, "stood": 0, "error": 0},
    )
    user["name"] = username
    user["tokens"] += 1
    save_state()

    # Отправляем лог в фоне, чтобы он не задерживал выдачу следующего токена.
    import asyncio as _asyncio

    async def _log():
        await log_to_admin(
            f"🪙 Токен взят:\nФайл: {name}\nПользователь: @{username} (id {uid})\n"
            f"Чат: {chat_id}\nОстаток в пуле: {len(chat_pools)}"
        )

    _asyncio.get_running_loop().create_task(_log())
    return "ok"


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = "🔥 Привет! 💰 Отправь ZIP-архив с .txt файлами, а затем нажми «💵 Получить токен» в меню бота."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("connect"))
async def cmd_connect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    connected_chats.add(message.chat.id)
    save_state()
    await log_to_admin(f"🔒 Чат привязан: {message.chat.id}")

    text = "🔒 Бот привязан к этому чату.\n\n" + MENU_TEXT
    html = fmt(text)
    sent = await message.answer(html, parse_mode="HTML", reply_markup=TOKEN_MENU)
    _last_menu[message.chat.id] = sent.message_id


@dp.message(Command("unconnect"))
async def cmd_unconnect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    connected_chats.discard(message.chat.id)
    save_state()
    await log_to_admin(f"🔓 Чат отвязан: {message.chat.id}")

    text = "🔓 Бот отвязан от этого чата. Команды бота больше недоступны."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("limit"))
async def cmd_limit(message: types.Message):
    if message.from_user is None or not can_manage(message.from_user.id, message.chat.id):
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    parts = (message.text or "").split()
    chat_id = message.chat.id
    if len(parts) == 1:
        cur = chat_limits.get(chat_id, 0)
        if cur:
            text = f"💬 Текущий лимит токенов в этом чате: <b>{cur}</b> за день."
        else:
            text = "📭 Лимит в этом чате не установлен (без ограничений)."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    try:
        limit = int(parts[1])
    except ValueError:
        text = "➖ Лимит должен быть числом. Пример: /limit 100 или /limit 0 (убрать)."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if limit < 0:
        text = "➖ Лимит не может быть отрицательным."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if limit == 0:
        chat_limits.pop(chat_id, None)
        text = "➖ Лимит снят: токены в этом чате выдаются без ограничений."
    else:
        chat_limits[chat_id] = limit
        text = f"➕ Установлен лимит: <b>{limit}</b> токенов за день в этом чате."

    save_state()
    await log_to_admin(f"🔒 Лимит чата {chat_id} -> {limit if limit else 'без лимита'}")
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


async def is_connected(message: types.Message) -> bool:
    if message.chat.id not in connected_chats:
        text = "➖ Бот не привязан к этому чату. Используй /connect."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return False
    return True


def can_manage(user_id: int, chat_id: int) -> bool:
    """Имеет ли пользователь права управления в чате (админ или модератор)."""
    if user_id == ADMIN_ID:
        return True
    return moderators.get((chat_id, user_id), False)


def is_private(message: types.Message) -> bool:
    return message.chat.type == "private"


@dp.message(F.document)
async def on_document(message: types.Message, state: FSMContext):
    doc = message.document
    if doc is None or not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        # Если это попытка пополнить пул — говорим, что нужен ZIP.
        cur = await state.get_state()
        if cur == AdminStates.waiting_upload_pool.state:
            text = "➖ Отправь ZIP-архив с .txt файлами для пополнения пула. /cancel — отмена."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
        else:
            text = "➖ Пожалуйста, отправь ZIP-архив."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
        return

    file_info = await bot.get_file(doc.file_id)
    destination = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=destination)

    try:
        files = extract_txt(destination.getvalue())
    except (zipfile.BadZipFile, OSError) as exc:
        text = f"➖ Не удалось прочитать ZIP-архив: {exc}"
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not files:
        text = "➖ В архиве нет .txt файлов."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    chat_pools.extend(files)
    save_state()

    # Если файл загружен в рамках пополнения пула — очищаем состояние.
    cur = await state.get_state()
    if cur == AdminStates.waiting_upload_pool.state:
        await state.clear()

    names = ", ".join(name for name, _ in files)
    await log_to_admin(
        f"💰 Загружен архив ({doc.file_name}): +{len(files)} txt, "
        f"всего в пуле {len(chat_pools)}\nФайлы: {names}"
    )
    text = (
        f"➕ Архив обработан. .txt файлов в пуле: {len(chat_pools)}. "
        "Нажми «💵 Получить токен» в меню."
    )
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("token"))
async def cmd_token(message: types.Message):
    if is_private(message):
        text = "❗ Команда /token недоступна в личных сообщениях."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not await is_connected(message):
        return

    username = message.from_user.username or message.from_user.full_name
    status = await issue_token(message.chat.id, message.from_user.id, username)
    if status == "limit":
        lim = chat_limits.get(message.chat.id, 0)
        text = f"🔒 Дневной лимит токенов в этом чате достигнут ({lim}). Попробуй завтра."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        await send_menu(message.chat.id)
        return
    if status == "empty":
        text = "📭 Файлов в пуле нет. Отправь ZIP-архив, чтобы добавить файлы."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not chat_pools:
        text = "🔥 Файлы закончились."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")

    # Показываем меню заново, чтобы кнопка не потерялась в чате.
    await send_menu(message.chat.id)


@dp.callback_query(F.data == "menu_stats")
async def on_menu_stats(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    if chat_id not in connected_chats:
        await callback.answer("Бот не привязан к этому чату.", show_alert=True)
        return

    html = fmt(build_stats_text(chat_id))
    try:
        await callback.message.edit_text(
            html,
            parse_mode="HTML",
            reply_markup=TOKEN_MENU,
        )
    except Exception:
        pass


@dp.callback_query(F.data == "get_token")
async def on_get_token(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    # Ключ дедупликации: связка «чат + id меню-сообщения», которые уже обработаны.
    dedup_key = f"{chat_id}:{callback.message.message_id}"
    if dedup_key in _processed_callbacks:
        await callback.answer()
        return
    _processed_callbacks.add(dedup_key)

    # Сразу подтверждаем колбэк, не дожидаясь долгих операций.
    try:
        await callback.answer()
    except Exception:
        pass

    if chat_id not in connected_chats:
        await callback.answer("Бот не привязан к этому чату.", show_alert=True)
        return

    # Удаляем сообщение-меню.
    original_message_id = callback.message.message_id
    try:
        await bot.delete_message(chat_id, original_message_id)
    except Exception:
        pass

    username = callback.from_user.username or callback.from_user.full_name
    status = await issue_token(chat_id, callback.from_user.id, username)

    if status == "limit":
        lim = chat_limits.get(chat_id, 0)
        text = f"🔒 Дневной лимит токенов в этом чате достигнут ({lim})."
        await send_menu(chat_id, "\n\n" + text)
        return

    if status == "empty":
        # Пул пуст: отправляем меню заново, чтобы не было тупика.
        await send_menu(chat_id, "\n\n📭 Файлы закончились.")
        return

    if not chat_pools:
        text = "🔥 Файлы закончились."
        html = fmt(text)
        await bot.send_message(chat_id, html, parse_mode="HTML")

    # После выдачи снова отправляем меню.
    print(f"[get_token] issuing done, sending menu to {chat_id}, pool={len(chat_pools)}")
    await send_menu(chat_id)


@dp.callback_query(F.data == "view_token")
async def on_view_token(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    entry = token_content.get(key)
    if entry is None:
        await bot.send_message(
            chat_id,
            fmt("📭 Контент токена больше не доступен."),
            parse_mode="HTML",
        )
        return

    name, data = entry
    body = data.decode("utf-8", errors="replace")

    import html as html_mod
    safe = html_mod.escape(body)

    back_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Вернуться к файлу", callback_data="back_to_file",
                    style="primary",
                    icon_custom_emoji_id="5456343997779831665",
                )
            ]
        ]
    )
    viewer = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)
    text = (
        f"✅ <b>Содержимое токена</b> ({name})\n"
        f"для: @{viewer}:\n\n"
        f"<pre>{safe}</pre>"
    )
    # Отправляем JSON отдельным сообщением, НЕ трогая исходный файл.
    try:
        sent = await bot.send_message(
            chat_id,
            fmt(text),
            parse_mode="HTML",
            reply_markup=back_markup,
        )
        # Автоудаление JSON-сообщения через 10 секунд, чтобы не засорять чат.
        import asyncio as _asyncio

        async def _autodelete():
            await _asyncio.sleep(10)
            try:
                await bot.delete_message(chat_id, sent.message_id)
            except Exception:
                pass

        _asyncio.get_running_loop().create_task(_autodelete())
    except Exception:
        pass

    # Меню должно ВСЕГДА оставаться последним сообщением в чате.
    await send_menu(chat_id)


@dp.callback_query(F.data == "back_to_file")
async def on_back_to_file(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    # Удаляем JSON-сообщение; исходный файл остаётся в чате.
    try:
        await bot.delete_message(chat_id, callback.message.message_id)
    except Exception:
        pass

    # Меню должно ВСЕГДА оставаться последним сообщением в чате.
    await send_menu(chat_id)


def build_stats_text(chat_id: int) -> str:
    """Строит текст статистики чата за сегодня (без форматирования эмодзи)."""
    st = get_stats(chat_id, date.today())
    total = st["tokens"]
    if total == 0:
        return "📭 За сегодня токены ещё не выдавались."

    def pct(value: int) -> str:
        return f"{round(value / total * 100)}%"

    ranked = sorted(
        st["users"].values(),
        key=lambda user: user["tokens"],
        reverse=True,
    )
    top_lines = "\n".join(
        f"{index}. @{user['name']} - ({user['tokens']})"
        for index, user in enumerate(ranked, start=1)
    ) or "Пока нет участников."

    return (
        "➕ <b>Проверка успешно завершена!</b>\n"
        "💬 <b>Статистика за сегодня</b>\n\n"
        f"➕ <b>Валид:</b> {st['stood']} ({pct(st['stood'])})\n"
        f"➖ <b>Ошибка:</b> {st['error']} ({pct(st['error'])})\n"
        f"💰 <b>Итого:</b> {total} (100%)\n\n"
        "🔗 <b>ТОП ПОЛЬЗОВАТЕЛЕЙ</b>\n\n"
        f"{top_lines}"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if is_private(message):
        text = "❗ Команда /stats недоступна в личных сообщениях."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not await is_connected(message):
        return

    html = fmt(build_stats_text(message.chat.id))
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("boost"), F.chat.type == "private")
async def cmd_boost_private(message: types.Message):
    """Админская корректировка счётчиков из личного чата с ботом."""
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    parts = (message.text or "").split()
    if len(parts) not in (2, 3):
        await message.answer(
            "Формат команды:\n"
            "/boost <количество> — если подключена одна группа\n"
            "/boost <chat_id> <количество> — если групп несколько"
        )
        return

    if len(parts) == 2:
        if not connected_chats:
            text = "📭 Нет подключённых групп. Сначала используй /connect в группе."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        if len(connected_chats) != 1:
            chat_ids = "\n".join(str(chat_id) for chat_id in sorted(connected_chats))
            text = (
                "➖ Подключено несколько групп. Укажи ID целевой группы:\n"
                f"/boost <chat_id> <количество>\n\nПодключённые ID:\n{chat_ids}"
            )
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        chat_id = next(iter(connected_chats))
        amount_raw = parts[1]
    else:
        try:
            chat_id = int(parts[1])
        except ValueError:
            text = "➖ ID группы должен быть числом."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        amount_raw = parts[2]
        if chat_id not in connected_chats:
            text = "➖ Эта группа не подключена к боту."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return

    try:
        amount = int(amount_raw)
    except ValueError:
        text = "➖ Количество должно быть целым числом. Пример: /boost 10"
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not 1 <= amount <= 1000:
        text = "➖ Укажи количество от 1 до 1000."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    st = get_stats(chat_id, date.today())
    eligible_ids = [
        uid for uid, user in st["users"].items()
        if int(user.get("tokens", 0)) > 0
    ]
    if not eligible_ids:
        text = "📭 В этой группе сегодня ещё нет участников, которые брали файлы через /token."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    # Случайно и равномерно распределяем добавления между участниками.
    shuffled_ids = eligible_ids[:]
    random.shuffle(shuffled_ids)
    distribution = {uid: 0 for uid in eligible_ids}

    for index in range(amount):
        if index > 0 and index % len(shuffled_ids) == 0:
            random.shuffle(shuffled_ids)
        uid = shuffled_ids[index % len(shuffled_ids)]
        user = st["users"][uid]
        user["tokens"] = int(user.get("tokens", 0)) + 1
        user["stood"] = int(user.get("stood", 0)) + 1
        distribution[uid] += 1

    # Изменяются исходные счётчики статистики и ТОПа; отдельных счётчиков нет.
    st["tokens"] += amount
    st["stood"] += amount
    save_state()

    details = [
        f"• @{st['users'][uid]['name']}: +{count}"
        for uid, count in distribution.items()
        if count
    ]
    text = (
        f"➕ Счётчики группы {chat_id} обновлены.\n"
        f"Добавлено: {amount}\n"
        f"💰 Итого: {st['tokens']}\n"
        f"➕ Валид: {st['stood']}\n\n"
        "Распределение по участникам:\n" + "\n".join(details)
    )
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")
    await log_to_admin(
        f"Корректировка счётчиков\nЧат: {chat_id}\nКоличество: {amount}\n"
        + "\n".join(details)
    )


@dp.message(Command("apanel"))
async def cmd_apanel(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет доступа к админ-панели."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Статус чатов",
                    callback_data="apanel_status",
                    style="primary",
                    icon_custom_emoji_id="5454172127307523969",
                ),
                InlineKeyboardButton(
                    text="Сброс пулов",
                    callback_data="apanel_reset",
                    style="primary",
                    icon_custom_emoji_id="5456173826880593418",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="apanel_stats",
                    style="primary",
                    icon_custom_emoji_id="5453952087543015968",
                ),
                InlineKeyboardButton(
                    text="Сброс статистики чата",
                    callback_data="apanel_reset_stats",
                    style="primary",
                    icon_custom_emoji_id="5453960484204083655",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Пополнить пул",
                    callback_data="apanel_upload_pool",
                    style="primary",
                    icon_custom_emoji_id="5454134554933619492",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Панель Модераторов",
                    callback_data="apanel_modpanel",
                    style="primary",
                    icon_custom_emoji_id="5454068128969417666",
                ),
            ],
        ]
    )
    text = "🪐 АДМИН-ПАНЕЛЬ\nУправление пулами и статистикой"
    html = fmt(text)
    await message.answer(html, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("apanel_"))
async def on_apanel_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    action = callback.data
    if action == "apanel_status":
        if connected_chats:
            lines = [f"🔗 {chat_id} — подключён" for chat_id in sorted(connected_chats)]
            lines.append(f"💰 Всего файлов в пуле: {len(chat_pools)}")
            text = "🔽 Статус чатов\n\n" + "\n".join(lines)
            html = fmt(text)
            await callback.message.edit_text(html, parse_mode="HTML")
        else:
            text = "📭 Нет подключённых чатов."
            html = fmt(text)
            await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_reset":
        chat_pools.clear()
        save_state()
        text = "➖ Все файлы из пула удалены."
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_stats":
        lines = []
        for chat_id in sorted(connected_chats):
            st = get_stats(chat_id, date.today())
            lines.append(
                f"💵 Токены: {st['tokens']}, ➕ встал: {st['stood']}, "
                f"➖ ошибка: {st['error']} (чат {chat_id})"
            )
        body = "\n".join(lines) if lines else "📭 Нет данных."
        text = "💬 Статистика\n\n" + body
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_reset_stats":
        await state.set_state(AdminStates.waiting_reset_chat)
        text = "🔽 Отправь ID чата, для которого сбросить статистику ⚠️ (безвозвратно)."
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_upload_pool":
        await state.set_state(AdminStates.waiting_upload_pool)
        text = "💰 Отправь ZIP-архив с .txt файлами — он будет добавлен в общий пул. /cancel — отмена."
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_modpanel":
        submenu = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Добавить модератора",
                        callback_data="apanel_add_mod",
                        style="primary",
                        icon_custom_emoji_id="5456360443209609671",
                    ),
                    InlineKeyboardButton(
                        text="Снять модератора",
                        callback_data="apanel_remove_mod",
                        style="primary",
                        icon_custom_emoji_id="5456173826880593418",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="◀ Назад",
                        callback_data="apanel_back",
                        style="primary",
                    ),
                ],
            ]
        )
        text = "🔗 <b>ПАНЕЛЬ МОДЕРАТОРОВ</b>\nУправление модераторами чатов."
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML", reply_markup=submenu)

    elif action == "apanel_back":
        # Возврат в главное меню админ-панели.
        main_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Статус чатов",
                        callback_data="apanel_status",
                        style="primary",
                        icon_custom_emoji_id="5454172127307523969",
                    ),
                    InlineKeyboardButton(
                        text="Сброс пулов",
                        callback_data="apanel_reset",
                        style="primary",
                        icon_custom_emoji_id="5456173826880593418",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Статистика",
                        callback_data="apanel_stats",
                        style="primary",
                        icon_custom_emoji_id="5453952087543015968",
                    ),
                    InlineKeyboardButton(
                        text="Сброс статистики чата",
                        callback_data="apanel_reset_stats",
                        style="primary",
                        icon_custom_emoji_id="5453960484204083655",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Пополнить пул",
                        callback_data="apanel_upload_pool",
                        style="primary",
                        icon_custom_emoji_id="5454134554933619492",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Панель Модераторов",
                        callback_data="apanel_modpanel",
                        style="primary",
                        icon_custom_emoji_id="5454068128969417666",
                    ),
                ],
            ]
        )
        text = "🪐 АДМИН-ПАНЕЛЬ\nУправление пулами и статистикой"
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML", reply_markup=main_keyboard)

    elif action == "apanel_add_mod":
        await state.set_state(AdminStates.waiting_mod_chat_id)
        text = (
            "➕ Добавление модератора.\n\n"
            "Отправь ID чата, в котором модератору дать права.\n"
            "/cancel — отмена."
        )
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    elif action == "apanel_remove_mod":
        await state.set_state(AdminStates.waiting_remove_moderator)
        text = (
            "➖ Снятие модератора.\n\n"
            "Отправь ID модератора (аккаунта Telegram), которого снять.\n"
            "/cancel — отмена."
        )
        html = fmt(text)
        await callback.message.edit_text(html, parse_mode="HTML")

    await callback.answer()


@dp.message(AdminStates.waiting_reset_chat)
async def on_reset_stats_chat(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    text_in = (message.text or "").strip()
    try:
        chat_id = int(text_in)
    except ValueError:
        text = "➖ ID чата должен быть числом. Попробуй ещё раз или отправь /cancel."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    # Сброс статистики выбранного чата за сегодня.
    chat_stats.pop(chat_id, None)
    save_state()

    text = f"➕ Статистика чата {chat_id} сброшена."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")
    await state.clear()


@dp.message(Command("cancel"), AdminStates.waiting_reset_chat)
async def on_cancel_reset(message: types.Message, state: FSMContext):
    await state.clear()
    text = "➖ Отменено."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("cancel"), AdminStates.waiting_upload_pool)
async def on_cancel_upload(message: types.Message, state: FSMContext):
    await state.clear()
    text = "➖ Пополнение пула отменено."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("cancel"), AdminStates.waiting_mod_chat_id)
async def on_cancel_mod_chat(message: types.Message, state: FSMContext):
    await state.clear()
    text = "➖ Добавление модератора отменено."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("cancel"), AdminStates.waiting_add_moderator)
async def on_cancel_add_mod(message: types.Message, state: FSMContext):
    await state.clear()
    text = "➖ Добавление модератора отменено."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("cancel"), AdminStates.waiting_remove_moderator)
async def on_cancel_remove_mod(message: types.Message, state: FSMContext):
    await state.clear()
    text = "➖ Снятие модератора отменено."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(AdminStates.waiting_mod_chat_id)
async def on_add_mod_chat(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID чата должен быть числом. /cancel — отмена."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return
    await state.update_data(mod_chat_id=chat_id)
    await state.set_state(AdminStates.waiting_add_moderator)
    text = (
        f"➕ Чат <b>{chat_id}</b> выбран.\n\n"
        "Теперь отправь <b>ID аккаунта Telegram</b> пользователя, "
        "которому дать права модератора.\n/cancel — отмена."
    )
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(AdminStates.waiting_add_moderator)
async def on_add_mod_user(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID аккаунта должен быть числом. /cancel — отмена."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    data = await state.get_data()
    chat_id = data.get("mod_chat_id")
    if chat_id is None:
        await state.clear()
        return

    moderators[(chat_id, user_id)] = True
    save_state()
    await log_to_admin(f"➕ Модератор {user_id} добавлен в чат {chat_id}")

    text = f"➕ Модератор <b>{user_id}</b> получил права в чате <b>{chat_id}</b>."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")
    await state.clear()


@dp.message(AdminStates.waiting_remove_moderator)
async def on_remove_mod(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID аккаунта должен быть числом. /cancel — отмена."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    removed = [(c, u) for (c, u) in moderators if u == user_id]
    for c, u in removed:
        moderators.pop((c, u), None)
    save_state()

    if removed:
        await log_to_admin(f"➖ Модератор {user_id} снят (чаты: {[c for c, _ in removed]})")
        text = f"➖ Модератор <b>{user_id}</b> снят со всех чатов."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
    else:
        text = f"❓ Модератор с ID <b>{user_id}</b> не найден."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data.in_({"stood", "error"}))
async def on_mark(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    uid = token_owner.pop(key, None)
    if uid is None:
        await callback.answer("Эта отметка уже обработана или устарела.", show_alert=True)
        return

    label = "встал" if callback.data == "stood" else "ошибка"
    marker_emoji = "➕" if callback.data == "stood" else "➖"
    by_user = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)
    caption = (
        f"{marker_emoji} <b>Помечен как {label}</b>\n"
        f"пользователем: @{by_user}"
    )
    html = fmt(caption)

    try:
        await callback.message.edit_caption(
            caption=html, parse_mode="HTML", reply_markup=None
        )
    except Exception:
        # Возвращаем привязку, чтобы можно было повторить действие после ошибки Telegram API.
        token_owner[key] = uid
        await callback.answer("Не удалось обновить сообщение. Попробуй ещё раз.", show_alert=True)
        return

    # Сразу подтверждаем колбэк, чтобы query не устарел.
    try:
        await callback.answer()
    except Exception:
        pass

    st = get_stats(chat_id, date.today())
    field = "stood" if callback.data == "stood" else "error"
    st[field] += 1

    user = st["users"].get(uid)
    if user is not None:
        user[field] += 1

    save_state()
    await log_to_admin(
        f"{'➕' if callback.data == 'stood' else '➖'} Отметка: {label}\n"
        f"Пользователь: id {uid}\nЧат: {chat_id}"
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(dp.start_polling(bot))