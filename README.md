# nestegg-locator

Lagersuche für Home Assistant auf Basis des CSV-Exports der iPhone-App **Nest Egg**.
Fragen per Telegram:

```
wo ist galaxy quest   →  🔎 2 Treffer für „galaxy quest“:
                         📦 Galaxy Quest (DVD) → Kiste 3 · Keller
                         📦 Galaxy Quest (Bluray) → Kiste 3 · Keller
kiste 6               →  kompletter Inhalt, nach Kategorie gruppiert
lager                 →  Übersicht: Items pro Kiste/Ort
```

Freie Fragen („habe ich was von Tolkien eingelagert?“) beantwortet der KI-Agent über
`script.lager_suche`. Ein neuer Export wird einfach aus Nest Egg per *Teilen → Telegram*
an den HA-Bot geschickt und ersetzt automatisch den alten.

Bewusst klein gehalten: ein Python-Skript (nur Standardbibliothek), eine CSV, keine
Datenbank, keine Custom Integration.

## Aufbau

```
Telegram ─► Node-RED (Bridge) ─┬─ Text ─► Event telegram_text ─► Automation „Smart Router“
                               │                                   └─ wo ist / kiste / lager ─► shell_command.lager_suche
                               └─ Dokument (.csv/.zip) ─► /homeassistant/nestegg/upload/upload.bin
                                                       ─► Event nestegg_csv_uploaded ─► Automation „Nest Egg: Import“
                                                                                         └─ shell_command.lager_import
KI-Agent (Assist) ─► script.lager_suche ─► shell_command.lager_suche
```

| Pfad auf HA | Inhalt |
|---|---|
| `/config/nestegg-lager/` | dieses Repo |
| `/config/nestegg-lager/config.json` | optionale lokale Konfiguration (nicht im Git) |
| `/config/nestegg/inventar.csv` | aktuelles Inventar |
| `/config/nestegg/inventar_alt.csv` | Sicherung vor dem letzten Import |
| `/config/nestegg/upload/upload.bin` | zuletzt per Telegram hochgeladene Datei |

## Installation

In der App **Terminal & SSH**:

```sh
git clone https://github.com/gman-k8s/nestegg-locator.git /config/nestegg-lager
mkdir -p /config/nestegg/upload
python3 /config/nestegg-lager/lager_suche.py --import /pfad/zum/Export.csv.zip   # erster Import
```

Den ersten Export kann man auch per Telegram schicken, sobald der Upload-Flow läuft.

### shell_commands

Unter dem bestehenden `shell_command:`-Block ergänzen, danach
*Entwicklerwerkzeuge → YAML → Shell-Befehle neu laden*:

```yaml
  lager_suche: "python3 /config/nestegg-lager/lager_suche.py -- {{ text | replace('\"', ' ') | replace(\"'\", ' ') }}"
  lager_import: "python3 /config/nestegg-lager/lager_suche.py --import /config/nestegg/upload/upload.bin"
```

Anführungszeichen werden entfernt, weil HA die Argumente mit `shlex` zerlegt – ein
Apostroph („Ocean's Eleven“) würde den Aufruf sonst abbrechen. Die Shell wird bei
Templates nicht benutzt, Sonderzeichen sind also unkritisch.

### Node-RED

`nodered/csv_upload_flow.json` importieren (Menü → Importieren). Der Flow erwartet die
bestehende Telegram-Bridge (Node `telegram receiver`, Token in `global.telegram_token`):

1. Im Function-Node **Set Tokens** ergänzen:
   `global.set('nestegg_owner_id', <deine Telegram user_id>);`
2. Im Switch **Type?** eine Regel `== document` hinzufügen und **über** „sonst“ ziehen;
   den neuen Ausgang mit **Dokument → Nest-Egg-Upload?** verbinden.
3. Deploy.

Der Zielpfad ist `/homeassistant/nestegg/upload/upload.bin` (dort ist das HA-Config-
Verzeichnis im Node-RED-Container gemountet). Abweichend per
`global.set('nestegg_upload_path', '…')` setzen.

## Update

```sh
cd /config/nestegg-lager && git pull
```

Inventar und `config.json` liegen außerhalb des Repos bzw. sind ignoriert.

## CSV-Upload

1. Nest Egg → Exportieren → CSV → Teilen → Telegram → HA-Bot.
2. Der Bot antwortet z. B. `✅ 389 Items in 5 Kisten (+ 5 weitere Orte) importiert (vorher 380).`
3. Ungültige Dateien werden abgelehnt (`❌ …`), das alte Inventar bleibt erhalten.

Akzeptiert wird das Nest-Egg-ZIP (daraus wird `Artikel.csv` genommen) oder eine
einzelne CSV. Encoding (UTF-8, UTF-8-BOM, cp1252) und Trennzeichen (`,` `;` Tab)
werden erkannt.

## Kommandozeile

```sh
python3 lager_suche.py wo ist galaxy quest
python3 lager_suche.py kiste 4
python3 lager_suche.py lager
python3 lager_suche.py --json tolkien
python3 lager_suche.py "hobbit; herr der ringe"   # mehrere Begriffe, je ein Abschnitt
python3 lager_suche.py --import upload.bin
python3 lager_suche.py --data /anderer/pfad/inventar.csv starcraft
```

Nest Egg speichert keine Autoren oder Regisseure. Der KI-Agent soll bei solchen Fragen
deshalb nach konkreten Titeln suchen, mehrere auf einmal mit `;` getrennt. Das steht in der
Beschreibung von `script.lager_suche`, die das Modell als Tool-Beschreibung sieht.

Unscharfe Treffer (Tippfehler) brauchen denselben Anfangsbuchstaben und eine Ähnlichkeit
von mindestens `fuzzy_cutoff` (Standard 0.8).

Exit-Code 0 mit Antwort auf stdout; bei Fehlern (kein Inventar, ungültiger Import)
Exit-Code 1 und Meldung auf stderr.

## Quirks mit reveries (optional)

Liegt [reveries](https://github.com/gman-k8s/reveries) unter `/config/reveries` (also neben
diesem Repo), bekommen eindeutige Suchtreffer gelegentlich eine sarkastische Antwort:

```
wo ist indiana jones 4  →  Wovon redest du? Es gibt nur drei Indiana-Jones-Filme.
                           Aber wenn du dir den Tag versauen willst: Kiste 4.
```

```sh
git clone https://github.com/gman-k8s/reveries.git /config/reveries
```

- Nur bei der Suche, nur wenn alle Treffer in derselben Kiste liegen und nicht unscharf
  gefunden wurden. `kiste N`, `lager`, `--json` und mehrere Begriffe mit `;` bleiben sachlich.
- Fakten für die Vorlagen: `{kiste}` (Nummer), `{ort}` (z. B. Keller), `{titel}`.
- Fehlt reveries oder PyYAML, oder ist eine Quirk-Datei kaputt, antwortet die Suche wie
  bisher. Die Kistennummer steht in jeder Quirk-Antwort, dafür sorgt reveries.
- `--sachlich` schaltet Quirks für einen Aufruf ab, z. B. für den KI-Agenten:
  `lager_suche_sachlich: "python3 /config/nestegg-lager/lager_suche.py --sachlich -- {{ … }}"`.
- Abweichender Pfad über `"reveries": {"dir": "/pfad"}` in `config.json`, abschalten mit
  `"dir": false`. Ein LLM für frei formulierte Antworten (nur bei einem Treffer, mit
  Fakten-Check) lässt sich dort ebenfalls eintragen:
  `"llm": {"type": "openai", "url": "http://vllm:8000/v1", "model": "…", "share": 0.4}`
  oder `{"type": "gemini", "model": "…", "api_key": "…"}`.

## Konfiguration

Ohne `config.json` gelten die Standardwerte für den deutschen Nest-Egg-Export.
Vorlage: `config.example.json`. Spalten werden als Liste möglicher Namen angegeben,
die erste vorhandene gewinnt – so funktioniert auch ein englischer Export.

## Tests

```sh
python3 -m unittest discover -s tests
```
