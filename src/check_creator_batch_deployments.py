from datetime import datetime
from requests import RequestException
from etherscan_client import etherscan_get

known_phishing_contracts = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

PAGE_SIZE = 10000
MAX_BLOCK = 999999999
ONE_HOUR = 60 * 60
ONE_DAY = 24 * ONE_HOUR

def get_contract_creations(contract_addresses):
    data = etherscan_get({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": ",".join(contract_addresses),
    })

    if data.get("status") != "1":
        raise RuntimeError(f"API error while getting creators: {data}")

    return data["result"]

def get_transactions(address):
    txs = []
    page = 1

    while True:
        try:
            data = etherscan_get({
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": MAX_BLOCK,
                "page": page,
                "offset": PAGE_SIZE,
                "sort": "asc",
            })
        except RequestException as exc:
            raise RuntimeError(
                f"Network request failed for {address}: {exc.__class__.__name__}"
            ) from None

        if data.get("status") == "0" and data.get("message") == "No transactions found":
            return txs

        if data.get("status") != "1":
            raise RuntimeError(f"API error for {address}: {data}")

        page_txs = data["result"]
        txs.extend(page_txs)

        if len(page_txs) < PAGE_SIZE:
            return txs

        page += 1

def is_successful_contract_creation(tx, creator):
    contract_address = tx.get("contractAddress", "").strip()

    if not contract_address:
        return False

    if tx.get("from", "").lower() != creator.lower():
        return False

    if tx.get("isError") == "1" or tx.get("txreceipt_status") == "0":
        return False

    return True

def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

def count_nearby_deployments(deployments, target_tx, window_seconds):
    target_time = int(target_tx["timeStamp"])

    return sum(
        1 for tx in deployments
        if abs(int(tx["timeStamp"]) - target_time) <= window_seconds
    )

def print_deployment(tx, known_contracts):
    contract = tx["contractAddress"]
    marker = "KNOWN_PHISHING" if contract.lower() in known_contracts else "candidate"

    print("-" * 80)
    print("Marker:", marker)
    print("Time:", format_time(tx["timeStamp"]))
    print("Contract:", contract)
    print("Block:", tx["blockNumber"])
    print("Tx:", tx["hash"])

creations = get_contract_creations(known_phishing_contracts)

for creation in creations:
    creator = creation["contractCreator"]
    known_contract = creation["contractAddress"]
    known_contracts = {
        item["contractAddress"].lower()
        for item in creations
        if item["contractCreator"].lower() == creator.lower()
    }

    transactions = get_transactions(creator)
    deployments = [
        tx for tx in transactions
        if is_successful_contract_creation(tx, creator)
    ]
    known_deployment = next(
        tx for tx in deployments
        if tx["contractAddress"].lower() == known_contract.lower()
    )

    print("=" * 80)
    print("Creator:", creator)
    print("Known phishing contract:", known_contract)
    print("Total creator transactions:", len(transactions))
    print("Total contracts deployed by creator:", len(deployments))
    print("First deployment:", format_time(deployments[0]["timeStamp"]), deployments[0]["contractAddress"])
    print("Last deployment:", format_time(deployments[-1]["timeStamp"]), deployments[-1]["contractAddress"])
    print("Known phishing deploy time:", format_time(known_deployment["timeStamp"]))
    print("Deployments within 1 hour:", count_nearby_deployments(deployments, known_deployment, ONE_HOUR))
    print("Deployments within 24 hours:", count_nearby_deployments(deployments, known_deployment, ONE_DAY))
    print("All deployed contracts:")

    for deployment in deployments:
        print_deployment(deployment, known_contracts)
