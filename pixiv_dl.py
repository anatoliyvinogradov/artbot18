#!/usr/bin/env python3
import argparse
import os
import re
import sys
import pathlib
import unicodedata
from typing import Optional, Iterable, List

import requests
from dotenv import load_dotenv

# --- Фикс кодировки для Windows-консоли (безопасно на Linux) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- .env ---
load_dotenv()

PIXIV_PHPSESSID = (
    os.getenv("PIXIV_PHPSESSID")
    or os.getenv("PIXIV_SESSION")
    or os.getenv("PHPSESSID")
)
OUTPUT_DIR = pathlib.Path(os.getenv("OUTPUT_DIR", "./images")).resolve()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS", "")).split() if t.strip()]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

PIXIV_AJAX_ILLUST = "https://www.pixiv.net/ajax/illust/{id}"
PIXIV_AJAX_PAGES  = "https://www.pixiv.net/ajax/illust/{id}/pages"
IMG_REFERER_FMT   = "https://www.pixiv.net/en/artworks/{id}"

ID_RE = re.compile(r"(\d{6,})")
URL_ID_RE = re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", re.I)


# ----------------- Утилиты -----------------

def parse_id(token: str) -> Optional[str]:
    """Достаём ID из URL или строки с цифрами (одной)."""
    if not token:
        return None
    m = URL_ID_RE.search(token)
    if m:
        return m.group(1)
    m = ID_RE.search(token)
    if m:
        return m.group(1)
    return None


def split_inputs(raw: Optional[str]) -> List[str]:
    """Разбивает строку по запятой в список токенов (id или url)."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def sanitize_filename(name: str) -> str:
    # нормализуем юникод, убираем недопустимые символы, приводим пробелы
    name = unicodedata.normalize("NFKC", name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def get_illust_json(sess: requests.Session, illust_id: str) -> dict:
    headers = {
        "User-Agent": UA,
        "Referer": IMG_REFERER_FMT.format(id=illust_id),
        "Cookie": f"PHPSESSID={PIXIV_PHPSESSID}",
    }
    r = sess.get(PIXIV_AJAX_ILLUST.format(id=illust_id), headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("error") and data.get("body"):
        return data["body"]
    raise RuntimeError(f"Pixiv ajax error: {data.get('message') or 'unknown'} (id={illust_id})")


def get_pages_json(sess: requests.Session, illust_id: str) -> list[dict]:
    headers = {
        "User-Agent": UA,
        "Referer": IMG_REFERER_FMT.format(id=illust_id),
        "Cookie": f"PHPSESSID={PIXIV_PHPSESSID}",
    }
    r = sess.get(PIXIV_AJAX_PAGES.format(id=illust_id), headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("error") and data.get("body") is not None:
        return data["body"]
    return []


def pick_main_image_url(illust: dict, pages: list[dict]) -> str:
    """URL основной (первой) картинки: предпочитаем original, затем regular/small/thumb."""
    def pick_from(urls: dict) -> Optional[str]:
        return urls.get("original") or urls.get("regular") or urls.get("small") or urls.get("thumb")

    if not pages:
        urls = (illust.get("urls") or {})
        url = pick_from(urls)
        if url:
            return url
        raise RuntimeError("Не найден URL изображения (single).")
    first = pages[0] or {}
    urls = first.get("urls") or {}
    url = pick_from(urls)
    if url:
        return url
    raise RuntimeError("Не найден URL первой страницы (multi).")


def iter_all_page_urls(illust: dict, pages: list[dict]) -> Iterable[str]:
    """Итерация по URL всех страниц (или одной, если работа одиночная)."""
    def pick_from(urls: dict) -> Optional[str]:
        return urls.get("original") or urls.get("regular") or urls.get("small") or urls.get("thumb")

    if not pages:
        urls = (illust.get("urls") or {})
        url = pick_from(urls)
        if url:
            yield url
        return
    for p in pages:
        urls = (p or {}).get("urls") or {}
        url = pick_from(urls)
        if url:
            yield url


def download_image(sess: requests.Session, url: str, illust_id: str) -> bytes:
    if not url:
        raise RuntimeError("Пустой URL изображения")
    headers = {
        "User-Agent": UA,
        "Referer": IMG_REFERER_FMT.format(id=illust_id),
        "Cookie": f"PHPSESSID={PIXIV_PHPSESSID}",
    }
    r = sess.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content


def guess_ext_from_url(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.I)
    return ("." + m.group(1).lower()) if m else ".jpg"


def make_filename(illust_id: str, title: str, tags: list[str]) -> str:
    token = f"pixiv.net_en_artworks_{illust_id}"   # формат для парсера бота
    tag_block = " ".join(tags) if tags else ""
    safe_title = sanitize_filename(title)[:120] or f"artwork_{illust_id}"
    return f"[{tag_block}]({token}){safe_title}"


def save_blob(out_dir: pathlib.Path, base: str, ext: str, blob: bytes, suffix: Optional[str] = None) -> pathlib.Path:
    name = base + (suffix or "") + ext
    path = out_dir / name
    i = 1
    while path.exists():
        path = out_dir / f"{base}{suffix or ''} ({i}){ext}"
        i += 1
    path.write_bytes(blob)
    return path


def process_single(
    sess: requests.Session,
    illust_id: str,
    out_dir: pathlib.Path,
    extra_tags: list[str],
    download_all: bool,
) -> list[pathlib.Path]:
    """Скачивает 1 работу (одну или все страницы). Возвращает список путей."""
    illust = get_illust_json(sess, illust_id)
    pages = get_pages_json(sess, illust_id) if int(illust.get("pageCount") or 1) > 1 else []

    title = illust.get("title") or ""
    tags  = list(DEFAULT_TAGS) + (extra_tags or [])
    base  = make_filename(illust_id, title, tags)

    saved_paths: list[pathlib.Path] = []

    if download_all:
        for idx, url in enumerate(iter_all_page_urls(illust, pages)):
            ext  = guess_ext_from_url(url)
            blob = download_image(sess, url, illust_id)
            suffix = f"_p{idx}"
            saved_paths.append(save_blob(out_dir, base, ext, blob, suffix))
    else:
        url  = pick_main_image_url(illust, pages)
        ext  = guess_ext_from_url(url)
        blob = download_image(sess, url, illust_id)
        saved_paths.append(save_blob(out_dir, base, ext, blob))

    return saved_paths


# ----------------- CLI -----------------

def main():
    if not PIXIV_PHPSESSID:
        raise SystemExit("В .env не найден PIXIV_PHPSESSID — залогинься на pixiv и скопируй значение куки.")

    parser = argparse.ArgumentParser(
        description=(
            "Download Pixiv image(s) with custom filename.\n"
            "Формат 1 (простой):\n"
            "  pixiv_dl.py <ID|URL[,ID|URL,...]> [tags...] [--all] [--out DIR]\n"
            "Формат 2 (старый, совместимый):\n"
            "  pixiv_dl.py --id <...> [--tags \"...\"] [--all] [--out DIR]\n"
        )
    )

    # Простой позиционный режим
    parser.add_argument("inputs", nargs="?", help="ID/URL или список через запятую (позиционно)")
    parser.add_argument("tags_pos", nargs="*", help="Теги позиционно (несколько слов)")

    # Старые флаги (совместимость)
    parser.add_argument("--url", help="URL или список URL (через запятую)")
    parser.add_argument("--id", help="ID или список ID/URL (через запятую)")
    parser.add_argument("--tags", default="", help="Теги через пробел (флаг)")

    # Общие опции
    parser.add_argument("--out", default=str(OUTPUT_DIR), help="Выходная папка")
    parser.add_argument("--all", dest="download_all", action="store_true", help="Скачать все страницы работы")

    args = parser.parse_args()

    # 1) Источник ID/URL
    raw_inputs = args.inputs or args.url or args.id
    if not raw_inputs:
        raise SystemExit("Нужно передать ID/URL. См. --help")

    tokens = split_inputs(raw_inputs)

    # 2) Теги и флаг --all
    download_all = bool(args.download_all)

    if args.inputs:
        # Позиционный режим: теги в tags_pos
        tag_tokens = list(args.tags_pos or [])
    else:
        # Старый режим: теги во флаге --tags
        tag_tokens = [t for t in (args.tags or "").split() if t.strip()]

    # Если --all случайно попал в теги — учитываем и убираем
    if "--all" in tag_tokens:
        download_all = True
        tag_tokens = [t for t in tag_tokens if t != "--all"]

    extra_tags = tag_tokens

    # 3) Конвертируем в список числовых ID
    id_list: list[str] = []
    for tok in tokens:
        iid = parse_id(tok)
        if iid:
            id_list.append(iid)
        else:
            print(f"[warn] Пропущен токен без ID: {tok}")

    if not id_list:
        raise SystemExit("Не удалось извлечь ни одного Pixiv ID.")

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as sess:
        for iid in id_list:
            try:
                paths = process_single(sess, iid, out_dir, extra_tags, download_all)
                for p in paths:
                    print(f"Saved: {p}")
            except Exception as e:
                print(f"[error] {iid}: {e}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
