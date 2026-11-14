# a7v Tether: Sony α7 V → Lightroom Classic, kabellos und live

Jedes Foto, das du mit der α7 V machst, landet Sekunden später in Lightroom
Classic, **ohne Kabel**. Dazu gibt es eine Oberfläche im Browser mit
Live-Vorschau, Verlauf, Kamera-Assistent und allen Einstellungen.

```
 α7 V ──WLAN (FTP/FTPS)──▶ a7v Tether ──▶ überwachter Ordner ──▶ Lightroom Classic
 Autom. FTP-Übertragung     · Sicherungskopie   (nur fertige Dateien)   „Automatisch importieren“
                            · Duplikat-Filter
                            · Umbenennen
                            · Pause/Sammeln
                            · Live-Dashboard ◀── Browser / Handy
```

Lightroom Classic kann Sony-Kameras nur per USB tethern. Die α7 V hat aber eine
eingebaute **FTP-Übertragung**: Sie sendet jedes Bild direkt nach der Aufnahme
im Hintergrund. a7v Tether nimmt diese Bilder entgegen. Erst wenn eine Datei
**vollständig** angekommen ist, legt es sie in den Ordner, den Lightroom
überwacht.

## Funktionen

| | |
|---|---|
| **Live-Ansicht** | Großes Vorschaubild mit Belichtung, Blende, ISO, Brennweite und Objektiv. Dazu ein Filmstreifen, eine Lightbox (Pfeiltasten) und die Übertragungen mit Tempo. |
| **Lightroom-Kontrolle** | Erkennt, ob Lightroom die Bilder wirklich abholt, und warnt, wenn der Auto-Import aus ist. |
| **Pause / Sammeln** | Bilder werden empfangen und gesichert, aber erst auf Knopfdruck an Lightroom gegeben. Gesammelte Bilder bleiben auch nach einem Neustart erhalten. |
| **Sessions** | Ein Name pro Shooting, z. B. „Hochzeit Müller“. Wird für Statistik, Umbenennung und Sicherungsordner verwendet. |
| **Umbenennen** | Muster wie `{date}_{session}_{seq}`. RAW und JPEG einer Aufnahme behalten denselben Namen, damit Lightroom das Paar erkennt. |
| **Sicherungskopie** | Jede Datei zusätzlich auf ein zweites Laufwerk, z. B. in `Sicherung/{session}/{date}/`. |
| **Duplikat-Filter** | Sendet die Kamera ein Bild erneut, wird es am Inhalt erkannt und übersprungen. |
| **Dateitypen** | Auswahl, was an Lightroom geht (ARW, JPG, HIF …). Alles andere wird nur gesichert. |
| **Verlauf** | Alle Übertragungen mit Suche, Filtern und **CSV-Export** für Excel. |
| **Kamera-Assistent** | Zeigt genau die Werte, die du in der Kamera einträgst. Mit **Selbsttest** für Ordner, Port, Anmeldung, Datenkanal und Speicherplatz. |
| **FTPS** | Verschlüsselte Übertragung mit eigenem Zertifikat, auf Knopfdruck erzeugt. |
| **Sicherheit** | Freigabeliste für Kamera-IPs, lokale Oberfläche mit Schutz vor fremden Webseiten (CSRF und DNS-Rebinding). |
| **Handy-Ansicht** | Optional im WLAN mit PIN erreichbar, nur zum Zuschauen, Pausieren und für Session-Wechsel. |
| **Benachrichtigungen** | Ton bei jedem Bild, Desktop-Benachrichtigung, Warnung bei wenig Speicherplatz. |
| **Kamera-Simulator** | Testet die ganze Kette mit Lightroom auch ohne Kamera. |
| **Hell / Dunkel** | Folgt dem System oder wird fest eingestellt. |

## Installation & Start

Voraussetzung: **Python 3.9+** von [python.org](https://www.python.org/downloads/).
Unter Windows bei der Installation „Add Python to PATH“ anhaken.

| System  | Start |
|---------|-------|
| Windows | `start_windows.bat` doppelklicken |
| macOS   | `start_macos.command` doppelklicken (beim ersten Mal: Rechtsklick → Öffnen) |

Beim ersten Start wird alles eingerichtet, danach öffnet sich die App im Browser
(`http://127.0.0.1:8765`). Erlaube die Firewall-Abfrage für **private
Netzwerke**.

## In 3 Schritten einsatzbereit

1. **Kamera:** In der App unter **Kamera** stehen alle Werte zum Abtippen:
   *Menü → Netzwerk → FTP-Übertragungsfunktion → Servereinstellung*.
   Danach **FTP-Funktion** und **Autom. FTP-Übertragung** einschalten.
2. **Lightroom:** *Datei → Automatisch importieren → Einstellungen …*, den in
   der App angezeigten Ordner als „Überwachter Ordner“ eintragen, dann
   *Automatischen Import aktivieren* anhaken.
3. **Selbsttest** klicken, auslösen, fertig.

> Tipp: Reserviere im Router eine feste IP für deinen Rechner, dann musst du in
> der Kamera nie wieder etwas ändern. Ohne Router geht es auch über den
> mobilen Hotspot des Laptops.

Die Menünamen der Kamera können je nach Firmware leicht abweichen.

## Testen ohne Kamera

```
.venv/bin/python -m a7v_tether.simulate --port 21 --password DEIN_PASSWORT --count 10
```

(Windows: `.venv\Scripts\python …`.) Der Simulator sendet echte JPEGs mit
EXIF-Daten per FTP. Sie erscheinen in der App und werden von Lightroom
importiert.

## Verschlüsselung (FTPS)

1. **Einstellungen → Sicherheit → Zertifikat erzeugen**
2. `cacert.pem` herunterladen, ins Hauptverzeichnis der Speicherkarte kopieren
   und in der Kamera als Root-Zertifikat importieren
3. In der App „Verschlüsselt übertragen“ einschalten und in der Kamera
   **Sicheres Protokoll: Ein** setzen

Das Zertifikat gilt für die aktuellen IP-Adressen des Rechners. Ändert sich die
IP, erneuere es. Die Kamera vertraut weiter, weil die Root-CA gleich bleibt.

## Fehlersuche

| Problem | Lösung |
|---|---|
| Kamera: „Verbindung zum Server fehlgeschlagen“ | IP und Port vergleichen. Firewall für TCP 21 und 60000–60100 freigeben. Gäste-WLANs blockieren Geräte untereinander oft. |
| Protokoll: „Anmeldung fehlgeschlagen“ | Benutzer oder Passwort in der Kamera stimmen nicht. |
| Pill „Lightroom reagiert nicht“ | Lightroom öffnen und *Automatischen Import aktivieren* anhaken. Unterstützt deine Lightroom-Version die α7-V-RAWs? |
| „Port 21 benötigt Administratorrechte“ | Port 2121 einstellen und in der Kamera eintragen. |
| Übertragung bricht ab | Näher an den Router, 5 GHz nutzen, Energiesparen der Kamera verlängern. Abgebrochene Dateien werden verworfen, die Kamera sendet sie erneut. |
| Weboberfläche nicht erreichbar | Port 8765 belegt? Die App nimmt automatisch den nächsten freien Port, siehe Konsolenfenster. |

## Für Fortgeschrittene

```
python -m a7v_tether --help
  --data-dir PFAD   Einstellungen, Verlauf, Vorschauen (Standard: Benutzer-Konfigordner)
  --port N          FTP-Port   --watch-dir PFAD   Lightroom-Ordner
  --web-port N      Port der Oberfläche   --no-browser   --headless   -v
```

Einstellungen liegen als `settings.json`, der Verlauf als SQLite-Datenbank im
Datenordner (Windows `%APPDATA%\a7v-tether`, macOS
`~/Library/Application Support/a7v-tether`).

**Aufbau:** `server.py` (FTP), `importer.py` (Übergabe, Sicherung,
Duplikate, Pause, Lightroom-Kontrolle), `metadata.py` (EXIF und Vorschau aus
ARW/JPG ohne Zusatzpakete), `webui.py` + `static/` (Oberfläche, API,
Live-Ereignisse), `history.py` (Verlauf), `tls.py` (Zertifikate).

```
pip install -r requirements.txt pytest
python -m pytest
```

## Sicherheit

Ohne FTPS gehen Bilder und Passwort **unverschlüsselt** durchs WLAN. Nutze
dann nur dein eigenes Netz oder den Laptop-Hotspot. Die Oberfläche ist
standardmäßig nur auf dem Rechner selbst erreichbar.
