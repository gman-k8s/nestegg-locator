#!/usr/bin/env python3
"""Lagersuche über einen Nest-Egg-CSV-Export.

Suchen:      lager_suche.py wo ist galaxy quest
             lager_suche.py kiste 4
             lager_suche.py --json tolkien
Importieren: lager_suche.py --import /config/nestegg/upload/upload.bin

Nur Standardbibliothek, damit es per shell_command im HA-Core-Container läuft.
"""

import argparse
import copy
import csv
import difflib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    # None → $NESTEGG_DATA oder <repo>/../nestegg/inventar.csv
    "data_file": None,
    # Pro Feld eine Liste möglicher Spaltennamen; die erste vorhandene gewinnt.
    "columns": {
        "name": ["Name"],
        "description": ["Beschreibung", "Description"],
        "category": ["Kategorie", "Category"],
        "location": ["Ort", "Location"],
        "quantity": ["Menge", "Quantity"],
        "notes": ["Bemerkungen", "Notes"],
        "extra": ["Hersteller", "Modell", "Strichcode", "Manufacturer", "Model", "Barcode"],
    },
    # Dateiname der Artikelliste im Nest-Egg-ZIP
    "zip_members": ["Artikel.csv", "Items.csv"],
    "location_separator": "»",
    "container_word": "Kiste",
    "unknown_locations": ["Unbekannt", "Unknown"],
    "max_chars": 4000,
    # Optionale Quirks über reveries (https://github.com/gman-k8s/reveries).
    # dir: None → $REVERIES_DIR oder <repo>/../reveries; false → aus.
    # llm: None oder {"type": "openai"|"gemini", "url": …, "model": …, "api_key": …, "share": 0.4}
    "reveries": {"dir": None, "llm": None},
    "fuzzy_cutoff": 0.8,
    "category_icons": {
        "Filme": "🎬",
        "Spiele": "🎮",
        "Bücher": "📚",
        "Musik": "🎵",
        "Elektronik": "🔌",
        "Hobbies": "🧰",
        "Essen & Trinken": "🥫",
        "Kunst & Handwerk": "🎨",
        "Unkategorisiert": "🗃️",
    },
}

REQUIRED_FIELDS = ("name", "location")
STOPWORDS = {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "mein", "meine",
             "meinen", "und", "von", "the", "a", "an", "of"}
SEARCH_PREFIX = re.compile(
    r"^(?:wo\s+(?:ist|sind|liegt|liegen|steht|stehen|finde\s+ich)|lager(?:suche)?|suche?)\b\s*:?\s*(.*)$"
)
BOX_PREFIX = re.compile(r"^kiste\b\s*(.*)$")
OVERVIEW = {"lager", "kisten", "kiste", "lagersuche"}
UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


class InventoryError(Exception):
    """Inventar fehlt oder ist ungültig – Text ist für den Nutzer gedacht."""


def normalize(text):
    text = text.lower().translate(UMLAUTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


@dataclass
class Item:
    name: str
    location: str
    description: str = ""
    category: str = ""
    quantity: str = ""
    notes: str = ""
    extra: str = ""
    separator: str = "»"
    unknown: bool = False
    haystack: str = field(default="", repr=False)
    name_norm: str = field(default="", repr=False)

    @property
    def segments(self):
        return [s.strip() for s in self.location.split(self.separator) if s.strip()]

    @property
    def place(self):
        segments = self.segments
        return segments[0] if segments else ""

    @property
    def container(self):
        return f" {self.separator} ".join(self.segments[1:])

    def where(self):
        if self.unknown:
            return "❓ Ort unbekannt"
        if self.container:
            return f"{self.container} · {self.place}"
        return self.place

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "location": self.location,
            "place": self.place,
            "container": self.container,
            "quantity": self.quantity,
            "notes": self.notes,
        }


# ---------------------------------------------------------------- Konfiguration

def load_config(path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path is None:
        return config
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        user = json.load(fh)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    if config["data_file"] and not os.path.isabs(config["data_file"]):
        config["data_file"] = str(path.parent / config["data_file"])
    return config


def data_path(config):
    if config.get("data_file"):
        return Path(config["data_file"])
    if os.environ.get("NESTEGG_DATA"):
        return Path(os.environ["NESTEGG_DATA"])
    return SCRIPT_DIR.parent / "nestegg" / "inventar.csv"


# ---------------------------------------------------------------- Einlesen

def decode(data):
    if not data.strip():
        raise InventoryError("Die Datei ist leer.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def detect_delimiter(text):
    header = text.split("\n", 1)[0]
    counts = {d: header.count(d) for d in (",", ";", "\t")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


def resolve_columns(header, config):
    present = {h.strip(): h for h in header}
    mapping = {}
    for key, candidates in config["columns"].items():
        found = [present[c] for c in candidates if c in present]
        mapping[key] = found if key == "extra" else (found[0] if found else None)
    missing = [k for k in REQUIRED_FIELDS if mapping[k] is None]
    if missing:
        wanted = ", ".join("/".join(config["columns"][k]) for k in missing)
        raise InventoryError(f"Spalte fehlt: {wanted}. Gefunden: {', '.join(present) or '–'}")
    return mapping


def parse_inventory(data, config):
    text = decode(data)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=detect_delimiter(text))
    mapping = resolve_columns(reader.fieldnames or [], config)
    unknown = {normalize(u) for u in config["unknown_locations"]}
    separator = config["location_separator"]

    def get(row, key):
        column = mapping.get(key)
        return (row.get(column) or "").strip() if column else ""

    items = []
    for row in reader:
        name = get(row, "name")
        if not name:
            continue
        item = Item(
            name=name,
            location=get(row, "location"),
            description=get(row, "description"),
            category=get(row, "category"),
            quantity=get(row, "quantity"),
            notes=get(row, "notes"),
            extra=" ".join((row.get(c) or "").strip() for c in mapping["extra"]),
            separator=separator,
        )
        item.unknown = normalize(item.location) in unknown or not item.location
        item.name_norm = normalize(item.name)
        item.haystack = normalize(" ".join(
            [item.name, item.description, item.category, item.location, item.notes, item.extra]))
        items.append(item)
    if not items:
        raise InventoryError("Die Datei enthält keine Artikel.")
    return items


def load_inventory(path, config):
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise InventoryError(
            f"Kein Inventar gefunden ({path}). Schick mir den Nest-Egg-Export per Telegram.")
    return parse_inventory(data, config)


# ---------------------------------------------------------------- Anfrage

def parse_request(text):
    text = " ".join(text.lower().strip().strip("?!.").split())
    if text in OVERVIEW:
        return "overview", ""
    match = BOX_PREFIX.match(text)
    if match:
        return ("box", match.group(1).strip()) if match.group(1).strip() else ("overview", "")
    match = SEARCH_PREFIX.match(text)
    if match:
        text = match.group(1).strip()
    words = text.split()
    while len(words) > 1 and words[0] in STOPWORDS:
        words.pop(0)
    return "search", " ".join(words)


# ---------------------------------------------------------------- Suche

@dataclass
class Hit:
    score: float
    item: Item


@dataclass
class SearchResult:
    query: str
    hits: list
    fuzzy: bool = False
    suggestions: list = field(default_factory=list)


def query_tokens(query_norm):
    tokens = query_norm.split()
    meaningful = [t for t in tokens if t not in STOPWORDS]
    return meaningful or tokens


def stems(token):
    """Token plus einfache Pluralformen ohne Endung (netzteile → netzteil)."""
    forms = [token]
    for ending in ("en", "er", "e", "n", "s"):
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            forms.append(token[: -len(ending)])
    return forms


def contains(text, token, word_start=True):
    """Token (oder Pluralstamm) im Text – standardmäßig nur am Wortanfang."""
    if word_start:
        return any(f" {form}" in f" {text}" for form in stems(token))
    return any(form in text for form in stems(token))


def exact_score(item, query_norm, tokens):
    if item.name_norm == query_norm:
        return 100
    if item.name_norm.startswith(query_norm):
        return 90
    if f" {query_norm}" in f" {item.name_norm}":
        return 80
    if all(contains(item.name_norm, t) for t in tokens):
        return 70
    if all(contains(item.haystack, t) for t in tokens):
        return 60
    # Wortinneres, z. B. „kanal“ in „Kabelkanal“ – nur wenn sonst nichts passt
    if all(contains(item.haystack, t, word_start=False) for t in tokens):
        return 40
    return 0


def similar(a, b):
    """Ähnlichkeit 0..1; Tippfehler am Wortanfang sind selten, daher muss der erste Buchstabe passen."""
    if not a or not b or a[0] != b[0]:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def fuzzy_score(item, query_norm, tokens, cutoff):
    whole = similar(query_norm, item.name_norm)
    words = item.haystack.split()
    per_token = []
    for token in tokens:
        if token in item.haystack:
            per_token.append(1.0)
        elif len(token) < 4:
            per_token.append(0.0)
        else:
            per_token.append(max(similar(token, w) for w in words))
    best = max(whole, min(per_token))
    return best * 50 if best >= cutoff else 0


def suggestions(items, query_norm, limit=3, cutoff=0.6):
    """Namen, bei denen jedes Suchwort einem Wort im Namen zumindest ähnelt."""
    by_norm = {}
    for item in items:
        by_norm.setdefault(item.name_norm, item.name)
    tokens = query_tokens(query_norm)
    scored = []
    for name_norm, name in by_norm.items():
        if name_norm[:1] != query_norm[:1]:
            continue
        words = name_norm.split()
        per_token = [max(difflib.SequenceMatcher(None, t, w).ratio() for w in words) for t in tokens]
        whole = difflib.SequenceMatcher(None, query_norm, name_norm).ratio()
        if min(per_token) >= cutoff and whole >= cutoff - 0.1:
            scored.append((sum(per_token) / len(per_token) + whole, name))
    return [name for _, name in sorted(scored, key=lambda s: -s[0])[:limit]]


def search(items, query, cutoff=DEFAULT_CONFIG["fuzzy_cutoff"]):
    query_norm = normalize(query)
    if not query_norm:
        return SearchResult(query, [])
    tokens = query_tokens(query_norm)

    def rank(hits):
        return sorted(hits, key=lambda h: (-h.score, h.item.name_norm, h.item.location))

    hits = [Hit(s, i) for i in items if (s := exact_score(i, query_norm, tokens))]
    if any(h.score >= 60 for h in hits):
        hits = [h for h in hits if h.score >= 60]
    if hits:
        return SearchResult(query, rank(hits))
    hits = [Hit(s, i) for i in items if (s := fuzzy_score(i, query_norm, tokens, cutoff))]
    if hits:
        return SearchResult(query, rank(hits), fuzzy=True)
    return SearchResult(query, [], suggestions=suggestions(items, query_norm))


def box_matches(item, wanted_norm, config):
    segments = [normalize(s) for s in item.segments[1:]]
    if wanted_norm.isdigit():
        number = str(int(wanted_norm))
        word = normalize(config["container_word"])
        return any(s in (f"{word} {number}", number) for s in segments)
    return any(wanted_norm in s for s in segments)


def find_box(items, wanted, config):
    wanted_norm = normalize(wanted)
    if wanted_norm.startswith(normalize(config["container_word"]) + " "):
        wanted_norm = wanted_norm.split(" ", 1)[1]
    groups = {}
    for item in items:
        if not item.unknown and wanted_norm and box_matches(item, wanted_norm, config):
            groups.setdefault(item.location, []).append(item)
    return groups


# ---------------------------------------------------------------- Ausgabe

def item_label(item):
    label = item.name
    if item.description:
        label += f" ({item.description})"
    if item.quantity not in ("", "1"):
        label += f" ×{item.quantity}"
    return label


def note_suffix(item):
    return f" — 📝 {item.notes}" if item.notes else ""


def hit_line(item):
    return f"📦 {item_label(item)} → {item.where()}{note_suffix(item)}"


def fit(header, lines, max_chars):
    """Header + so viele Zeilen wie passen, Rest als „… und X weitere“."""
    out = [header] if header else []
    length = len(header)
    for index, line in enumerate(lines):
        rest = len(lines) - index - 1
        reserve = len(f"\n… und {len(lines)} weitere") if rest else 0
        if length + 1 + len(line) + reserve > max_chars:
            out.append(f"… und {len(lines) - index} weitere")
            break
        out.append(line)
        length += 1 + len(line)
    return "\n".join(out)


def location_title(location, items):
    return items[0].where() if items else location


def format_search(result, config):
    if not result.hits:
        text = f"🤷 Nichts gefunden für „{result.query}“."
        if result.suggestions:
            text += "\nMeintest du: " + ", ".join(result.suggestions) + "?"
        return text
    lines = [hit_line(h.item) for h in result.hits]
    if result.fuzzy:
        header = f"🔎 Keine exakten Treffer für „{result.query}“ – ähnlich:"
    elif len(lines) == 1:
        return lines[0]
    else:
        header = f"🔎 {len(lines)} Treffer für „{result.query}“:"
    return fit(header, lines, config["max_chars"])


def format_box(groups, wanted, items, config):
    if not groups:
        boxes = sorted({i.container for i in items if i.container and not i.unknown},
                       key=natural_key)
        return f"🤷 Keine Kiste „{wanted}“ gefunden.\nVorhanden: " + ", ".join(boxes)
    icons = config["category_icons"]
    blocks = []
    for location, members in sorted(groups.items(), key=lambda kv: natural_key(kv[0])):
        lines = []
        by_category = {}
        for item in members:
            by_category.setdefault(item.category or "Ohne Kategorie", []).append(item)
        for category in sorted(by_category, key=normalize):
            lines.append(f"{icons.get(category, '🏷️')} {category}")
            for item in sorted(by_category[category], key=lambda i: i.name_norm):
                lines.append(f"• {item_label(item)}{note_suffix(item)}")
        header = f"📦 {location_title(location, members)} ({len(members)} Items)"
        blocks.append((header, lines))
    budget = config["max_chars"] // len(blocks)
    return "\n\n".join(fit(h, lines, budget) for h, lines in blocks)


def natural_key(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", normalize(text))]


def format_overview(items, config):
    counts = {}
    for item in sorted(items, key=lambda i: (i.unknown, natural_key(i.location))):
        key = "❓ Ort unbekannt" if item.unknown else item.where()
        counts[key] = counts.get(key, 0) + 1
    lines = [f"{where}: {n}" for where, n in counts.items()]
    return fit(f"📦 Lager: {len(items)} Items", lines, config["max_chars"])


def answer_many(parts, items, config, as_json):
    """Mehrere Begriffe, getrennt durch „;“ – z. B. vom KI-Agenten: „hobbit; herr der ringe“."""
    if as_json:
        return json.dumps([json.loads(answer(p, items, config, True)) for p in parts], ensure_ascii=False)
    budget = dict(config, max_chars=max(200, config["max_chars"] // len(parts) - 2))
    return "\n\n".join(answer(p, items, budget) for p in parts)


def answer(text, items, config, as_json=False, quirks=False):
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > 1:
        return answer_many(parts, items, config, as_json)
    mode, arg = parse_request(text)
    if mode == "box":
        groups = find_box(items, arg, config)
        if as_json:
            found = [i for members in groups.values() for i in members]
            return dump_json(mode, arg, found)
        return format_box(groups, arg, items, config)
    if mode == "overview":
        return dump_json(mode, arg, items) if as_json else format_overview(items, config)
    result = search(items, arg, config["fuzzy_cutoff"])
    if as_json:
        return dump_json(mode, arg, [h.item for h in result.hits],
                         fuzzy=result.fuzzy, suggestions=result.suggestions)
    plain = format_search(result, config)
    return with_reveries(text, result, plain, config) if quirks else plain


# ---------------------------------------------------------------- reveries (optional)

def reveries_dir(config):
    configured = (config.get("reveries") or {}).get("dir")
    if configured is False or configured == "":
        return None
    if configured:
        return Path(configured)
    if "REVERIES_DIR" in os.environ:
        return Path(os.environ["REVERIES_DIR"]) if os.environ["REVERIES_DIR"] else None
    return SCRIPT_DIR.parent / "reveries"


def import_reveries(path):
    """reveries aus dem geklonten Ordner; None, wenn nicht da – die Suche bleibt dann sachlich."""
    if path is None or not path.is_dir():
        return None
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    try:
        import reveries
    except Exception as exc:  # z. B. PyYAML fehlt
        print(f"reveries nicht verfügbar: {exc!r}", file=sys.stderr)
        return None
    return reveries


def reveries_llm(reveries, settings):
    if not settings:
        return None
    from reveries import llm
    if settings.get("type") == "gemini":
        return llm.gemini(settings["model"], settings["api_key"])
    return llm.openai_compatible(settings["url"], settings.get("model", "default"),
                                 api_key=settings.get("api_key"))


def with_reveries(question, result, plain, config):
    """Würzt eine Suchantwort mit einem Quirk – nur bei eindeutigen Treffern in einer Kiste."""
    if result.fuzzy or not result.hits:
        return plain
    items = [h.item for h in result.hits]
    if any(i.unknown for i in items) or len({i.location for i in items}) != 1:
        return plain
    path = reveries_dir(config)
    reveries = import_reveries(path)
    if reveries is None:
        return plain
    try:
        item = items[0]
        word = re.escape(config["container_word"])
        number = re.fullmatch(rf"{word}\s+(\S+)", item.container, re.I)
        kiste = number.group(1) if number else item.container
        if not kiste:
            return plain
        settings = config["reveries"].get("llm") or {}
        return reveries.apply(
            plain=plain,
            facts={"kiste": kiste, "ort": item.place, "titel": item.name},
            project="nestegg",
            texts=[question] + [i.name for i in items],
            quirks=reveries.load(path / "quirks"),
            llm=reveries_llm(reveries, settings),
            llm_share=settings.get("share", 0.4),
            persona=reveries.load_persona(path / "persona.md"),
        )
    except Exception as exc:  # reveries darf die Suche nie kaputt machen
        print(f"reveries übersprungen: {exc!r}", file=sys.stderr)
        return plain


def dump_json(mode, query, items, **extra):
    data = {"mode": mode, "query": query, "total": len(items),
            "items": [i.to_dict() for i in items], **extra}
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------- Import

def extract_items_csv(src, config):
    """Liefert die Bytes der Artikel-CSV aus einer CSV- oder Nest-Egg-ZIP-Datei."""
    if not zipfile.is_zipfile(src):
        return Path(src).read_bytes()
    with zipfile.ZipFile(src) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.endswith("/")]
        preferred = [n for n in csvs if Path(n).name in config["zip_members"]]
        for name in preferred + [n for n in csvs if n not in preferred]:
            data = zf.read(name)
            try:
                parse_inventory(data, config)
                return data
            except InventoryError:
                continue
    raise InventoryError("Im ZIP ist keine Artikel-CSV mit Name/Ort-Spalten.")


def summary(items, config):
    word = normalize(config["container_word"])
    locations = {i.location for i in items}
    boxes = {i.location for i in items
             if any(normalize(s).startswith(word + " ") for s in i.segments[1:])}
    text = f"✅ {len(items)} Items in {len(boxes)} Kisten"
    others = len(locations) - len(boxes)
    if others:
        text += f" (+ {others} weitere Orte)"
    return text + " importiert"


def import_file(src, target, config):
    target = Path(target)
    data = extract_items_csv(src, config)
    items = parse_inventory(data, config)

    previous = None
    if target.exists():
        try:
            previous = len(load_inventory(target, config))
        except InventoryError:
            previous = None

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(target.stem + "_alt" + target.suffix)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-", suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists():
            shutil.copy2(target, backup)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    message = summary(items, config)
    if previous is not None:
        message += f" (vorher {previous})"
        if len(items) < previous / 2:
            message += f"\n⚠️ Deutlich weniger als vorher – alte Liste liegt in {backup.name}."
    return message + "."


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description="Lagersuche über den Nest-Egg-Export")
    parser.add_argument("text", nargs="*", help="Suchtext, z. B. 'wo ist galaxy quest' oder 'kiste 4'")
    parser.add_argument("--import", dest="import_src", metavar="DATEI",
                        help="Neue CSV/ZIP prüfen und als Inventar übernehmen")
    parser.add_argument("--json", action="store_true", help="Ergebnis als JSON")
    parser.add_argument("--sachlich", action="store_true", help="ohne reveries-Quirks antworten")
    parser.add_argument("--config", help="Pfad zur config.json (Standard: neben dem Skript)")
    parser.add_argument("--data", help="Pfad zur Inventar-CSV (überschreibt config)")
    args = parser.parse_args(argv)

    try:
        config_path = args.config or (SCRIPT_DIR / "config.json")
        config = load_config(config_path if Path(config_path).exists() else None)
        if args.data:
            config["data_file"] = args.data
        target = data_path(config)

        if args.import_src:
            print(import_file(args.import_src, target, config))
            return 0

        text = " ".join(args.text)
        if not text.strip():
            parser.error("Suchtext fehlt")
        print(answer(text, load_inventory(target, config), config, as_json=args.json,
                     quirks=not args.sachlich))
        return 0
    except InventoryError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"❌ Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
