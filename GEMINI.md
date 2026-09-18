# Architektur-Regeln für dieses Projekt

- **Modulare Cogs ("Neue Funktion"):** 
  Sobald der User nach einer "Neuen Funktion" verlangt (z.B. durch das Stichwort "Neue Funktion"), MUSS zwingend eine **neue `.py` Datei** (z.B. als neuer Cog) erstellt werden. 
  Die neue Funktionalität darf **nicht** in bereits existierende Dateien (wie `leaderboard.py` oder `server_status.py`) eingebaut werden, es sei denn, der User verlangt explizit ein Update einer bestehenden Datei.
