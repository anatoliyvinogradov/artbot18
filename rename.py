import re
import os
from pathlib import Path

# === Настройки ===
FOLDER = Path(r"./images")  # <- укажи свою папку
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
DRY_RUN = False  # True = только показать, что было бы переименовано

# Имя должно начинаться с: [что-угодно] (что-угодно)
START_PATTERN = re.compile(r'^\[[^\]]*\]\([^\)]*\)')

def has_required_prefix(name: str) -> bool:
    """
    Возвращает True, если имя начинается на [ ... ]( ... ) (контент может быть пустым).
    """
    return bool(START_PATTERN.match(name))

def make_unique_path(dir_path: Path, target_name: str) -> Path:
    """
    Если файл с таким именем уже существует — добавляем " (n)" перед расширением.
    """
    candidate = dir_path / target_name
    if not candidate.exists():
        return candidate

    stem, suffix = os.path.splitext(target_name)
    n = 1
    while True:
        candidate = dir_path / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1

def main():
    if not FOLDER.exists():
        print(f"Папка не найдена: {FOLDER}")
        return

    to_rename = []

    for p in FOLDER.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALLOWED_EXT:
            continue

        name = p.name
        if has_required_prefix(name):
            # Уже ок — пропускаем
            continue

        new_name = f"[](){name}"
        new_path = make_unique_path(p.parent, new_name)
        to_rename.append((p, new_path))

    if not to_rename:
        print("Все файлы уже корректно оформлены, переименований не требуется.")
        return

    print("Найдено файлов для переименования:", len(to_rename))
    for old, new in to_rename:
        print(f"- {old.name}  ->  {new.name}")

    if DRY_RUN:
        print("\nDRY_RUN=True: переименования НЕ выполнялись.")
        return

    # Выполняем переименования
    for old, new in to_rename:
        old.rename(new)

    print("\nГотово: переименования выполнены.")

if __name__ == "__main__":
    main()
