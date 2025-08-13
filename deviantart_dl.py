#!/usr/bin/env python3
import argparse
import os
import re
import sys
import json
import unicodedata
from pathlib import Path
from typing import Optional, Iterable, List, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# --- Фикс кодировки Windows-консоли (безопасно на Linux) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./images")).resolve()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS", "")).split() if t.strip()]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Примеры URL:
# https://www.deviantart.com/dashawallflower/art/Merchant-meets-the-wolf-1104774946
# https://www.deviantart.com/deviation/1104774946
ID_RE = re.compile(r"(\d{6,})")
URL_ID_RE = re.compile(r"deviantart\.com/(?:deviation/|.+?/art/.+?-)(\d+)", re.I)

IMG_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", re.I)


# ----------------- Утилиты -----------------

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


def split_inputs(raw: Optional[str]) -> List[str]:
    """Разбивает строку по запятой в список токенов (id или url)."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def make_artwork_url(art_input: str) -> str:
    """ Если передали ID — формируем URL deviation/<id>, иначе возвращаем как есть. """
    if art_input.startswith(("http://", "https://")):
        return art_input
    illust_id = parse_id(art_input)
    if not illust_id:
        raise SystemExit("Не удалось распознать ID DeviantArt.")
    return f"https://www.deviantart.com/deviation/{illust_id}"


def sanitize_filename(name: str) -> str:
    # умеренная чистка для NTFS и пр.
    name = unicodedata.normalize("NFKC", name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def guess_ext_from_url(url: str) -> str:
    m = IMG_EXT_RE.search(url)
    return ("." + m.group(1).lower()) if m else ".jpg"


def get_soup(sess: requests.Session, url: str) -> BeautifulSoup:
    headers = {"User-Agent": UA, "Referer": url}
    r = sess.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def extract_from_meta(soup: BeautifulSoup) -> Tuple[str, str, List[str]]:
    """
    Берём og:/twitter: мета — основной title, canonical URL и один image.
    Возвращаем (title, canonical_url, [image_urls]).
    """
    def mprop(prop):
        el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (el.get("content") or "").strip() if el else ""

    og_title = mprop("og:title") or mprop("twitter:title")
    og_url   = mprop("og:url")
    og_img   = mprop("og:image") or mprop("twitter:image")

    imgs = []
    if og_img:
        imgs.append(og_img)

    return og_title, og_url, imgs


def try_extract_nextdata_all_images(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Фоллбек/расширение: попробуем найти в JSON (Next.js state, initial state и т.п.)
    все встречающиеся поля-URL-изображений.
    Возвращаем (title|None, canonical_url|None, [image_urls]).
    """
    title = None
    canonical = None
    image_urls: List[str] = []

    def add_url(u: str):
        if not u:
            return
        # только вероятные изображения
        if IMG_EXT_RE.search(u) and u not in image_urls:
            image_urls.append(u)

    for sc in soup.find_all("script"):
        txt = sc.string or sc.text or ""
        if not txt or ("{" not in txt or "}" not in txt):
            continue
        # грубая попытка найти большой JSON
        start = txt.find("{")
        end = txt.rfind("}")
        if start < 0 or end <= start:
            continue
        jtxt = txt[start:end+1]

        try:
            obj = json.loads(jtxt)
        except Exception:
            continue

        # рекурсивно пройдём по объекту, собирая title/url/src, похожие на нужные
        def walk(o):
            nonlocal title, canonical
            if isinstance(o, dict):
                # возможные заголовки
                if not title:
                    t = o.get("title")
                    if isinstance(t, str) and t.strip():
                        title = t.strip()
                # canonical
                if not canonical:
                    u = o.get("url")
                    if isinstance(u, str) and "deviantart.com" in u:
                        canonical = u
                # возможные ссылки на изображения
                for k in ("src", "href", "url"):
                    v = o.get(k)
                    if isinstance(v, str):
                        add_url(v)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(obj)

    return title, canonical, image_urls


def build_token_from_canonical(canonical_url: str, fallback_id: Optional[str]) -> str:
    """
    Формируем токен для имени:
      deviantart.com_<username>_art_<slug-id>
    Если canonical пустой — deviantart.com_deviation_<id> (если вытащили id), иначе просто deviantart.com
    """
    if canonical_url:
        u = urlparse(canonical_url)
        netloc = u.netloc  # deviantart.com
        path = u.path.strip("/")
        token_path = path.replace("/", "_")
        return f"{netloc}_{token_path}" if token_path else netloc
    if fallback_id:
        return f"deviantart.com_deviation_{fallback_id}"
    return "deviantart.com"


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


def make_filename(token: str, title: str, tags: List[str]) -> str:
    # квадратные скобки — теги (через пробел), круглые — токен-ссылка
    safe_title = sanitize_filename(title)[:120] or "deviation"
    tag_block = " ".join(tags) if tags else ""
    return f"[{tag_block}]({token}){safe_title}"


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def collect_all_images(sess: requests.Session, url: str) -> Tuple[str, str, List[str]]:
    """
    Возвращает (title, canonical_url, [image_urls...]) со страницы DeviantArt.
    Сначала meta, затем JSON; объединяем и убираем дубли.
    """
    soup = get_soup(sess, url)

    title1, canonical1, imgs1 = extract_from_meta(soup)
    title2, canonical2, imgs2 = try_extract_nextdata_all_images(soup)

    title = title1 or title2 or ""
    canonical = canonical1 or canonical2 or url

    images = unique_preserve_order([*(imgs1 or []), *(imgs2 or [])])

    # На крайний случай попробуем достать хоть что-то из <img> тегов
    if not images:
        for im in soup.find_all("img"):
            src = (im.get("src") or im.get("data-src") or "").strip()
            if IMG_EXT_RE.search(src):
                images.append(src)

    return title, canonical, images


def run_single(art_input: str, out_dir: Path, extra_tags: List[str], download_all: bool) -> List[Path]:
    """
    Скачивает одно «произведение»: одну или все картинки.
    Возвращает список сохранённых путей.
    """
    url = make_artwork_url(art_input)
    out_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as sess:
        title, canonical, images = collect_all_images(sess, url)

        if not images:
            raise SystemExit("Не удалось определить URL(ы) изображения со страницы.")

        token = build_token_from_canonical(canonical, parse_id(art_input))
        tags = (extra_tags or []) if extra_tags else DEFAULT_TAGS
        base_name = make_filename(token, title, tags)

        saved: List[Path] = []
        to_download = images if download_all else images[:1]

        for idx, img_url in enumerate(to_download):
            data = download_image(sess, img_url, referer_url=canonical)
            ext = guess_ext_from_url(img_url)
            suffix = f"_p{idx}" if download_all else None

            out_path = out_dir / ((base_name + (suffix or "")) + ext)
            i = 1
            final_path = out_path
            while final_path.exists():
                final_path = out_dir / f"{out_path.stem} ({i}){ext}"
                i += 1

            final_path.write_bytes(data)
            print(f"Saved: {final_path}")
            saved.append(final_path)

        return saved


# ----------------- CLI -----------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download DeviantArt image(s) with custom filename.\n"
            "Формат (предпочтительный):\n"
            "  deviantart_dl.py <ID|URL[,ID|URL,...]> [tags...] [--all] [--out DIR]\n"
            "Совместимость со старым форматом (--id/--url/--tags) сохранена.\n"
        )
    )

    # Позиционный режим
    parser.add_argument("inputs", nargs="?", help="ID/URL или список через запятую (позиционно)")
    parser.add_argument("tags_pos", nargs="*", help="Теги позиционно (несколько слов)")

    # Старые флаги (совместимость)
    parser.add_argument("--url", help="URL или список URL (через запятую)")
    parser.add_argument("--id", help="ID или список ID/URL (через запятую)")
    parser.add_argument("--tags", default="", help="Теги через пробел (флаг)")

    # Общие опции
    parser.add_argument("--out", default=str(OUTPUT_DIR), help="Выходная папка")
    parser.add_argument("--all", dest="download_all", action="store_true", help="Скачать все картинки со страницы")

    args = parser.parse_args()

    # 1) Источники ID/URL
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

    # 3) Готовим выходную папку
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 4) Обработка списка
    for tok in tokens:
        try:
            run_single(tok, out_dir, extra_tags, download_all)
        except Exception as e:
            print(f"[error] {tok}: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
