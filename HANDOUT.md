# 🤖 KartelBot – Projekt-Handout für die nächste KI

## Projektübersicht

Der **KartelBot** ist ein Discord-Bot (Python, discord.py) für die Gaming-Community "Das Kartell". Er verwaltet **3 WarDogs-Gameserver** gleichzeitig und bietet Server-Status, Leaderboards, Map-Voting, ein Admin-Bansystem, Ingame-Broadcasts und ein umfangreiches Hintergrund-Tracking für Server- und Spieler-Statistiken.

---

## 📁 Verzeichnisstruktur

```
C:\Users\tlmar\Documents\antigravity\calm-meitner\   ← Lokales Repo (Windows)
└── bot/
    ├── start.py              ← Einstiegspunkt, lädt alle Cogs
    ├── config.py             ← Zentrale Konfiguration (liest .env)
    ├── database.py           ← MySQL-Verbindung (aiomysql), alle Tabellen (inkl. init_db)
    ├── .env                  ← Secrets (NICHT im Repo)
    ├── .env.example          ← Template für die .env
    ├── requirements.txt
    └── cogs/
        ├── server_status.py  ← Server-Status Embeds (via Wardogs Masterlist API)
        ├── leaderboard.py    ← Leaderboard Embeds + "Eigenen Platz finden" Button (Turbo-Modus am Rundenende)
        ├── match_events.py   ← Runden-Ende Erkennung + Ingame Broadcasts
        ├── ban_tracker.py    ← Automatische Erkennung neuer Bans via RCON API
        ├── admin_panel.py    ← Admin Panel (Ban/Unban/Lookup + Auto-Unban Timer)
        ├── map_vote.py       ← Map-Voting System pro Server (Kein Chat-Spam, reines In-Place Editing)
        ├── discord_logger.py ← Zentraler Log-Handler (bot_log Events → Discord)
        └── stats_tracker.py  ← Zentraler Statistik-Tracker (Spielzeit, Factions, Ping, Round History)
```

> [!IMPORTANT]
> **Produktionspfad auf dem Server:** `/AMP/python-app-runner/`
> Das AMP-Panel klont das Repo direkt ins Root. Dateipfade für JSON-State-Files (z.B. `message_id.json`, `admin_panel_msg_id.json`) müssen daher **ohne** `bot/` Prefix sein, sonst gibt es `FileNotFoundError`.

---

## ⚙️ Konfiguration

### .env Variablen

| Variable | Beschreibung |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot-Token |
| `GUILD_ID` | Discord Server ID |
| `DB_CONNECTION_URL` | MySQL-URL (`mysql://user:pass@host:port/dbname`) |
| `SERVER_STATUS_CHANNEL_ID` | Kanal für Server-Status Embeds |
| `DISCORD_LOG_CHANNEL_ID` | Kanal für Bot-Logs |
| `LEADERBOARD_CHANNEL_ID` | Kanal für Leaderboard Embeds |
| `SERVER1_RCON_URL` / `_PASS` | RCON-Zugang Server 1 |
| `SERVER2_RCON_URL` / `_PASS` | RCON-Zugang Server 2 |
| `SERVER3_RCON_URL` / `_PASS` | RCON-Zugang Server 3 |
| `SERVER2_VOTE_CHANNEL_ID` | Kanal für Map-Voting Server 2 |
| `SERVER3_VOTE_CHANNEL_ID` | Kanal für Map-Voting Server 3 |
| `ADMIN_ROLE_IDS` | Komma-getrennte Discord-Rollen-IDs für Admin-Berechtigung (z.B. `123,456`) |
| `ADMIN_PANEL_CHANNEL_ID` | Kanal für das Admin-Ban-Panel |
| `SERVER_IDS` | Wardogs Masterlist UUIDs (komma-getrennt) |

### config.py Zusätzliche Einstellungen

- `BROADCAST_MESSAGES` – Liste von Ingame-Broadcast-Texten (zufällig ausgewählt am Rundenende)
- `MAP_VOTE_OPTIONS` – Map-Auswahlmöglichkeiten für das Voting
- `ADMIN_PANEL_MSG_ID_FILE` – JSON-Datei für die Panel-Message-ID

---

## 🗄️ Datenbank-Tabellen (MySQL)

| Tabelle | Zweck |
|---|---|
| `server_uptime` | Uptime-Tracking pro Server |
| `leaderboard` | Lifetime-Stats aller Spieler (Kills, Deaths, Cash) |
| `player_daily_stats` | Tägliche Stats (für 7d/30d Zeiträume) |
| `global_bans` | Ban-Historie mit Dauer, Grund, Admin, Status, Ablaufzeit |
| `banned_players` | Sync-Tabelle für ban_tracker (announced-Flag) |
| **`player_playtime`** | *Neu:* Spielzeit pro Spieler und Server in Sekunden |
| **`player_faction_stats`** | *Neu:* Häufigkeit der Faction-Wahl pro Spieler |
| **`player_ping_stats`** | *Neu:* Ping-Samples für Durchschnitts-Ping |
| **`server_player_snapshots`**| *Neu:* Zeitstempel + Spieleranzahl für Peak-Zeiten-Analyse |
| **`round_history`** | *Neu:* Match-Historie (Start, Ende, Dauer, Map, Gewinner-Faction) |
| **`faction_wins`** | *Neu:* Einfacher Zähler für Faction-Winrates |
| **`map_play_stats`** | *Neu:* Einfacher Zähler für Map-Beliebtheit / Rotation |

---

## 🧩 Cog-Übersicht

### server_status.py
- Fragt die **Wardogs Masterlist API** ab (`https://wardogserverlist.com/api/server/{UUID}`)
- Postet/editiert **ein** Status-Embed im konfigurierten Kanal

### leaderboard.py
- Fragt alle 3 Server via **RCON API** ab
- **Turbo-Modus:** Pollt schneller, wenn der höchste FactionScore >= 99 ist, um genaue Endrunden-Stats (Kills/Deaths/Cash) zu sichern.
- Zeigt Top-10 Embeds mit Dropdown.
- **🔍 "Eigenen Platz finden"** Button unter jedem Panel → Modal für Steam-ID → zeigt persönlichen Rang (All-Time, 30d, 7d).

### match_events.py
- Pollt alle 5 Sekunden `/v1/status` aller 3 Server
- Erkennt Rundenende wenn `factionScores >= 100` und sendet Ingame Broadcasts.

### ban_tracker.py & admin_panel.py
- Tracker: Erfasst Server-Bans und spiegelt sie in der globalen DB.
- Admin Panel: Zentralisiertes UI zum Bannen/Entbannen von Spielern über alle Server hinweg.
- **Queue-System:** Da offline Spieler nicht über RCON gebannt werden können, legt der Bot Offline-Bans in eine `pending_admin_actions.json` und feuert sie ab, sobald der Spieler joint.
- **Auto-Unban Task:** Automatische Entbannung nach Ablauf der Strafe (z.B. nach 7 Tagen).

### map_vote.py
- Ein Voting-Panel pro Server (Server 2 & 3)
- Spieler wählen die nächste Map per Button
- **In-Place Editing & No-Spam:** Embeds werden nur editiert. Es gibt keine separaten Chat-Nachrichten für das Ende des Votings, Ergebnisse gehen ausschließlich ins Panel und in den Discord Log-Kanal.
- Gewinner-Map wird via `/v1/config` PUT injiziert und beim Neustart der Runde aufgeräumt.

### stats_tracker.py *(Neu)*
- Unabhängiger, langsamer Tracker (60-Sekunden Intervall).
- Speist die 7 neuen Statistik-Tabellen (Spielzeit, Pings, Factions, Round History, Peak Player Counts).
- Arbeitet unabhängig vom Leaderboard-Turbo, da Langzeittrends (Spielzeit, Ping) nicht in der Sekunde des Rundenendes aktualisiert werden müssen.

### discord_logger.py
- Lauscht auf interne `bot_log` Events und postet saubere Embeds im Admin-Log Kanal.

---

## 🚨 Kritische Regeln & Gotchas

### Architektur-Regeln (aus GEMINI.md)

1. **Modulare Cogs:** Bei "Neuer Funktion" → **IMMER** neue `.py` Datei erstellen, außer ein bestehendes Modul soll explizit erweitert werden.
2. **Zentrales Logging:** Für Admin/Bot-Logs **immer** `self.bot.dispatch("bot_log", "Titel", "Beschreibung", discord.Color.blue())` verwenden. **Kein** direktes `channel.send()` für Log-Spam.

### Bekannte Fallstricke

| Problem | Ursache | Lösung |
|---|---|---|
| `FileNotFoundError` bei JSON-Dateien | Pfad mit `bot/` Prefix | Nur Dateinamen verwenden, kein `bot/` davor |
| `UnboundLocalError: config` | `import config` innerhalb einer Funktion | Imports immer ganz oben in der Datei |
| Doppelte Discord-Panels über Nacht | Temporäre API-Fehler (Rate Limit/Timeout) | `except Exception` muss ein `continue`/`return` feuern, KEIN `message = None` |
| Discord Modals & Dropdowns | Discord API Limitierung | Buttons für Modals nutzen, erst nach Interaktion auslösen |

### Server-Sicherheit

> [!CAUTION]
> **NIEMALS den Server neustarten** ohne explizite Erlaubnis des Users. Auch keine Konfigurationsänderungen an Services durchführen.

---

## 🔄 Deployment-Workflow

1. Änderungen lokal machen.
2. Syntax checken: `python -m py_compile bot/cogs/<datei>.py`
3. `git add` → `git commit` → `git push`
4. User macht auf dem Server (AMP Panel) `git pull` und startet den Bot neu.
