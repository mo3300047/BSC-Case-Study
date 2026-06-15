import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BSCSCAN_API_KEY")

BASE_URL = "https://api.etherscan.io/v2/api"

LAST_CALL = 0

MIN_INTERVAL = 0.4

def etherscan_get(params):
    global LAST_CALL
    now = time.time()
    wait = MIN_INTERVAL - (now - LAST_CALL)

    if wait > 0:
        time.sleep(wait)

    params["chainid"] = 56
    params["apikey"] = API_KEY

    response = requests.get(
        BASE_URL,
        params=params,
        timeout=20
    )

    LAST_CALL = time.time()
    data = response.json()

    if data.get("status") == "0":
        print(
            "API warning:",
            data.get("message"),
            data.get("result")
        )

    return data