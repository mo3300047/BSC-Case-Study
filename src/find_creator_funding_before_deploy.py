from datetime import datetime
from requests import RequestException
from etherscan_client import etherscan_get

contracts = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

PAGE_SIZE = 10000
MAX_BLOCK = 999999999

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

def find_tx(transactions, tx_hash):
    tx_hash = tx_hash.lower()

    for tx in transactions:
        if tx["hash"].lower() == tx_hash:
            return tx

    raise RuntimeError(f"Deployment tx not found in creator tx list: {tx_hash}")

def tx_position(tx):
    return (
        int(tx["blockNumber"]),
        int(tx.get("transactionIndex") or 0),
    )

def is_successful_incoming_bnb(tx, address):
    if tx.get("to", "").lower() != address.lower():
        return False

    if int(tx.get("value", "0")) <= 0:
        return False

    if tx.get("isError") == "1" or tx.get("txreceipt_status") == "0":
        return False

    return True

def find_last_funding_before_deploy(transactions, creator, deploy_tx):
    deploy_position = tx_position(deploy_tx)

    candidates = [
        tx for tx in transactions
        if tx_position(tx) < deploy_position
        and is_successful_incoming_bnb(tx, creator)
    ]

    if not candidates:
        return None

    return candidates[-1]

def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

def bnb_from_wei(value):
    return int(value) / 10**18

for creation in get_contract_creations(contracts):
    contract = creation["contractAddress"]
    creator = creation["contractCreator"]
    deploy_hash = creation["txHash"]

    transactions = get_transactions(creator)
    deploy_tx = find_tx(transactions, deploy_hash)
    funding_tx = find_last_funding_before_deploy(transactions, creator, deploy_tx)

    print("=" * 80)
    print("Contract:", contract)
    print("Creator:", creator)
    print("Deploy Time:", format_time(deploy_tx["timeStamp"]))
    print("Deploy Block:", deploy_tx["blockNumber"])
    print("Deploy Tx:", deploy_hash)

    if funding_tx is None:
        print("Last funding before deploy: Not found")
        continue

    seconds_before = int(deploy_tx["timeStamp"]) - int(funding_tx["timeStamp"])
    blocks_before = int(deploy_tx["blockNumber"]) - int(funding_tx["blockNumber"])

    print("-" * 80)
    print("Last funding before deploy")
    print("Time:", format_time(funding_tx["timeStamp"]))
    print("From:", funding_tx["from"])
    print("To:", funding_tx["to"])
    print("Value BNB:", bnb_from_wei(funding_tx["value"]))
    print("Block:", funding_tx["blockNumber"])
    print("Blocks Before Deploy:", blocks_before)
    print("Seconds Before Deploy:", seconds_before)
    print("Tx:", funding_tx["hash"])