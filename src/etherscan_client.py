import os
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BSCSCAN_API_KEY")

BASE_URL = "https://api.etherscan.io/v2/api"
CACHE_DIR = Path(os.getenv("ETHERSCAN_CACHE_DIR", ".cache/etherscan"))
BUDGET_FILE = CACHE_DIR / "request_budget.json"
DAILY_REQUEST_LIMIT = int(os.getenv("ETHERSCAN_DAILY_REQUEST_LIMIT", "80000"))
REQUEST_TIMEOUT = int(os.getenv("ETHERSCAN_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("ETHERSCAN_MAX_RETRIES", "3"))

LAST_CALL = 0

MIN_INTERVAL = float(os.getenv("ETHERSCAN_MIN_INTERVAL", "0.4"))

def cache_key(params):
    stable_params = {
        key: str(value)
        for key, value in sorted(params.items())
        if key.lower() != "apikey"
    }
    payload = json.dumps(stable_params, sort_keys=True).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()

def read_json(path, default):
    if not path.exists():
        return default

    with path.open() as file:
        return json.load(file)

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with tmp_path.open("w") as file:
        json.dump(data, file, indent=2, sort_keys=True)

    tmp_path.replace(path)

def today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def consume_daily_budget():
    budget = read_json(BUDGET_FILE, {})
    today = today_key()
    used = int(budget.get(today, 0))

    if used >= DAILY_REQUEST_LIMIT:
        raise RuntimeError(
            f"Etherscan daily request budget exceeded: {used}/{DAILY_REQUEST_LIMIT}"
        )

    budget[today] = used + 1
    write_json(BUDGET_FILE, budget)

def is_retryable_api_error(data):
    if data.get("status") != "0":
        return False

    message = (data.get("message") or "").lower()
    result = (data.get("result") or "").lower()
    text = f"{message} {result}"

    return (
        "timeout" in text
        or "server too busy" in text
        or "try again later" in text
        or "rate limit" in text
    )

def should_cache_response(data):
    return data.get("status") == "1" or data.get("message") == "No records found"

def etherscan_get(params):
    global LAST_CALL
    params = dict(params)
    params["chainid"] = 56

    request_cache_key = cache_key(params)
    cache_path = CACHE_DIR / f"{request_cache_key}.json"
    cached = read_json(cache_path, None)

    if cached is not None:
        if should_cache_response(cached):
            return cached

        cache_path.unlink(missing_ok=True)

    params["apikey"] = API_KEY

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        consume_daily_budget()

        now = time.time()
        wait = MIN_INTERVAL - (now - LAST_CALL)

        if wait > 0:
            time.sleep(wait)

        try:
            response = requests.get(
                BASE_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            LAST_CALL = time.time()
            data = response.json()

            if is_retryable_api_error(data) and attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue

            break
        except requests.RequestException as exc:
            last_error = exc

            if attempt == MAX_RETRIES:
                raise

            time.sleep(min(2 ** attempt, 10))
    else:
        raise last_error

    if data.get("status") == "0" and data.get("message") != "No records found":
        print(
            "API warning:",
            data.get("message"),
            data.get("result")
        )

    if should_cache_response(data):
        write_json(cache_path, data)

    return data