# Architektur-Regeln für dieses Projekt

- **Modulare Cogs ("Neue Funktion"):** 
  Sobald der User nach einer "Neuen Funktion" verlangt (z.B. durch das Stichwort "Neue Funktion"), MUSS zwingend eine **neue `.py` Datei** (z.B. als neuer Cog) erstellt werden. 
  Die neue Funktionalität darf **nicht** in bereits existierende Dateien (wie `leaderboard.py` oder `server_status.py`) eingebaut werden, es sei denn, der User verlangt explizit ein Update einer bestehenden Datei.

- **Zentrales Discord-Logging:**
  Alle neuen Features oder Cogs, die wichtige Aktionen ausführen (z. B. Ingame-Broadcasts, Banns oder Statuswechsel), MÜSSEN diese Ereignisse zentral loggen. 
  Verwende dafür **immer** den zentralen Event-Dispatcher: 
  `self.bot.dispatch("bot_log", "Titel", "Beschreibung", discord.Color.blue())`. 
  Verzichte auf direkte Channel-Logik (wie `channel.send`) in den Cogs selbst, um Spam und redundanten Code zu vermeiden, und überlasse das Posten dem zentralen `discord_logger`.
