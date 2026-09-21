# Das Kartell · Spielerportal

Eigenständige FastAPI-Anwendung für `https://kartell.marshmello0w.de`.
Die Webinstanz bekommt ausschließlich ihre eigene `.env`, einen Steam-Web-API-Key
und einen lesenden Datenbankbenutzer. Botdateien oder Bot-Secrets werden nicht benötigt.

## Installation / AMP Python App Runner

Python 3.11 oder neuer. Im AMP-Anwendungsverzeichnis liegt der Ordner `web/`.

```sh
python3 -m venv venv
venv/bin/python -m pip install -r web/requirements.txt
venv/bin/python web/start.py
```

AMP: **App Subdirectory** auf `web`, **App Script Filename** auf `start.py`,
Paketinstallation auf **Requirements.txt file** und Download-Typ auf **None** stellen.
Dadurch installiert AMP `web/requirements.txt`. Dateien per SFTP in `web/` hochladen,
anschließend nur diese Webinstanz aktualisieren und starten.

`web/.env.example` als `web/.env` anlegen und die fünf Werte ausfüllen:

- `PUBLIC_BASE_URL=https://kartell.marshmello0w.de`
- `WEB_DB_CONNECTION_URL=mysql://kartell_web:URL_ENCODED_PASSWORD@HOST:PORT/DATABASE`
- `WEB_REWARD_DB_CONNECTION_URL=mysql://kartell_rewards_submit:URL_ENCODED_PASSWORD@HOST:PORT/DATABASE`:
  separater Zugang, der ausschließlich Shop-Anfragen einfügt.
- `STEAM_WEB_API_KEY`: Steam-Web-API-Key für Namen und Avatar.
- `WEB_SESSION_SECRET`: mindestens 32 zufällige Zeichen, zum Beispiel mit
  `python -c "import secrets; print(secrets.token_urlsafe(48))"` generieren.

Die `.env` wird nur aus `web/` geladen. In AMP gesetzte Umgebungsvariablen haben Vorrang.
Sonderzeichen in Benutzername/Passwort der MySQL-URL müssen URL-kodiert werden.
Die `.env` darf nicht ins Git und sollte auf dem Server nur für den App-Benutzer lesbar sein.

Der Launcher liest zusätzlich optionale Variablen aus `web/.env` bzw. der Prozessumgebung:
`WEB_BIND_HOST` (Standard `127.0.0.1`), `WEB_PORT` (Standard `8080`),
`WEB_TRUSTED_PROXIES` (Standard `127.0.0.1`). Bei einer isolierten AMP-Containerinstanz
an deren private Container-IP/Port binden beziehungsweise `0.0.0.0` im Container;
der Reverse Proxy muss diesen Port erreichen können. Proxy-IP explizit eintragen.
Der öffentliche Zugriff erfolgt ausschließlich über HTTPS am Reverse Proxy.

**Genau ein Uvicorn-Worker.** Offene OpenID-Anmeldungen und öffentliche Datencaches
liegen pro Prozess im Speicher. Ein Neustart verwirft begonnene Logins; Benutzer starten
den Login dann erneut. Mehrere Worker benötigen zuerst einen gemeinsamen State-Store.
Die signierte Sitzung läuft nach 12 Stunden Inaktivität ab. Ein Logout entfernt das Cookie.

## Reverse Proxy / Domain

DNS für `kartell.marshmello0w.de` auf den bestehenden Proxy zeigen lassen. Beispiel für
Caddy auf demselben Host (bei anderem internen Port/Container-IP Upstream anpassen):

```caddyfile
kartell.marshmello0w.de {
    reverse_proxy 127.0.0.1:8080
}
```

TLS am Proxy bereitstellen. Die vollständige URL bleibt für Steam fest:
`https://kartell.marshmello0w.de/auth/steam/callback`.
OpenID ist die Browseranmeldung; es gibt kein Steam-Passwort im Portal und keine
OAuth-Zugriffstokens. Die Antwort wird gegen Steams festen OpenID-Endpunkt geprüft.
Namens-/Avatar-Ausfall verhindert den Zugriff auf verifizierte eigene Stats nicht.

Proxy-Zugriffslogs sollten für `/auth/steam/callback` keine Querystrings speichern.
Uvicorn-Zugriffslogs sind im Launcher deaktiviert. Anwendungslogs enthalten keine
OpenID-Claims, Datenbank-URLs oder API-Keys.

## Datenbankrechte

Auf derselben MySQL-/MariaDB-Datenbank wie der Bot einen separaten Benutzer anlegen.
`DATABASE_NAME`, `WEB_HOST` und Passwort im folgenden Beispiel ersetzen. `WEB_HOST`
auf die Quell-IP der Webinstanz beschränken. Dieses SQL wird **nicht** automatisch
beim App-Start ausgeführt und benötigt einen berechtigten Datenbankadministrator.

```sql
CREATE USER 'kartell_web'@'WEB_HOST' IDENTIFIED BY 'GENERATED_SECRET';
GRANT SELECT ON `DATABASE_NAME`.`leaderboard` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`player_daily_stats` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`player_playtime` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`player_faction_stats` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`player_ping_stats` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`banned_players` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT (steam_id, desired, expires_at) ON `DATABASE_NAME`.`admin_targets` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT (id, steam_id, reason, duration_str, issued_at, expires_at, status)
  ON `DATABASE_NAME`.`global_bans` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`durable_state` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`quest_points` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`vip_memberships` TO 'kartell_web'@'WEB_HOST';
GRANT SELECT ON `DATABASE_NAME`.`reward_requests` TO 'kartell_web'@'WEB_HOST';
```

Keine `INSERT`, `UPDATE`, `DELETE`, DDL- oder Admin-Auftragsrechte vergeben.
Zusätzlich setzt die App jede DB-Sitzung auf `READ ONLY`. Sie führt keine Migrationen aus.
MySQL 8 bzw. eine MariaDB-Version mit `ROW_NUMBER()` wie beim bestehenden Bot verwenden.

Für Einlösungen wird ein zweiter, strikt begrenzter Nutzer verwendet. Er erhält keinerlei
Lese- oder Änderungsrechte für Punktestände, VIPs oder Spielerdaten:

```sql
CREATE USER 'kartell_rewards_submit'@'WEB_HOST' IDENTIFIED BY 'GENERATED_SECRET';
GRANT INSERT ON `DATABASE_NAME`.`reward_requests` TO 'kartell_rewards_submit'@'WEB_HOST';
```

Der Bot verarbeitet die Anfrage innerhalb weniger Sekunden, prüft Punkte, VIP-Slots und
bei einem Fraktionswechsel den Live-RCON-Status und ist der einzige Prozess, der Werte ändert.

## Daten und Sichtbarkeit

- `/`: letzte gespeicherte Bot-Runden-Snapshots für `server1` bis `server3`.
  Über 30 Sekunden alte, unklare oder unvollständige Messungen gelten als veraltet,
  nicht als bewiesener Serverausfall. Die Gesamtspielerzahl zählt nur aktuelle Messungen.
  Das Dashboard lädt alle 30 Sekunden nach; DB-Cache 5 Sekunden.
- `/leaderboard`: dieselbe K/D-/Cash-Reihenfolge und dieselben Ban-Ausschlüsse wie Discord.
  7/30 Tage zählen einschließlich heute in UTC; Gesamtwerte sind die gespeicherten
  Lifetime-Counter. 50 Einträge je Seite; Cache 20 Sekunden.
- `/me`: nur die ID aus der verifizierten Steam-Sitzung. Parameter mit anderen Steam-IDs
  werden ignoriert. Bekannte Namen umfassen die gespeicherten Tagesnamen. Die zuletzt
  gemessenen Rundenwerte sind nicht automatisch eine aktuell laufende Runde. Fraktionswerte
  zählen erkannte Serverbeitritte beziehungsweise Fraktionswechsel, nicht Polling-Messungen.
- Ban-Gründe und Ablaufdaten sind nur privat sichtbar. Keine Admin-Nennungen oder Aufträge.
- `/rewards`: nur mit Steam-Anmeldung. Das Portal kann lediglich eine kurzlebige Anfrage für
  die eigene Steam-ID erzeugen; VIP-Aktivierung, Stornierung und externe Entfernung bestätigt
  das Admin-Team im Discord-VIP-Channel.
- `/health`: HTTP 200 bei erreichbarer DB, HTTP 503 bei Ausfall; keine Verbindungsdetails.
- Öffentliche Datencaches enthalten keine Profile. HTML wird mit `private, no-store` ausgeliefert.
- Spielerdaten werden nicht neu erhoben; bei fehlenden Daten zeigt die Oberfläche einen Leerzustand.

## Tests

Vom Repository-Verzeichnis:

```sh
web/venv/bin/python -m unittest discover -s tests -p test_web.py -v
web/venv/bin/python tests/web_preview.py
```

Auf Windows `web/venv/Scripts/python` verwenden. Die Vorschau lauscht ausschließlich auf
`127.0.0.1:8765`, verwendet synthetische Daten und besitzt eine Testanmeldung unter
`/__fixture_login`. **Nie `tests/` auf die Webinstanz deployen.** Produktionscode kennt
diese Route nicht. Browserprüfung: 320, 375, 414, 768 und 1440 Pixel Breite, Filter,
Tastaturfokus, Fehler-/Leerzustände und das private Profil prüfen.

Vor Go-live: `/health`, frische Bot-Daten und echte Steam-Anmeldung über die HTTPS-Domain
prüfen. Für einen Code-Rollback den vorherigen `web/`-Stand wiederherstellen und nur die
Webanwendung neu starten; keine Datenbankänderungen sind nötig.

## Gestaltung / Quellen

Hallmark: Stat-Led Dashboard, eigenes Schwarz-Orange-System in `tokens.css`,
Barlow Condensed und IBM Plex Sans. Fonts werden lokal ausgeliefert; OFL-Lizenzen
liegen in `static/fonts/`. Die Nutzerreferenz dient der Markenrichtung und ist kein Hintergrundbild.

- [Steam-Browseranmeldung](https://partner.steamgames.com/doc/features/auth)
- [OpenID 2.0 Verifikation](https://openid.net/specs/openid-authentication-2_0.html#verification)
