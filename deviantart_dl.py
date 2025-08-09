import argparse
import base64
import os
import re
import sys
import json
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --- Фикс кодировки Windows-консоли ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./images")).resolve()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS", "")).split() if t.strip()]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Пример валидных URL:
# https://www.deviantart.com/dashawallflower/art/Merchant-meets-the-wolf-1104774946
# https://www.deviantart.com/deviation/1104774946
ID_RE = re.compile(r"(\d{6,})")
URL_ID_RE = re.compile(r"deviantart\.com/(?:deviation/|.+?/art/.+?-)(\d+)", re.I)

def parse_id(text: str) -> Optional[str]:
    """ Извлекаем ID из URL или строки с цифрами. """
    if not text:
        return None
    m = URL_ID_RE.search(text)
    if m:
        return m.group(1)
    m = ID_RE.search(text)
    if m:
        return m.group(1)
    return None

def make_artwork_url(art_input: str) -> str:
    """ Если передали ID — формируем URL deviation/<id>, иначе возвращаем как есть. """
    if art_input.startswith("http://") or art_input.startswith("https://"):
        return art_input
    illust_id = parse_id(art_input)
    if not illust_id:
        raise SystemExit("Не удалось распознать ID DeviantArt.")
    return f"https://www.deviantart.com/deviation/{illust_id}"

def sanitize_filename(name: str) -> str:
    # умеренная чистка для NTFS и пр.
    name = unicodedata.normalize("NFKC", name).strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name

def guess_ext_from_url(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.I)
    return ("." + m.group(1).lower()) if m else ".jpg"

def get_soup(sess: requests.Session, url: str) -> BeautifulSoup:
    headers = {"User-Agent": UA, "Referer": url}
    r = sess.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def extract_from_meta(soup: BeautifulSoup) -> dict:
    """ Берём og: и twitter: мета — это самый стабильный способ достать основное изображение. """
    def mprop(prop):
        el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (el.get("content") or "").strip() if el else ""

    og_title = mprop("og:title") or mprop("twitter:title")
    og_url   = mprop("og:url")
    og_image = mprop("og:image") or mprop("twitter:image")

    return {
        "title": og_title,
        "canonical_url": og_url,
        "image_url": og_image,
    }

def try_extract_nextdata(soup: BeautifulSoup) -> dict:
    """
    Фоллбек: иногда DeviantArt прячет данные в JSON внутри <script> (Next.js state).
    Мы попробуем найти большой JSON и достать поля title / media URL.
    """
    data = {}
    # ищем большие JSON-скрипты
    for sc in soup.find_all("script"):
        txt = sc.string or sc.text or ""
        if not txt:
            continue
        if "deviationId" in txt and "media" in txt:
            # попытка грубого извлечения JSON-объекта deviation
            # (не идеальный способ, но как фоллбек)
            try:
                # иногда встречается window.__INITIAL_STATE__=...;
                jtxt = txt
                # выцепим первый { ... } который похож на JSON
                start = jtxt.find("{")
                end = jtxt.rfind("}")
                if start >= 0 and end > start:
                    obj = json.loads(jtxt[start:end+1])
                    # попытаемся пройтись по возможным путям
                    # (структуры могут отличаться; поэтому берём best-effort)
                    title = ""
                    image = ""
                    can_url = ""
                    def walk(o):
                        nonlocal title, image, can_url
                        if isinstance(o, dict):
                            if not title and "title" in o and isinstance(o["title"], str):
                                title = o["title"]
                            if not can_url and "url" in o and isinstance(o["url"], str) and "deviantart.com" in o["url"]:
                                can_url = o["url"]
                            # media url может быть в разных местах
                            if (not image) and "src" in o and isinstance(o["src"], str) and o["src"].startswith("http"):
                                image = o["src"]
                            for v in o.values():
                                walk(v)
                        elif isinstance(o, list):
                            for v in o:
                                walk(v)
                    walk(obj)
                    if title or image or can_url:
                        data = {
                            "title": title,
                            "canonical_url": can_url,
                            "image_url": image,
                        }
                        return data
            except Exception:
                continue
    return data

def build_token_from_canonical(canonical_url: str) -> str:
    """
    Формируем токен для имени:
      deviantart.com_<username>_art_<slug-id>
    Если canonical пустой — вернём deviantart.com_deviation_<id> (если вытащили id).
    """
    if not canonical_url:
        return "deviantart.com"
    u = urlparse(canonical_url)
    netloc = u.netloc  # deviantart.com
    path = u.path.strip("/")

    # Ожидаемый путь: <username>/art/<slug-id>
    # Если формат другой, всё равно склеим через "_"
    token_path = path.replace("/", "_")
    token = f"{netloc}_{token_path}" if token_path else netloc
    return token

def download_image(sess: requests.Session, img_url: str, referer_url: str) -> bytes:
    if not img_url:
        raise RuntimeError("Не найдено изображение (image_url пуст).")
    headers = {
        "User-Agent": UA,
        "Referer": referer_url or "https://www.deviantart.com/",
    }
    r = sess.get(img_url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content

def make_filename(token: str, title: str, tags: list[str]) -> str:
    # квадратные скобки — теги (через пробел), круглые — токен-ссылка
    safe_title = sanitize_filename(title)[:120] or "deviation"
    tag_block = " ".join(tags) if tags else ""
    return f"[{tag_block}]({token}){safe_title}"

def run(art_input: str, out_dir: Path, extra_tags: list[str]):
    url = make_artwork_url(art_input)
    out_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as sess:
        soup = get_soup(sess, url)

        # 1) основной способ — og: мета
        info = extract_from_meta(soup)

        # 2) фоллбек — пробуем вынуть из встроенного JSON
        if not info.get("image_url"):
            fallback = try_extract_nextdata(soup)
            # аккуратно заполняем пустые поля
            for k, v in fallback.items():
                if v and not info.get(k):
                    info[k] = v

        title = info.get("title") or ""
        can_url = info.get("canonical_url") or url  # если не нашли canonical — оставим исходный
        img_url = info.get("image_url") or ""

        if not img_url:
            raise SystemExit("Не удалось определить URL основного изображения. Страница может быть защищена/меняться.")

        token = build_token_from_canonical(can_url)
        # теги: если были переданы через --tags — берём их, иначе DEFAULT_TAGS
        tags = extra_tags if extra_tags else DEFAULT_TAGS

        base_name = make_filename(token, title, tags)
        ext = guess_ext_from_url(img_url)
        out_path = out_dir / (base_name + ext)

        data = download_image(sess, img_url, referer_url=can_url)

        # избегаем коллизий
        final_path = out_path
        i = 1
        while final_path.exists():
            final_path = out_dir / f"{base_name} ({i}){ext}"
            i += 1

        final_path.write_bytes(data)
        print(f"Saved: {final_path}")

def main():
    parser = argparse.ArgumentParser(description="Download main DeviantArt image and save with custom filename.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="DeviantArt artwork URL, e.g. https://www.deviantart.com/USER/art/slug-1104774946")
    g.add_argument("--id", help="DeviantArt artwork ID, e.g. 1104774946")
    parser.add_argument("--out", default=str(OUTPUT_DIR), help="Output directory (default from .env OUTPUT_DIR)")
    parser.add_argument("--tags", default="", help="Extra tags separated by spaces, e.g. 'art fanart'")
    args = parser.parse_args()

    art_input = args.url or args.id
    out_dir = Path(args.out).resolve()
    extra_tags = [t for t in (args.tags or "").split() if t.strip()]

    run(art_input, out_dir, extra_tags)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
