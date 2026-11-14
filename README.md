# α7 V → Lightroom Classic: WLAN-Tethering mit Live-Import

Jedes Foto, das du mit der Sony α7 V machst, erscheint wenige Sekunden später
in Lightroom Classic – **ohne Kabel**.

```
 Sony α7 V ──WLAN/FTP──▶  a7v-tether (FTP-Server)  ──▶  Überwachter Ordner  ──▶  Lightroom Classic
 (Auto-FTP-Übertragung)    auf deinem Rechner          (fertige Dateien)          "Automatisch importieren"
```

## Warum so?

Lightroom Classic kann Sony-Kameras nur **per USB** tethern. Die α7 V hat aber
eine eingebaute **FTP-Übertragung**: Sie schickt jedes Bild direkt nach der
Aufnahme im Hintergrund per WLAN an einen FTP-Server. Dieses Projekt ist dieser
Server. Er legt jede Datei erst dann in den Lightroom-Ordner, **wenn sie
vollständig übertragen ist**. So importiert Lightroom keine halben RAWs, und
abgebrochene Übertragungen werden verworfen.

## Voraussetzungen

- Windows 10/11 oder macOS, **Python 3.9+** ([python.org](https://www.python.org/downloads/))
- Lightroom Classic in einer Version, die die RAW-Dateien der α7 V unterstützt
- Kamera und Rechner im selben WLAN (Router oder Hotspot des Laptops)

## 1. Server starten

| System  | Start |
|---------|-------|
| Windows | `start_windows.bat` doppelklicken |
| macOS   | `start_macos.command` doppelklicken (beim ersten Mal: Rechtsklick → Öffnen) |

Beim ersten Start wird alles automatisch eingerichtet, und eine `config.toml`
wird angelegt. **Ändere dort das Passwort** und bei Bedarf den Ordner. Danach
siehst du:

```
  In der Kamera eintragen:  Host 192.168.1.23   Port 21
                            Benutzer 'a7v'   Passwort 'lightroom'
  Lightroom-Überwachungsordner: C:\Users\du\Pictures\Lightroom-Watch
```

Erlaubt die Firewall-Abfrage für **private Netzwerke**. Falls nötig, gib manuell
TCP-Port **21** und **60000–60100** frei.

> Tipp: Reserviere im Router eine feste IP für deinen Rechner. Dann musst du
> in der Kamera nie wieder etwas ändern.

## 2. Lightroom Classic einrichten (einmalig)

1. **Datei → Automatisch importieren → Einstellungen für automatischen Import …**
2. **Überwachter Ordner:** den angezeigten Ordner wählen (`…/Lightroom-Watch`). Er muss leer sein.
3. **Ziel:** wohin Lightroom die Bilder verschieben soll, z. B. `Bilder/Shooting-2026-11-14`.
4. Optional: Dateibenennung, **Entwicklungseinstellungen** (Preset wird live angewendet), Metadaten und Stichwörter.
5. **Datei → Automatisch importieren → Automatischen Import aktivieren** anhaken.

## 3. Kamera einrichten (einmalig)

Die Menünamen können sich je nach Firmware leicht unterscheiden. Du findest sie
unter **Menü → Netzwerk**.

1. **WLAN verbinden:** *Wi-Fi → Zugangspunkt-Einstellungen* → dein WLAN wählen (5 GHz ist schneller).
2. **FTP-Übertragungsfunktion → Servereinstellung → Server 1:**
   - Hostname: die angezeigte IP, z. B. `192.168.1.23`
   - Sicheres Protokoll: **Aus**
   - Port: `21`, also derselbe wie in `config.toml`
   - Benutzername und Passwort: wie angezeigt
   - Passivmodus: **Ein**
   - Verzeichnis: leer lassen (das Programm nimmt jedes Verzeichnis an)
3. **FTP-Funktion: Ein**
4. **Automatische FTP-Übertragung: Ein** (überträgt sofort nach jeder Aufnahme)
5. **Zu übertragender Dateityp:** RAW, JPEG/HEIF oder RAW+JPEG, je nach Wunsch

Jetzt auslösen. Im Fenster erscheint `→ Lightroom: DSC01234.ARW`, und kurz
danach ist das Bild in Lightroom.

## Konfiguration

Alles steht in `config.toml` (Vorlage: `config.example.toml`). Jede Einstellung
lässt sich auch beim Start überschreiben:

```
python -m a7v_tether --port 2121 --watch-dir "D:\LR-Watch" --backup-dir "D:\Backup" --extensions ARW
```

| Option | Bedeutung |
|---|---|
| `watch_dir` | Ordner, den Lightroom überwacht |
| `staging_dir` | Eingangsordner für laufende Uploads (Standard: `<watch_dir>-Eingang`, gleiche Festplatte) |
| `backup_dir` | Optional: zusätzliche Kopie jeder Datei, falls Lightroom einmal nicht läuft |
| `extensions` | Welche Dateitypen an Lightroom gehen. Andere, z. B. Videos, bleiben im Eingangsordner. |
| `port` | FTP-Port. Unter macOS/Linux ohne Adminrechte einen Port ≥ 1024 wählen, z. B. 2121, und in der Kamera eintragen. |

## Fehlersuche

| Problem | Lösung |
|---|---|
| Kamera meldet „Verbindung zum Server fehlgeschlagen“ | IP und Port prüfen, Firewall freigeben, beide Geräte im selben WLAN? Gäste-WLANs blockieren Geräte untereinander oft. |
| „Anmeldung fehlgeschlagen“ im Fenster | Benutzername und Passwort in der Kamera stimmen nicht mit `config.toml` überein. |
| Datei kommt an, aber nicht in Lightroom | Ist „Automatischen Import aktivieren“ angehakt? Unterstützt deine Lightroom-Version die α7-V-RAWs (Hilfe → Nach Updates suchen)? |
| Übertragung bricht ab | Kamera näher an den Router, 5 GHz nutzen, Energiesparmodus der Kamera verlängern. Abgebrochene Dateien werden verworfen. Die Kamera wiederholt sie. |
| Port 21 lässt sich nicht öffnen | Ein anderes Programm nutzt ihn, oder es fehlen Rechte. Nimm `--port 2121` und trage den Port in der Kamera ein. |

## Sicherheit

FTP überträgt Bilder und Passwort **unverschlüsselt**. Nutze die Lösung nur in
deinem eigenen bzw. vertrauenswürdigen Netz oder im Laptop-Hotspot, nicht in
öffentlichen WLANs.

## Entwicklung

```
pip install -r requirements.txt pytest
python -m pytest
```

Die Tests laden wie die Kamera echte Dateien per FTP hoch. Sie prüfen, dass nur
vollständige Dateien und nur die ausgewählten Typen im Lightroom-Ordner landen.
