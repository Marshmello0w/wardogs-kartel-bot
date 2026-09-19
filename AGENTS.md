# Projektregeln

## Modulare Cogs ("Neue Funktion")

- Sobald der User nach einer "Neuen Funktion" verlangt (zum Beispiel durch das Stichwort "Neue Funktion"), muss eine neue `.py`-Datei erstellt werden, etwa als neuer Cog.
- Die neue Funktionalität darf nicht in bereits existierende Dateien wie `leaderboard.py` oder `server_status.py` eingebaut werden, es sei denn, der User verlangt ausdrücklich ein Update einer bestehenden Datei.

## Zentrales Discord-Logging

- Alle neuen Features oder Cogs, die wichtige Aktionen ausführen (zum Beispiel Ingame-Broadcasts, Banns oder Statuswechsel), müssen diese Ereignisse zentral loggen.
- Verwende dafür immer den zentralen Event-Dispatcher:
  `self.bot.dispatch("bot_log", "Titel", "Beschreibung", discord.Color.blue())`
- Verwende in den Cogs selbst keine direkte Channel-Logik wie `channel.send`; das Posten übernimmt der zentrale `discord_logger`.
