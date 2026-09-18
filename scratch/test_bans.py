import requests
import json
import os
from dotenv import load_dotenv

load_dotenv("../bot/.env")
rcon_pass = os.getenv("SERVER2_RCON_PASS")

url = "http://84.32.176.40:20001/v1/bans"
headers = {"Authorization": f"Bearer {rcon_pass}"}

try:
    response = requests.get(url, headers=headers, timeout=10)
    print("Status:", response.status_code)
    data = response.json()
    with open("bans.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Found {len(data.get('bans', []))} bans.")
except Exception as e:
    print("Error:", e)
