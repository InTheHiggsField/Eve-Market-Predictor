import json
import requests

info = requests.get("https://esi.evetech.net/markets/prices")
with open("market_response.json", "w") as file:
    json.dump(info.json(), file)

#Plex = 44992
#