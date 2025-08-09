import argparse
import os
import re
import sys
import json
import pathlib
import unicodedata
from typing import Optional

import requests
from dotenv import load_dotenv

# --- Фикс кодировки для Windows ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

PIXIV_PHPSESSID = os.getenv("PIXIV_PHPSESSID") or os.getenv("PIXIV_SESSION") or os.getenv("PHPSESSID")
OUTPUT_DIR = pathlib.Path(os.getenv("OUTPUT_DIR", "./images")).resolve()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS", "")).split() if t.strip()]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

PIXIV_AJAX_ILLUST = "https://www.pixiv.net/ajax/illust/{id}"
PIXIV_AJAX_PAGES  = "https://www.pixiv.net/ajax/illust/{id}/pages"

IMG_REFERER_FMT = "https://www.pixiv.net/en/artworks/{id}"  # важно для i.pximg.net

ID_RE = re.compile(r"(\d{6,})")
URL_ID_RE = re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", re.I)

def parse_id(text: str) -> Optional[str]:
    """Достаём ID из URL или строки с цифрами."""
    m = URL_ID_RE.search(text)
    if m:
        return m.group(1)
    m = ID_RE.search(text)
    if m:
        return m.group(1)
    return None

def sanitize_filename(name: str) -> str:
    # нормализуем юникод, убираем недопустимые для NTFS символы, приводим пробелы
    name = unicodedata.normalize("NFKC", name).strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name)
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
    raise RuntimeError(f"Pixiv ajax error: {data.get('message') or 'unknown'}")

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
    """
    Берём оригинал, если есть, иначе 'regular'.
    Для мультимпейдж — первую страницу.
    """
    # single
    if not pages:
        urls = (illust.get("urls") or {})
        return urls.get("original") or urls.get("regular") or urls.get("small") or urls.get("thumb")
    # multi -> первая страница
    first = pages[0] or {}
    urls = first.get("urls") or {}
    return urls.get("original") or urls.get("regular") or urls.get("small") or urls.get("thumb")

def download_image(sess: requests.Session, url: str, illust_id: str) -> bytes:
    if not url:
        raise RuntimeError("Не удалось получить URL изображения")
    headers = {
        "User-Agent": UA,
        "Referer": IMG_REFERER_FMT.format(id=illust_id),
        "Cookie": f"PHPSESSID={PIXIV_PHPSESSID}",
    }
    r = sess.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content

def guess_ext_from_url(url: str) -> str:
    # часто у Pixiv оригиналы .png/.jpg/.jpeg/.gif
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|\Z)", url, re.I)
    return ("." + m.group(1).lower()) if m else ".jpg"

def make_filename(illust_id: str, title: str, tags: list[str]) -> str:
    token = f"pixiv.net_en_artworks_{illust_id}"  # именно так просил
    # теги для [ ... ] можно пока опустить или использовать DEFAULT_TAGS
    tag_block = " ".join(tags) if tags else ""
    safe_title = sanitize_filename(title)[:120] or f"artwork_{illust_id}"
    return f"[{tag_block}]({token}){safe_title}"

def run(art_input: str, out_dir: pathlib.Path, extra_tags: list[str]):
    if not PIXIV_PHPSESSID:
        raise SystemExit("В .env не найден PIXIV_PHPSESSID — залогинься на pixiv и скопируй значение куки.")

    illust_id = parse_id(art_input)
    if not illust_id:
        raise SystemExit("Не смог извлечь ID из аргумента. Пример URL: https://www.pixiv.net/en/artworks/123456789")

    out_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as sess:
        illust = get_illust_json(sess, illust_id)

        # страницы (для мультистраничных работ)
        pages = []
        if int(illust.get("pageCount") or 1) > 1:
            pages = get_pages_json(sess, illust_id)

        url = pick_main_image_url(illust, pages)

        # метаданные для имени
        title = illust.get("title") or ""
        # Можно использовать теги Pixiv, но они часто японские/с пробелами/сервисные.
        tags = list(DEFAULT_TAGS) + (extra_tags or [])
        filename_base = make_filename(illust_id, title, tags)
        ext = guess_ext_from_url(url)
        out_path = (out_dir / (filename_base + ext))

        # скачиваем
        data = download_image(sess, url, illust_id)

        # пишем на диск
        # на всякий случай избегаем коллизий
        final_path = out_path
        i = 1
        while final_path.exists():
            final_path = out_dir / f"{filename_base} ({i}){ext}"
            i += 1

        final_path.write_bytes(data)
        print(f"Saved: {final_path}")

def main():
    parser = argparse.ArgumentParser(description="Download main Pixiv image and save with custom filename.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="Pixiv artwork URL, e.g. https://www.pixiv.net/en/artworks/123456789")
    g.add_argument("--id", help="Pixiv artwork ID, e.g. 123456789")
    parser.add_argument("--out", default=str(OUTPUT_DIR), help="Output directory (default from .env OUTPUT_DIR)")
    parser.add_argument("--tags", default="", help="Extra tags separated by spaces, e.g. 'art fanart'")
    args = parser.parse_args()

    art_input = args.url or args.id
    out_dir = pathlib.Path(args.out).resolve()
    extra_tags = [t for t in args.tags.split() if t.strip()]
    run(art_input, out_dir, extra_tags)

if __name__ == "__main__":
    try:
        run_from_cli = True
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
