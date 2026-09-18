import requests
import json
import os
from dotenv import load_dotenv

load_dotenv("bot/.env")
url = "http://84.32.176.40:20001/v1/status"
headers = {"Authorization": f"Bearer {os.getenv('SERVER2_RCON_PASS')}"}
response = requests.get(url, headers=headers)
with open("scratch/status.json", "w", encoding="utf-8") as f:
    json.dump(response.json(), f, indent=2)
