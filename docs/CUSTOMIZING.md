# Anpassen

## Seitentitel und Icon

Über Umgebungsvariablen des Stacks – der Updater schreibt die Seite bei jedem Start mit diesen Werten neu:

```env
SITE_TITLE=Enki´s Games
SITE_ICON=🎮
```

| Umgebung | So übernehmen |
|----------|---------------|
| Portainer | Stack → *Environment variables* → Wert ändern → **Update the stack** |
| Docker Compose | `.env` ändern → `docker compose up -d` |
| nur Neustart | `docker compose restart updater` übernimmt Werte **nicht** – der Container muss neu erstellt werden (`up -d`) |

Danach die Seite im Browser neu laden (`Strg+F5`). Spieldaten, KI-Texte und Einstellungen bleiben erhalten.

> `SITE_ICON` sollte ein einzelnes Emoji sein. Sonderzeichen im Titel (`´`, `&`, `'`) sind erlaubt und werden korrekt maskiert.

## App-Symbol (Home-Bildschirm)

Standardmäßig erzeugt der Updater ein Gamepad-Symbol im Farbverlauf der Seite. Eigenes Symbol: quadratisches PNG (mind. 512×512 px, ohne Transparenz) als `icon.png` ins `data`-Volume kopieren und den Updater neu starten:

```bash
docker cp icon.png <updater-container>:/data/icon.png && docker restart <updater-container>
```

Auf bereits installierten Handys wird das neue Symbol erst nach erneutem Hinzufügen zum Home-Bildschirm übernommen. Der App-Name entspricht `SITE_TITLE` (über 15 Zeichen wird er unter dem Symbol gekürzt).

## Port

`WEB_PORT=8095` in `.env` bzw. in den Stack-Variablen ändern und den Stack neu deployen.

## Farben und Layout

Alle Farben stehen als CSS-Variablen am Anfang von [`app/app.html`](../app/app.html):

```css
:root{
  --bg:#0d0f14; --bg2:#141821; --panel:#191e29; --panel2:#202636; --line:#2a3142;
  --text:#e8ebf2; --muted:#9aa3b5; --dim:#6b7489; --accent:#7c5cff; --accent2:#22d3ee;
  --ok:#34d399; --warn:#fbbf24; --radius:14px; --side:270px;
  --card:168px;          /* Breite der Cover-Kacheln */
}
```

Beispiel für einen grünen Akzent: `--accent:#22c55e; --accent2:#a3e635;`

Nach Änderungen an `app/app.html`:

- **Portainer (Git-Stack):** Änderung committen/pushen → Stack → *Pull and redeploy*
- **Docker Compose:** `docker compose up -d --build`

Der Updater übernimmt die neue `app.html` beim Start automatisch – ein erneuter Import ist nicht nötig.

## Bibliotheksfarben

Die farbigen Punkte der Bibliotheken (Steam, Epic, GOG …) stehen in `app/app.html` in der Konstante `LIBCOL`. Fehlende Bibliotheken erscheinen grau.

## Platzhalter im Code

`app/app.html` enthält die Platzhalter `__SITE_TITLE__` und `__SITE_ICON__`. Sie werden vom Updater ersetzt – nicht von Hand ausfüllen.
