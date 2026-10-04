# Backup, Umzug & Neuinstallation

## Was muss gesichert werden?

| Volume (Compose-Name) | Volume-Name auf dem Host* | Inhalt | Sichern |
|---|---|---|:-:|
| `data` | `<stack>_data` | `ai/` (KI-Texte), `ai_config.json` (KI-Einstellungen + API-Keys), `steam/` (Steam-Cache), `appids.json`, `mapping.json` | **ja** |
| `html` | `<stack>_html` | veröffentlichte Seite | optional – lässt sich jederzeit aus dem Playnite-Export neu erzeugen |
| `conf` | `<stack>_conf` | nginx-Konfiguration | nein – wird beim Start neu geschrieben |

\* `<stack>` = Name des Portainer-Stacks bzw. des Compose-Projekts (Ordnername). Anzeigen: `docker volume ls | grep _data`

Zusätzlich sichern: deine Stack-Variablen (`.env` bzw. Portainer-Environment) – vor allem `UPDATE_TOKEN`.

## Backup erstellen

Auf dem Docker-Host (Volume-Name anpassen):

```bash
docker run --rm -v playnite-game-library_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/gamelibrary-data-$(date +%F).tgz -C /data .
```

Tipp: In Proxmox sichert ein Backup des LXC/VM-Containers (vzdump) die Docker-Volumes automatisch mit.

## Neuinstallation (neuer Server / Neuaufsetzen)

1. Stack aus diesem Repo neu anlegen (siehe [README → Installation](../README.md#installation-mit-portainer)) – **gleiches `UPDATE_TOKEN`** und gleicher `SITE_TITLE` wie vorher.
2. Stack einmal starten, damit die Volumes angelegt werden, dann den Updater stoppen:
   ```bash
   docker stop $(docker ps -qf "name=updater")
   ```
3. Backup in das neue `data`-Volume zurückspielen:
   ```bash
   docker run --rm -v playnite-game-library_data:/data -v "$PWD":/backup alpine \
     sh -c "cd /data && tar xzf /backup/gamelibrary-data-YYYY-MM-DD.tgz && chmod 600 ai_config.json 2>/dev/null; ls"
   ```
4. Stack wieder starten (Portainer: *Start* bzw. `docker compose up -d`).
5. Playnite-Export **einmal neu importieren** (Button oder Skript). Dank des Caches geht das schnell; alle KI-Texte werden automatisch wieder zugeordnet.

Ohne Backup funktioniert alles genauso – Steam-Daten werden dann neu geladen und KI-Texte müssen neu erzeugt werden.

## Bestehende Installation übernehmen (alte Struktur)

Ältere Installationen hatten Code und Daten gemeinsam in einem Volume (gemountet als `/backup`, z. B. `enkis-games-backup`). Die Datennamen sind identisch, deshalb genügt es, den Inhalt in das neue `data`-Volume zu kopieren:

```bash
# alt -> neu (Volume-Namen anpassen)
docker run --rm -v enkis-games-backup:/old:ro -v playnite-game-library_data:/data alpine sh -c \
  "cp -a /old/ai /old/steam /old/appids.json /old/mapping.json /data/ 2>/dev/null; \
   cp -a /old/ai_config.json /data/ 2>/dev/null; ls -la /data"
```

Danach den neuen Stack starten und den Export einmal importieren. Läuft der neue Stack parallel zum alten, einen anderen `WEB_PORT` wählen und den Reverse-Proxy erst nach erfolgreichem Test umstellen.

Alternativ kann eine bestehende Installation ohne Umzug weiterlaufen: Der Updater läuft auch mit `DATA_DIR=/backup`, wenn `updater.py`, `gen.py` und `app.html` in diesem Volume liegen.

## Cover / Steam-Zuordnung korrigieren

Spiele ohne Steam-Link werden per Namenssuche einem Steam-Spiel zugeordnet. Falsche Treffer korrigieren:

1. Richtige Steam-AppID ermitteln (aus der Store-URL `store.steampowered.com/app/<AppID>/…`).
2. Spiel-ID ermitteln: Detailansicht öffnen – die URL endet auf `#game/<Spiel-ID>`.
3. Im Daten-Volume anpassen:
   - `appids.json`: `"<Spiel-ID>": <AppID>` (oder `null`, um keine Steam-Daten zu verwenden)
   - `mapping.json`: `"<Spiel-ID>": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/<AppID>/library_600x900.jpg"` (nur nötig, wenn das Cover fehlte)
   - zugehörige Datei `ai/<Spiel-ID>.json` löschen, falls der KI-Text zum falschen Spiel gehört
4. Export erneut importieren, danach ggf. den KI-Text für das Spiel neu erzeugen.

Bearbeiten z. B. so:
```bash
docker run --rm -it -v playnite-game-library_data:/data alpine vi /data/appids.json
```

Hat ein Spiel in Playnite einen Link zum Steam-Store, wird immer diese AppID verwendet.
