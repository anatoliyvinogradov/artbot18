import asyncio
import json
import os
import random
import re
import shutil
import time
import html
import logging
import base64
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from pathlib import Path
from urllib.parse import urlparse

# ---------- Конфиг ----------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

def _parse_dl_dir_map(s: str) -> dict[str, Path]:
    mp = {}
    for pair in (s or "").split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and val:
            mp[key] = Path(val).resolve()
    return mp


DEFAULT_TAGS = [t.strip() for t in os.getenv("DEFAULT_TAGS", "").split(",") if t.strip()]
MAX_TAGS = int(os.getenv("MAX_TAGS", "8"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "./images")).resolve()
USED_DIR = Path(os.getenv("USED_DIR", "./images_used")).resolve()
DEFAULT_INTERVAL_STR = os.getenv("DEFAULT_INTERVAL", "30m").strip()
ADMINS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMINS", ""))}

STATE_FILE = Path("./state.json")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

if not BOT_TOKEN or not CHANNEL_ID:
    raise RuntimeError("Заполни BOT_TOKEN и CHANNEL_ID в .env")

DL_DIR_MAP = _parse_dl_dir_map(os.getenv("DL_DIR_BY_BOT", ""))  # из .env
DEFAULT_OUT_DIR = Path(os.getenv("OUTPUT_DIR", "./images")).resolve()

CURRENT_BOT_ID: str | None = None
CURRENT_BOT_USERNAME: str | None = None

def _out_dir_for_current_bot() -> Path:
    # Сначала пробуем по ID, затем по username, иначе дефолт
    if CURRENT_BOT_ID and str(CURRENT_BOT_ID) in DL_DIR_MAP:
        return DL_DIR_MAP[str(CURRENT_BOT_ID)]
    if CURRENT_BOT_USERNAME and CURRENT_BOT_USERNAME in DL_DIR_MAP:
        return DL_DIR_MAP[CURRENT_BOT_USERNAME]
    return DEFAULT_OUT_DIR

# ---------- Утилиты ----------

DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$",
    re.IGNORECASE,
)

# ---------- Логгирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not CHANNEL_ID:
    logger.error("BOT_TOKEN или CHANNEL_ID не заданы в .env")
    raise RuntimeError("Заполни BOT_TOKEN и CHANNEL_ID в .env")
else:
    logger.info("Бот запущен. Канал: %s", CHANNEL_ID)

def parse_duration(s: str) -> int:
    """
    '90' -> 90 sec
    '10m' -> 600
    '2h30m' -> 9000
    '1d' -> 86400
    """
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    m = DURATION_RE.match(s)
    if not m:
        raise ValueError("Не смог понять интервал. Примеры: 45, 10m, 2h30m, 1d.")
    d, h, mnt, sec = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mnt * 60 + sec
    if total <= 0:
        raise ValueError("Интервал должен быть больше 0 секунд.")
    return total

def humanize_seconds(sec: int) -> str:
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return "".join(parts)

def list_images() -> list[Path]:
    if not IMAGES_DIR.exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return [p for p in IMAGES_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT]

def count_images_in(dir_path: Path) -> int:
    if not dir_path.exists():
        return 0
    return sum(1 for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT)

def move_used(src: Path) -> Path:
    USED_DIR.mkdir(parents=True, exist_ok=True)
    # чтобы избежать коллизий имен — добавим timestamp
    ts = int(time.time())
    dst = USED_DIR / f"{src.stem}_{ts}{src.suffix.lower()}"
    shutil.move(str(src), str(dst))
    return dst

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# --------- Парсинг имени файла ---------

def _decode_source_token(token: str) -> str:
    """
    Преобразует токен из круглых скобок в нормальный URL.
    Поддерживает:
      - уже готовый http/https
      - Windows-friendly https___domain_com_path
      - просто домен/путь: pixiv.net_username_file123 -> https://pixiv.net/username/file123
      - просто домен/путь с точками и слэшами
    """
    token = token.strip()

    # 1) Уже http/https — отдаем как есть
    if token.startswith(("http://", "https://")):
        return token

    # 2) Base64: b64:...
    if token.lower().startswith("b64:"):
        b64 = token[4:].strip()
        try:
            raw = base64.urlsafe_b64decode(b64 + "===").decode("utf-8", errors="ignore").strip()
            if raw.startswith(("http://", "https://")):
                return raw
        except Exception:
            return ""

    # 3) Windows-friendly с "___" вместо "://"
    if "___" in token:
        t = token.replace("___", "://", 1)
        # заменим оставшиеся "_" на "/"
        t = re.sub(r"_+", "/", t)
        return t if t.startswith(("http://", "https://")) else "https://" + t

    # 4) Простой формат: домен_путь (без протокола)
    #   pixiv.net_username_file123 -> https://pixiv.net/username/file123
    #   artstation.com_artwork_abc123 -> https://artstation.com/artwork/abc123
    if "_" in token:
        # заменим "_" на "/"
        t = token.replace("_", "/")
        return "https://" + t

    # 5) Просто домен без подчёркиваний: pixiv.net
    if "." in token:
        return "https://" + token

    # 6) Не удалось распознать
    return ""



def parse_filename_meta(image_path: Path) -> dict:
    """
    Поддержка в любом порядке/месте:
      [tag1, tag-two another]  — теги
      (https___pixiv_net)      — источник (Windows-friendly)
      author - title           — если есть, иначе всё оставшееся — title
    """
    name = image_path.stem.strip()

    # Собираем все ( ... ) и [ ... ] где угодно в строке, вырезая их
    tags: list[str] = []
    source_url = ""

    # 1) ссылки в круглых
    for m in re.finditer(r"\(([^)]+)\)", name):
        token = m.group(1).strip()
        url = _decode_source_token(token)
        if url and not source_url:  # берём первую осмысленную
            source_url = url
    name = re.sub(r"\([^)]+\)", "", name).strip()

    # 2) теги в квадратных
    for m in re.finditer(r"\[([^\]]+)\]", name):
        raw = m.group(1).strip()
        pieces = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        tags.extend(pieces)
    name = re.sub(r"\[[^\]]+\]", "", name).strip()

    # 3) author - title (опционально)
    author_name = ""
    title = ""
    m_at = re.match(r"(.+?)\s*-\s*(.+)$", name)
    if m_at:
        author_name = m_at.group(1).strip()
        title = m_at.group(2).strip()
    else:
        title = name.strip()

    return {
        "title": title,
        "author_name": author_name,
        "source_url": source_url,
        "tags": tags,
    }


# --------- Вспомогательные для подписи ---------

def _sanitize_tag(tag: str) -> str:
    # хештег: нижний регистр, пробелы -> _, оставляем буквы/цифры/_
    t = str(tag).strip().lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9_а-яё]", "", t)

def _domain(u: str) -> str:
    try:
        netloc = urlparse(u).netloc
        return netloc or ""
    except Exception:
        return ""

def build_caption_from_meta(meta: dict, default_tags: list[str] = None, max_tags: int = 8) -> str:
    """
    Собирает HTML-подпись только с источником и тегами.
    Пустые поля — пропускаем. Учитываем дефолтные теги.
    """
    default_tags = default_tags or []

    source_url = (meta.get("source_url") or "").strip()
    tags_src = meta.get("tags") or []

    # нормализация тегов
    all_tags = []
    for t in list(tags_src) + list(default_tags):
        st = _sanitize_tag(t)
        if st and st not in all_tags:
            all_tags.append(st)
    if max_tags > 0:
        all_tags = all_tags[:max_tags]
    hashtags = " ".join(f"#{t}" for t in all_tags)

    parts = []
    if source_url:
        dom = _domain(source_url) or "источник"
        parts.append(f'Источник: <a href="{source_url}">{html.escape(dom)}</a>')
    if hashtags:
        parts.append(f"Теги: {hashtags}")

    caption = "\n".join(parts).strip()
    if len(caption) > 1024:
        caption = caption[:1019].rstrip() + "…"
    return caption


# ---------- Глобальное состояние планировщика ----------

@dataclass
class SchedulerState:
    interval_sec: int
    next_post_ts: Optional[float] = None

scheduler_state = SchedulerState(interval_sec=parse_duration(DEFAULT_INTERVAL_STR))

# События/локи
reset_event = asyncio.Event()        # когда надо немедленно перепланировать /post
post_lock = asyncio.Lock()           # чтобы не наложились два постинга

# ---------- Бот ----------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

async def do_post_random_or_specific(filename: Optional[str] = None) -> str:
    """
    Если filename указан — ищем по имени (поддерживает частичное совпадение без учёта регистра).
    Иначе — случайное изображение.
    Возвращает человекочитаемое описание того, что отправлено.
    """
    imgs = list_images()
    if not imgs:
        raise RuntimeError("Папка с изображениями пуста.")

    # выбор файла
    if filename:
        needle = filename.strip().lower()
        exact = [p for p in imgs if p.name.lower() == needle]
        chosen = exact[0] if exact else None
        if not chosen:
            subset = [p for p in imgs if needle in p.name.lower()]
            if not subset:
                raise RuntimeError(f"Файл '{filename}' не найден в {IMAGES_DIR}")
            chosen = random.choice(subset)
    else:
        chosen = random.choice(imgs)

    logger.info("Выбран файл: %s", chosen)

    # метаданные + подпись
    meta = parse_filename_meta(chosen)
    caption = build_caption_from_meta(meta, default_tags=DEFAULT_TAGS, max_tags=MAX_TAGS)
    logger.info("Meta: %s", meta)
    logger.info("Caption preview: %r", caption)

    # отправка + перенос
    async with post_lock:
        file = FSInputFile(str(chosen))
        await bot.send_photo(chat_id=CHANNEL_ID, photo=file, caption=caption if caption else None)
        moved_to = move_used(chosen)

    logger.info("Файл %s отправлен и перемещён в %s", chosen.name, USED_DIR)
    return f"Опубликовано: <code>{moved_to.name}</code> (перенесено в {USED_DIR})"


async def scheduler_loop():
    """
    Простой цикл: спит до следующего времени постинга,
    реагирует на reset_event (когда /post делает пост и сбрасывает отсчёт).
    """
    logger.info("Планировщик запущен. Интервал: %s", humanize_seconds(scheduler_state.interval_sec))
    # восстановим состояние (интервал/следующее время) при старте
    state = load_state()
    if "interval_sec" in state:
        scheduler_state.interval_sec = int(state["interval_sec"])
    if "next_post_ts" in state:
        scheduler_state.next_post_ts = float(state["next_post_ts"])

    while True:
        try:
            now = time.time()
            if scheduler_state.next_post_ts is None or scheduler_state.next_post_ts <= now:
                # Сразу постим (если времени нет или просрочено)
                try:
                    await do_post_random_or_specific(None)
                except Exception as e:
                    # Если пусто — просто перенесём next_post, чтобы не спамить
                    print(f"[scheduler] Ошибка постинга: {e}")
                # Назначаем следующее
                scheduler_state.next_post_ts = time.time() + scheduler_state.interval_sec
                save_state({"interval_sec": scheduler_state.interval_sec,
                            "next_post_ts": scheduler_state.next_post_ts})

            # Ждём либо до дедлайна, либо сброса
            wait_time = max(0, scheduler_state.next_post_ts - time.time())
            try:
                reset_event.clear()
                # ждем меньше из двух: либо таймаут, либо ресет
                await asyncio.wait_for(reset_event.wait(), timeout=wait_time)
                # если сработал reset_event — просто продолжаем цикл (в нём уже всё переставим)
                continue
            except asyncio.TimeoutError:
                # Время вышло — цикл снова постит
                continue

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[scheduler] Ошибка: %s", e, exc_info=True)
            await asyncio.sleep(5)

# ---------- Команды ----------

@dp.message(Command("start", "help"))
async def cmd_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("Привет! Я автопостер изображений для канала.")
    text = (
        "<b>Команды для админа</b>\n"
        "/post — запостить сразу случайное изображение и <i>сбросить таймер</i>\n"
        "/post <имя_файла_или_часть> — запостить конкретный файл (если есть) и сбросить таймер\n"
        "/settime <интервал> — установить интервал (напр. 45, 10m, 2h30m, 1d)\n"
        "/status — показать текущие настройки\n"
    )
    await msg.answer(text)

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    nxt = scheduler_state.next_post_ts
    eta = int(max(0, (nxt - time.time()))) if nxt else None

    total_pending = count_images_in(IMAGES_DIR)
    total_used = count_images_in(USED_DIR)

    text_lines = [
        "📊 <b>Статус</b>",
        f"Папка: <code>{IMAGES_DIR}</code>",
        f"Использованные: <code>{USED_DIR}</code>",
        f"Доступно к постингу: <b>{total_pending}</b> шт.",
        f"Уже опубликовано (в used): <b>{total_used}</b> шт.",
        f"Интервал: <code>{humanize_seconds(scheduler_state.interval_sec)}</code>",
    ]
    if eta is not None:
        text_lines.append(f"Следующий пост через: <code>{humanize_seconds(eta)}</code>")
    else:
        text_lines.append("Следующий пост: не запланирован")

    logger.info("Команда /status от %s (%s): pending=%d, used=%d",
            msg.from_user.full_name, msg.from_user.id, total_pending, total_used)
    await msg.answer("\n".join(text_lines))


@dp.message(Command("settime"))
async def cmd_settime(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    if not command or not command.args:
        return await msg.answer(
            "Укажи интервал. Примеры: <code>/settime 45</code>, <code>/settime 10m</code>, <code>/settime 2h30m</code>"
        )

    try:
        sec = parse_duration(command.args)
        scheduler_state.interval_sec = sec
        scheduler_state.next_post_ts = time.time() + sec
        save_state({"interval_sec": scheduler_state.interval_sec,
                    "next_post_ts": scheduler_state.next_post_ts})
        reset_event.set()

        logger.info("Команда /settime от %s (%s) новый интервал: %s",
                    msg.from_user.full_name, msg.from_user.id, humanize_seconds(sec))

        await msg.answer(
            f"✅ Интервал установлен: <code>{humanize_seconds(sec)}</code>. "
            f"Следующий пост через <code>{humanize_seconds(sec)}</code>."
        )
    except Exception as e:
        logger.exception("Ошибка в /settime: %s", e)
        return await msg.answer(f"❌ {e}", parse_mode=None)


@dp.message(Command("post"))
async def cmd_post(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return

    filename = None  # <-- инициализируем заранее, чтобы не было UnboundLocalError
    try:
        if command and command.args:
            filename = command.args.strip()

        logger.info("Команда /post от %s (%s) аргумент: %s",
                    msg.from_user.full_name, msg.from_user.id, filename)

        info = await do_post_random_or_specific(filename)

        # Сбрасываем таймер и пересчитываем
        scheduler_state.next_post_ts = time.time() + scheduler_state.interval_sec
        save_state({"interval_sec": scheduler_state.interval_sec,
                    "next_post_ts": scheduler_state.next_post_ts})
        reset_event.set()

        await msg.answer(
            f"✅ {info}\nТаймер сброшен. Следующий пост через <code>{humanize_seconds(scheduler_state.interval_sec)}</code>."
        )
    except Exception as e:
        logger.exception("Ошибка в /post: %s", e)
        return await msg.answer(f"❌ {e}", parse_mode=None)

@dp.message(Command("dl"))
async def cmd_dl(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return

    usage = "Использование: <code>/dl &lt;pixiv_id&gt; &lt;tag&gt;</code>\nНапр: <code>/dl 124856160 art</code>"
    if not command or not command.args:
        return await msg.answer(usage)

    parts = command.args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(usage)

    pixiv_id, extra_tag = parts[0], parts[1]
    out_dir = _out_dir_for_current_bot()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "pixiv_dl.py"),
             "--id", pixiv_id, "--tags", extra_tag, "--out", str(out_dir)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            env=env,
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            return await msg.answer(
                "❌ Ошибка Pixiv:\n<pre>{}</pre>".format(html.escape(stderr or stdout or "no output")),
                parse_mode=ParseMode.HTML,
            )

        await msg.answer(
            "✅ Pixiv → <code>{}</code>\n<pre>{}</pre>".format(
                html.escape(str(out_dir)), html.escape(stdout or "Скрипт отработал без вывода")
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Ошибка в /dl: %s", e)
        await msg.answer(f"❌ Ошибка запуска: {e}")


@dp.message(Command("dl_da"))
async def cmd_dl_da(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return

    usage = "Использование: <code>/dl_da &lt;id&gt; &lt;tag&gt;</code>\nНапр: <code>/dl_da 1104774946 art</code>"
    if not command or not command.args:
        return await msg.answer(usage)

    parts = command.args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(usage)

    dev_id, tag = parts[0], parts[1]
    out_dir = _out_dir_for_current_bot()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "deviantart_dl.py"),
             "--id", dev_id, "--tags", tag, "--out", str(out_dir)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            env=env,
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            return await msg.answer(
                "❌ Ошибка DeviantArt:\n<pre>{}</pre>".format(html.escape(stderr or stdout or "no output")),
                parse_mode=ParseMode.HTML,
            )

        await msg.answer(
            "✅ DeviantArt → <code>{}</code>\n<pre>{}</pre>".format(
                html.escape(str(out_dir)), html.escape(stdout or "Скрипт отработал без вывода")
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Ошибка в /dl_da: %s", e)
        await msg.answer(f"❌ Ошибка запуска: {e}")


# ---------- Точка входа ----------

async def main():
    global CURRENT_BOT_ID, CURRENT_BOT_USERNAME

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    USED_DIR.mkdir(parents=True, exist_ok=True)

    # Узнаём кто мы
    me = await bot.get_me()
    CURRENT_BOT_ID = me.id
    CURRENT_BOT_USERNAME = (me.username or "").lower()
    logger.info("Запущен бот: id=%s username=@%s → out_dir=%s",
                CURRENT_BOT_ID, CURRENT_BOT_USERNAME, _out_dir_for_current_bot())

    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass



