"""Best-effort extraction of CPU / RAM / storage from messy OLX listing text.

People describe specs in wildly different ways, e.g.:
    i5-1135G7 | 16Gb DDR4 | 256Gb M2
    Ryzen 5 PRO 4650U/16/256
    Core Ultra 5 125U 16GB DDR5 512GB SSD
    16gb RAM_1000gb SSD
    32 RAM, 1TB
    SSD 240 Gb / nvme256

Everything here is heuristic and returns None when nothing is confident.
"""

import re

RAM_VALUES = {2, 3, 4, 6, 8, 12, 16, 20, 24, 32, 36, 48, 64, 96, 128}


def _norm_storage(num: int) -> str:
    """Render a GB storage figure, collapsing ~1000/2000 to TB."""
    if 990 <= num <= 1100:
        return "1TB"
    if 1990 <= num <= 2100:
        return "2TB"
    return f"{num}GB"


def parse_cpu(text: str) -> str | None:
    t = text
    # Intel Core Ultra (e.g. "Core Ultra 5 125U", "Ultra 7")
    m = re.search(r"\bUltra\s*([3579])\s*(\d{3}[A-Za-z]{0,2})?", t, re.I)
    if m:
        model = f" {m.group(2).upper()}" if m.group(2) else ""
        return f"Ultra {m.group(1)}{model}"
    # AMD Ryzen (e.g. "Ryzen 5 PRO 4650U", "Ryzen 5-7530U", "Ryzen 3 4300U")
    m = re.search(r"\bRyzen\s*([3579])\s*(pro)?\s*[\s\-]?(\d{4}[A-Za-z]{0,2})?", t, re.I)
    if m:
        pro = " Pro" if m.group(2) else ""
        model = f" {m.group(3).upper()}" if m.group(3) else ""
        return f"Ryzen {m.group(1)}{pro}{model}"
    # Intel Core iX with a model number (e.g. "i5-1135G7", "i7 10510u", "i3-1115G4")
    m = re.search(r"\b(i[3579])[\s\-]?(\d{4,5}(?:[a-z]\d|[a-z])?)\b", t, re.I)
    if m:
        return f"{m.group(1).lower()}-{m.group(2).upper()}"
    # Bare Intel iX (e.g. "Core i7", "i5 4 ядра")
    m = re.search(r"\b(i[3579])\b", t, re.I)
    if m:
        return m.group(1).lower()
    return None


def _shorthand(text: str):
    """Parse the 'RAM/Storage' shorthand like 16/512, 8/256, 16/1tb.

    Returns (ram_str, storage_str) with either possibly None.
    """
    # 16/1tb style
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d(?:[.,]\d)?)\s*(?:tb|тб)\b", text, re.I)
    if m and int(m.group(1)) in RAM_VALUES:
        return f"{int(m.group(1))}GB", f"{m.group(2).replace(',', '.').rstrip('.0') or '1'}TB"
    # 16/512 style (small / big); trailing (?!\d) so "256Gb" still matches but "2560" won't
    for m in re.finditer(r"\b(\d{1,2})\s*/\s*(\d{3,4})(?!\d)", text):
        ram, sto = int(m.group(1)), int(m.group(2))
        if ram in RAM_VALUES and sto >= 120:
            return f"{ram}GB", _norm_storage(sto)
    return None, None


def parse_ram(text: str) -> str | None:
    ram, _ = _shorthand(text)
    if ram:
        return ram
    # explicit "16 RAM", "RAM 16"
    m = re.search(r"\b(\d{1,2})\s*(?:gb|гб)?\s*ram\b", text, re.I) or \
        re.search(r"\bram\s*[:_-]?\s*(\d{1,2})\b", text, re.I) or \
        re.search(r"\b(\d{1,2})\s*(?:gb|гб)\s*ddr", text, re.I)
    if m and int(m.group(1)) in RAM_VALUES:
        return f"{int(m.group(1))}GB"
    # first NN GB whose value is a plausible RAM size (and isn't a 3+ digit storage)
    for m in re.finditer(r"\b(\d{1,2})\s*(?:gb|гб)\b", text, re.I):
        if int(m.group(1)) in RAM_VALUES:
            return f"{int(m.group(1))}GB"
    return None


def parse_storage(text: str) -> str | None:
    _, sto = _shorthand(text)
    if sto:
        return sto
    # TB form: "1TB", "1 ТБ", "2tb"
    m = re.search(r"\b(\d(?:[.,]\d)?)\s*(?:tb|тб)\b", text, re.I)
    if m:
        val = m.group(1).replace(",", ".")
        val = val.rstrip("0").rstrip(".") if "." in val else val
        return f"{val}TB"
    # number next to a storage keyword: "512 GB NVME", "SSD 240 Gb", "nvme256", "SSD 256"
    m = re.search(r"\b(\d{3,4})\s*(?:gb|гб)?\s*(?:ssd|nvme|hdd|m\.?2|emmc|emmc)\b", text, re.I) or \
        re.search(r"\b(?:ssd|nvme|hdd|m\.?2|emmc)\s*(\d{3,4})\b", text, re.I)
    if m:
        return _norm_storage(int(m.group(1)))
    # plain "256Gb", "1000gb" with a storage-plausible value
    for m in re.finditer(r"\b(\d{3,4})\s*(?:gb|гб)\b", text, re.I):
        if int(m.group(1)) >= 120:
            return _norm_storage(int(m.group(1)))
    return None


def parse_specs(*texts: str) -> dict:
    """Parse specs, trying each text in order and keeping the first hit per field."""
    out = {"cpu": None, "ram": None, "storage": None}
    for text in texts:
        if not text:
            continue
        out["cpu"] = out["cpu"] or parse_cpu(text)
        out["ram"] = out["ram"] or parse_ram(text)
        out["storage"] = out["storage"] or parse_storage(text)
        if all(out.values()):
            break
    return out
