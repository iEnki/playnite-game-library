# KI-Texte („Good to Know“)

Zu jedem Spiel kann ein kurzer Steckbrief aus Gamer-Sicht erzeugt werden: **Pitch, „So spielt es sich“, Highlights, Hinweise, „Für wen?“**. Der Original-Text aus dem Steam-Store bleibt unverändert; der KI-Text öffnet sich über den Button **Good to Know** in der Detailansicht.

## Anbieter

| Anbieter | Kosten | Hinweise |
|----------|--------|----------|
| **Ollama** | kostenlos | läuft lokal (z. B. auf deinem Gaming-PC); der PC muss während der Erzeugung laufen. Modelle mit 8–14 B Parametern liefern gute Ergebnisse. |
| **OpenAI** | nutzungsbasiert | API-Key von platform.openai.com. Die API-URL kann auf OpenAI-kompatible Dienste zeigen (OpenRouter, LM Studio, vLLM …). |
| **Claude** (Anthropic) | nutzungsbasiert | API-Key von console.anthropic.com. |

Grober Richtwert für ~1.000 Spiele (≈1.500 Tokens Eingabe + 400 Ausgabe pro Spiel): kleine Modelle wenige Cent bis wenige Dollar, große Modelle entsprechend mehr. Preise der Anbieter ändern sich – vorher dort prüfen.

## Einrichtung

1. Seite öffnen → **✨ KI-Texte** → Update-Token eingeben.
2. Tab **KI-Einstellungen** → Anbieter wählen.
3. Ollama-URL bzw. API-Key eintragen → **Verbindung testen** (lädt die verfügbaren Modelle) → Modell wählen.
4. Optional: Kreativität (Temperatur), Kontext (nur Ollama), Timeout, **parallele Anfragen** (Cloud: 3–4, Ollama: 1), Stil-Prompt.
5. **Einstellungen speichern**.

### Ollama im Netzwerk erreichbar machen

Ollama hört standardmäßig nur auf `localhost`. Läuft es auf einem anderen Rechner als Docker:

- Windows: Umgebungsvariable `OLLAMA_HOST=0.0.0.0` setzen, Ollama neu starten, Port **11434** in der Windows-Firewall für das private Netz freigeben.
- In den KI-Einstellungen `http://<IP-des-PCs>:11434` eintragen.

Läuft Ollama auf dem Docker-Host selbst, funktioniert `http://host.docker.internal:11434` (ist im Stack vorkonfiguriert).

## Texte erzeugen

Tab **Erzeugen**:

- **Welche Spiele?** Alle · Bibliotheken · aktuelle Ansicht (die gerade gefilterten/gesuchten Spiele) · einzelne Spiele
- **Was erzeugen?**
  - *Nur fehlende* – bestehende Texte bleiben
  - *Fehlende + anderes Modell* – ersetzt Texte, die mit einem anderen Modell erzeugt wurden (z. B. nach Wechsel auf Claude)
  - *Alle neu* – überschreibt alles (mit Bestätigung)
- **Auch Spiele ohne Steam-Daten:** Die KI schreibt aus eigenem Wissen. Kennt sie ein Spiel nicht sicher, wird es als „unbekannt“ übersprungen statt erfunden.
- **Höchstens N Spiele** – zum Testen eines neuen Modells.

Der Lauf läuft auf dem Server weiter, auch wenn der Browser geschlossen wird. Fortschritt, Restzeit und Protokoll sind jederzeit über den Button sichtbar; **Stopp** beendet nach dem aktuellen Spiel. Wird der Anbieter unerreichbar (PC aus), stoppt der Lauf automatisch – später mit *Nur fehlende* fortsetzen.

Nach einem Playnite-Import werden fehlende Texte automatisch im Hintergrund erzeugt (abschaltbar unter *KI-Einstellungen → Nach einem Playnite-Import*).

## Datenschutz & Speicherung

- KI-Texte: `data/ai/<Spiel-ID>.json` (bleiben bei Neuimporten erhalten)
- Einstellungen inkl. API-Keys: `data/ai_config.json` (Dateirechte 600). Die API gibt Keys nie zurück, nur eine Kurzform wie `sk-…1234`.
- An den KI-Anbieter gehen nur Spielname, Genres, Entwickler/Publisher, Erscheinungsdatum, Eigenschaften, Metacritic-Wert und die Shop-Beschreibung.

## Prompt anpassen

Unter *Stil & Regeln* lässt sich der System-Prompt (Ton, Regeln) ändern; „Standard wiederherstellen“ setzt ihn zurück. Der Aufbau des Steckbriefs (Felder) ist fest, damit die Darstellung funktioniert. Wer die Felder ändern will: `AI_SCHEMA` und `AI_PROMPT` in `app/updater.py` sowie `openGtk()` in `app/app.html`.
