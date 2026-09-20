# Einführung und Betrieb

## Vor der Umstellung

1. Aktuellen Bot-Commit, Arbeitsverzeichnis und Startbefehl notieren.
2. Nur den Bot kontrolliert stoppen. Keine Gameserver- oder AMP-Service-Konfiguration
   für diese Migration ändern; keine Gameserver-Neustarts auslösen.
3. Vollständige SQL-Sicherung der Bot-Datenbank erstellen. `.env` sowie
   `message_id.json`, `leaderboard_state.json`, `admin_panel_msg_id.json`,
   `map_vote_state.json` und `pending_admin_actions.json` getrennt sichern.
4. Das bisher im Git-Verlauf enthaltene Server-2-RCON-Passwort durch den Betreiber
   ersetzen lassen. Den neuen Wert in `SERVER2_RCON_PASS` eintragen. Das Entfernen
   aus aktuellem Quellcode löscht alte Git-Versionen nicht.
5. Neue Version zunächst gegen eine Kopie der Datenbank mit `maintenance.py migrate`
   prüfen. Niemals gleichzeitig alte und neue Bot-Version auf derselben Datenbank betreiben.

## Datenbank und Migration

Die Datenbank muss vorab existieren. Der Bot benötigt SELECT/INSERT/UPDATE/DELETE;
für Initialisierung und additive Migrationen zusätzlich CREATE. Die Rangabfragen
benötigen Window Functions (MySQL 8+ oder MariaDB 10.2+).

`python maintenance.py migrate` aus dem Bot-Arbeitsverzeichnis legt zusätzliche
Tabellen an. Wiederholungen sind unschädlich; vorhandene Statistik-/Ban-Tabellen
werden weder geleert noch rückwirkend neu berechnet. Verbindungen verwenden UTC
und utf8mb4. Bestehende Datumswerte werden nicht nachträglich verschoben.

Neue Daten:

- `schema_migrations`: ausgeführte additive Migrationen.
- `durable_state`: Rundenzustände, Voting, Initialisierung des Ban-Abgleichs.
- `round_events` / `event_deliveries`: dauerhafte Ereignisse und Broadcast-Zustellung.
- `tracked_rounds`: Zuordnung zu `round_history` und Qualitätskennzeichnung.
- `player_counter_state`: Messbasis pro Spieler und Runde.
- `admin_targets` / `admin_jobs`: gewünschter Ban-Zustand und Aufträge je Server.
- `legacy_admin_import`: einmaliger Import alter JSON-Aufträge; unsichere Zuordnungen
  werden als `needs_review` gespeichert.

Beim ersten regulären Botlauf werden die jüngsten historischen Admin-Entscheidungen
und die alte JSON-Warteschlange übernommen. Die Originaldatei bleibt erhalten.
Nicht zuordenbare Bans werden nicht ausgeführt. Solche Einträge anhand ihrer
Steam-ID/Historie prüfen und bei Bedarf einen neuen, ausdrücklichen Ban über das
Admin-Panel vergeben. Ein Unban ersetzt alte offene Ban-Aufträge.

## Nach dem Start prüfen

- Der zentrale Bot-Log meldet keine dauerhaften DB-/RCON-Fehler.
- Bestehende Discord-Panels wurden übernommen; Server 3 trägt seinen richtigen Namen.
- `python maintenance.py status` zeigt offene/abgeschlossene Admin-Aufträge und
  zurückgestellte Altaufträge. Der Befehl sendet keine RCON-/Discord-Aktionen,
  initialisiert aber bei Bedarf das Schema.
- Einen vollständigen Rundenwechsel beobachten: genau eine lokale neue Runde,
  neues Voting und korrektes Ergebnis beziehungsweise klar markierte Datenlücke.
- Temporäre Rotation nach Beginn der nächsten Runde und erstem Punkt wieder bereinigt.
- Persönlicher Rang stimmt mit der öffentlichen Rangliste überein.

Keine Testbans gegen echte Spieler zur Funktionsprüfung verwenden. Für Live-Tests
benötigt es einen ausdrücklich dafür vorgesehenen Testaccount/Testserver.

## Fehler und Konflikte

Ein ausstehender Unban bleibt offen, bis RCON-/Konfigurationszustand seine
Ausführung bestätigt. Ein deaktivierter Server verliert seine Aufträge nicht.
Wiederholungen werden zeitlich auseinandergezogen. Bei Authentifizierungsfehlern
die entsprechenden Umgebungsvariablen prüfen; Passwörter nicht in Logs kopieren.

Ein Rotationskonflikt erfordert einen Vergleich der in `durable_state` gespeicherten
`change.before`-/`change.after`-Listen mit der aktuellen Serverrotation. Keine alte
komplette Konfiguration zurückschreiben. Nach manueller Abstimmung und bereinigter
Rotation den Bot stoppen, eine Sicherung erstellen und nur das betroffene
`change`-Feld im Voting-Zustand auf `null` setzen. Anschließend kann der Bot neu starten.

Unklare Broadcast-Zustellungen stehen in `event_deliveries` als `uncertain`.
Ohne Zustellbeleg ist keine automatische Wiederholung vorgesehen. Fehlgeschlagene
Ban-Broadcasts mit unklarer Zustellung werden ebenfalls nicht wiederholt.

## Rückweg ohne Datenverlust

Bevor eine Software-Rücknahme erwogen wird, den neuen Bot stoppen und eine **aktuelle**
vollständige SQL-Sicherung anlegen. Nicht einfach die Datenbank vor der Umstellung
zurückspielen: dadurch gingen zwischenzeitliche Statistiken und Admin-Aktionen verloren.

```text
python maintenance.py export-state --output rollback-export-YYYYMMDD-HHMM
```

Das Zielverzeichnis muss neu sein. Exportiert werden aktuelle offene Admin-Aufträge,
Voting-Zustände einschließlich Rotationsänderungen sowie Rundenzustände. Message-ID-
Dateien ebenfalls aus dem aktuellen Arbeitsverzeichnis sichern.

Vor einem Rückwechsel alle noch vorhandenen Rotationsänderungen abgleichen und
ausstehende Admin-Aufträge prüfen. Die alte Version kennt die neuen Datenbankaufträge
nicht; sie benötigt die exportierte `pending_admin_actions.json`. Exportierte
Voting-Zustände müssen vor Verwendung durch eine alte Version geprüft werden, da
deren Aufräumlogik die neuen `change`-Felder nicht versteht.

Die zusätzlichen Tabellen können bestehen bleiben. Die bisherigen Aggregate,
aktuellen Spielerzähler und Ban-Historien bleiben auch für eine alte Version lesbar.
Für eine spätere erneute Vorwärtsmigration die erhaltenen neuen Tabellen mit den
zwischenzeitlich erfolgten alten Aktionen abgleichen. Eine gezielte Vorwärtskorrektur
ist meist sicherer als ein Rückwechsel auf die bekannten alten Ban-/Voting-Fehler.

## Grenzen der Messung

Polling rekonstruiert keine verpassten Endstände. `started_at=NULL` bedeutet einen
unbekannten Beginn; Dauer bleibt dann ebenfalls NULL. `tracked_rounds.quality`
unterscheidet `observed`, `partial`, `gap`, `missing_end` und `counter_drop`.
Auch ein beobachteter Start ist eine Annäherung an den tatsächlichen Startzeitpunkt.
Spielzeit wird nur zwischen erfolgreichen Proben bis maximal 90 Sekunden gezählt.
Die angezeigte Verfügbarkeit misst RCON-Erreichbarkeit, nicht zweifelsfrei den
Zustand des Gameserverprozesses.
