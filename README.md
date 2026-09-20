# Wardogs KartelBot

Discord-Bot für die drei WarDogs-Server von „Das Kartell“: Status, Leaderboards,
Map-Voting, globale Bans, Ingame-Broadcasts und Statistik-Erfassung.

## Voraussetzungen und Start

- Python 3.13; Pakete aus `bot/requirements.txt`.
- Eine vorhandene MySQL-8+- oder MariaDB-10.2+-Datenbank mit InnoDB. Lokal getestet mit MariaDB 12.1.
- Eine Discord-Anwendung mit Bot-Token sowie Zugriff auf die konfigurierten Kanäle.
- RCON-Zugangsdaten für jeden aktivierten Gameserver.

`bot/.env.example` als Vorlage für die lokale `.env` verwenden und alle Platzhalter
ersetzen. Keine Zugangsdaten committen. Im Entwicklungscheckout:

```powershell
cd bot
python -m pip install -r requirements.txt
python start.py
```

Start immer aus dem vorgesehenen Arbeitsverzeichnis. Auf AMP kann das direkt
`/AMP/python-app-runner/` sein, wenn die Python-Dateien dort liegen. Zustandsdateien
verwenden absichtlich keinen fest eingebauten `bot/`-Präfix.

## Projektstruktur

Die Discord-Schicht bleibt von gemeinsam genutzter Logik getrennt:

```text
bot/
├── cogs/             Discord-Commands, Events und Hintergrundtasks
├── core/             Konfiguration, Berechtigungen und Laufzeithilfen
├── domain/           Runden-, Rotations- und Statistiklogik
├── services/         Ban-Dienst und gemeinsamer RCON-Client
├── infrastructure/   Datenbank, Migrationen und dauerhafter Zustand
├── maintenance.py    Offline-Wartungswerkzeug
└── start.py           Bot-Einstiegspunkt
```

Nur Module in `cogs/` werden als Discord-Erweiterungen geladen. Die übrigen Pakete
sind normale Python-Module und können von mehreren Cogs gemeinsam verwendet werden.

## Verhalten

- Eine zentrale Serverliste und ein gemeinsamer HTTP-Client versorgen die Cogs.
- Der RoundTracker pollt alle fünf Sekunden. Zusätzliche Statusabfragen vor und nach
  Spielerabfragen sichern die Zuordnung der Zähler zu einer Runde ab.
- Rundenzustand und Ereignisse werden in der Datenbank gespeichert. Unbekannte
  Startzeiten, verpasste Rundenenden und Messlücken werden als solche erfasst.
- Leaderboards zählen pro Runde und verwenden für persönliche und öffentliche
  Ansichten dieselben Filter. Zeiträume sind UTC-Kalendertage einschließlich heute.
- Ban-Zeiten beginnen bei der Admin-Aktion. Offline-Aufträge verfallen mit dem Ban;
  fehlgeschlagene Unbans werden weiter versucht. Admin-Aktionen werden vor RCON-Zugriff
  dauerhaft gespeichert.
- Leere `ADMIN_ROLE_IDS` erlauben nur Discord-Administratoren Admin-Aktionen.
- Voting: mindestens fünf Stimmen für die Gewinneroption, Ende bei 95 % des Score-Limits.
  Bei Gleichstand gilt die Reihenfolge in `MAP_VOTE_OPTIONS`. Externe Rotationsänderungen
  werden nicht durch alte Konfigurationskopien überschrieben.
- Erfolgslogs entstehen erst nach bestätigter Ausführung. Unklar zugestellte
  Broadcasts werden nicht blind erneut gesendet.

Bestehende Statistiken bleiben bei der Umstellung erhalten. Die erste Spielerprobe
setzt eine Messbasis; verpasste historische Werte werden nicht erfunden.

## Tests

Die Tests nutzen keine produktiven Discord-/RCON-Zugänge und laden keine `.env`:

```powershell
bot/venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

Ohne `KARTEL_TEST_DB_PORT` werden Datenbanktests ausdrücklich übersprungen. Für den
vollständigen Lauf unter Windows mit installierter MariaDB:

```powershell
powershell -ExecutionPolicy Bypass -File tests/run_mysql_tests.ps1
```

Das Skript startet eine separate Datenbank nur auf Loopback, wählt einen freien Port,
erstellt ausschließlich zufällig benannte Testdatenbanken und beendet anschließend
seinen eigenen Serverprozess. Keine bestehende MariaDB-Instanz wird verwendet.

Die bisherigen `test_*.py`-Dateien außerhalb von `tests/` sind manuelle
Diagnoseskripte und werden nicht automatisch ausgeführt.

Einführung, Sicherung und Rückweg: [Betriebsanleitung](docs/OPERATIONS.md).
