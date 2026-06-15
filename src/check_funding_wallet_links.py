from collections import Counter, defaultdict
from datetime import datetime
from requests import RequestException
from etherscan_client import etherscan_get

known_phishing_contracts = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

PAGE_SIZE = 10000
MAX_BLOCK = 999999999
ONE_DAY = 24 * 60 * 60

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

    raise RuntimeError(f"Tx not found in address tx list: {tx_hash}")

def tx_position(tx):
    return (
        int(tx["blockNumber"]),
        int(tx.get("transactionIndex") or 0),
    )

def is_successful(tx):
    return tx.get("isError") != "1" and tx.get("txreceipt_status") != "0"

def is_incoming_bnb(tx, address):
    return (
        is_successful(tx)
        and tx.get("to", "").lower() == address.lower()
        and tx.get("from", "").lower() != address.lower()
        and int(tx.get("value", "0")) > 0
    )

def is_outgoing_bnb(tx, address):
    return (
        is_successful(tx)
        and tx.get("from", "").lower() == address.lower()
        and int(tx.get("value", "0")) > 0
    )

def find_last_incoming_before(transactions, address, target_tx):
    target_position = tx_position(target_tx)
    candidates = [
        tx for tx in transactions
        if tx_position(tx) < target_position
        and is_incoming_bnb(tx, address)
    ]

    if not candidates:
        return None

    return candidates[-1]

def bnb_from_wei(value):
    return int(value) / 10**18

def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

def print_transfer(tx):
    print("Time:", format_time(tx["timeStamp"]))
    print("From:", tx["from"])
    print("To:", tx["to"])
    print("Value BNB:", bnb_from_wei(tx["value"]))
    print("Block:", tx["blockNumber"])
    print("Tx:", tx["hash"])

def get_counterparties(transactions, address):
    counterparties = Counter()

    for tx in transactions:
        if not is_successful(tx):
            continue

        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        address = address.lower()

        if from_addr == address and to_addr:
            counterparties[to_addr] += 1
        elif to_addr == address and from_addr:
            counterparties[from_addr] += 1

    return counterparties

creations = get_contract_creations(known_phishing_contracts)
creators = {item["contractCreator"].lower() for item in creations}
creator_transactions = {}
funding_events = []

for creation in creations:
    creator = creation["contractCreator"]
    contract = creation["contractAddress"]
    deploy_hash = creation["txHash"]

    creator_txs = creator_transactions.setdefault(creator.lower(), get_transactions(creator))
    deploy_tx = find_tx(creator_txs, deploy_hash)
    funding_tx = find_last_incoming_before(creator_txs, creator, deploy_tx)

    if funding_tx is None:
        continue

    funding_events.append({
        "contract": contract,
        "creator": creator,
        "deploy_tx": deploy_tx,
        "funding_wallet": funding_tx["from"],
        "funding_tx": funding_tx,
    })

funding_wallets = sorted({event["funding_wallet"].lower() for event in funding_events})
funding_wallet_transactions = {
    wallet: get_transactions(wallet)
    for wallet in funding_wallets
}

print("=" * 80)
print("Funding wallets found:", len(funding_wallets))
for wallet in funding_wallets:
    print("Funding wallet:", wallet)

for event in funding_events:
    wallet = event["funding_wallet"].lower()
    wallet_txs = funding_wallet_transactions[wallet]
    funding_tx = find_tx(wallet_txs, event["funding_tx"]["hash"])
    upstream_tx = find_last_incoming_before(wallet_txs, wallet, funding_tx)

    outgoing_to_creators = [
        tx for tx in wallet_txs
        if is_outgoing_bnb(tx, wallet)
        and tx.get("to", "").lower() in creators
    ]
    nearby_outgoing = [
        tx for tx in wallet_txs
        if is_outgoing_bnb(tx, wallet)
        and abs(int(tx["timeStamp"]) - int(funding_tx["timeStamp"])) <= ONE_DAY
    ]

    print("=" * 80)
    print("Contract funded:", event["contract"])
    print("Creator funded:", event["creator"])
    print("Funding wallet:", event["funding_wallet"])
    print("Funding wallet tx count:", len(wallet_txs))
    print("-" * 80)
    print("Gas funding transfer")
    print_transfer(funding_tx)

    print("-" * 80)
    print("Funding wallet upstream before gas funding")
    if upstream_tx is None:
        print("Not found")
    else:
        print_transfer(upstream_tx)

    print("-" * 80)
    print("Transfers from this funding wallet to known creators:", len(outgoing_to_creators))
    for tx in outgoing_to_creators:
        print("  ", format_time(tx["timeStamp"]), tx["to"], bnb_from_wei(tx["value"]), tx["hash"])

    print("-" * 80)
    print("Outgoing BNB transfers within 24h of gas funding:", len(nearby_outgoing))
    for tx in nearby_outgoing[:20]:
        print("  ", format_time(tx["timeStamp"]), tx["to"], bnb_from_wei(tx["value"]), tx["hash"])

print("=" * 80)
print("Direct links between funding wallets")
direct_links = []
seen_direct_hashes = set()
for wallet, txs in funding_wallet_transactions.items():
    for tx in txs:
        if not is_successful(tx):
            continue

        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        tx_hash = tx["hash"].lower()

        if (
            from_addr in funding_wallets
            and to_addr in funding_wallets
            and from_addr != to_addr
            and tx_hash not in seen_direct_hashes
        ):
            direct_links.append(tx)
            seen_direct_hashes.add(tx_hash)

if not direct_links:
    print("No direct BNB/normal transaction links found between funding wallets")
else:
    for tx in direct_links:
        print_transfer(tx)

print("=" * 80)
print("Shared counterparties between funding wallets")
counterparties_by_wallet = {
    wallet: get_counterparties(txs, wallet)
    for wallet, txs in funding_wallet_transactions.items()
}
shared = defaultdict(dict)

for wallet, counterparties in counterparties_by_wallet.items():
    for counterparty, count in counterparties.items():
        shared[counterparty][wallet] = count

shared = {
    counterparty: counts
    for counterparty, counts in shared.items()
    if len(counts) > 1
    and counterparty not in funding_wallets
    and counterparty not in creators
}

if not shared:
    print("No shared counterparties found")
else:
    for counterparty, counts in sorted(shared.items(), key=lambda item: sum(item[1].values()), reverse=True)[:20]:
        print(counterparty, dict(counts))
