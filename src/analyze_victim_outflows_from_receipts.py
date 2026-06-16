import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from requests import RequestException
from web3 import Web3

from etherscan_client import etherscan_get

load_dotenv()

KNOWN_PHISHING_CONTRACTS = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

PAGE_SIZE = 10000
MAX_BLOCK = 999999999
TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def normalize_address(address):
    return Web3.to_checksum_address(address)


def address_from_topic(topic):
    topic_hex = topic.hex() if hasattr(topic, "hex") else topic

    return normalize_address("0x" + topic_hex[-40:])


def int_from_data(data):
    data_hex = data.hex() if hasattr(data, "hex") else data

    return int(data_hex, 16)


def int_from_rpc_value(value):
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)

    return int(value)


def hex_value(value):
    value_hex = value.hex() if hasattr(value, "hex") else value

    if value_hex.startswith("0x"):
        return value_hex

    return "0x" + value_hex


def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_json(path, default):
    if not path.exists():
        return default

    with path.open() as file:
        return json.load(file)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as file:
        json.dump(data, file, indent=2, sort_keys=True)


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
    return (
        tx.get("contractAddress", "").strip()
        and tx.get("from", "").lower() == creator.lower()
        and tx.get("isError") != "1"
        and tx.get("txreceipt_status") != "0"
    )


def is_successful(tx):
    return tx.get("isError") != "1" and tx.get("txreceipt_status") != "0"


def discover_spenders(known_contracts, include_creator_deployments):
    known_contracts = [normalize_address(contract) for contract in known_contracts]
    creations = get_contract_creations(known_contracts)
    spenders = {
        normalize_address(creation["contractAddress"]): {
            "source": "known_phishing",
            "creator": normalize_address(creation["contractCreator"]),
            "deploy_tx": creation["txHash"],
        }
        for creation in creations
    }

    if not include_creator_deployments:
        return spenders

    creators = sorted({item["creator"] for item in spenders.values()})

    for creator in creators:
        for tx in get_transactions(creator):
            if not is_successful_contract_creation(tx, creator):
                continue

            contract = normalize_address(tx["contractAddress"])
            spenders.setdefault(contract, {
                "source": "same_creator_candidate",
                "creator": creator,
                "deploy_tx": tx["hash"],
            })

    return spenders


def get_receipt(w3, tx_hash, cache_dir):
    cache_path = cache_dir / "receipts" / f"{tx_hash.lower()}.json"
    cached = read_json(cache_path, None)

    if cached is not None:
        return cached

    receipt = w3.eth.get_transaction_receipt(tx_hash)
    data = {
        "transactionHash": hex_value(receipt["transactionHash"]),
        "blockNumber": int_from_rpc_value(receipt["blockNumber"]),
        "status": int_from_rpc_value(receipt.get("status", 1)),
        "logs": [
            {
                "address": normalize_address(log["address"]),
                "topics": [hex_value(topic) for topic in log["topics"]],
                "data": hex_value(log["data"]),
                "logIndex": int_from_rpc_value(log["logIndex"]),
            }
            for log in receipt["logs"]
        ],
    }
    write_json(cache_path, data)

    return data


def parse_transfer_logs(receipt):
    rows = []

    for log in receipt["logs"]:
        if len(log["topics"]) < 3:
            continue

        if log["topics"][0].lower() != TRANSFER_TOPIC.lower():
            continue

        rows.append({
            "token": normalize_address(log["address"]),
            "from": address_from_topic(log["topics"][1]),
            "to": address_from_topic(log["topics"][2]),
            "value_raw": str(int_from_data(log["data"])),
            "log_index": log["logIndex"],
        })

    return rows


def scan_spender_receipts(w3, spenders, output_dir):
    transfer_rows = []
    transaction_rows = []

    for spender, metadata in sorted(spenders.items()):
        txs = [tx for tx in get_transactions(spender) if is_successful(tx)]
        print(f"Scanning receipts for spender {spender}: {len(txs)} successful txs", flush=True)

        for index, tx in enumerate(txs, start=1):
            if index % 100 == 0:
                print(f"  receipts scanned: {index}/{len(txs)}", flush=True)

            receipt = get_receipt(w3, tx["hash"], output_dir)
            transfers = parse_transfer_logs(receipt)
            tx_time = int(tx["timeStamp"])
            tx_row = {
                "spender": spender,
                "spender_source": metadata["source"],
                "spender_creator": metadata["creator"],
                "tx_hash": tx["hash"],
                "from": normalize_address(tx["from"]),
                "to": normalize_address(tx["to"]) if tx.get("to") else "",
                "block_number": int(tx["blockNumber"]),
                "timestamp": tx_time,
                "datetime": format_time(tx_time),
                "transfer_event_count": len(transfers),
            }
            transaction_rows.append(tx_row)

            for transfer in transfers:
                from_address = transfer["from"]
                to_address = transfer["to"]
                is_candidate_victim_outflow = (
                    from_address.lower() not in {spender.lower(), ZERO_ADDRESS.lower()}
                    and int(transfer["value_raw"]) > 0
                )
                transfer_rows.append({
                    **tx_row,
                    **transfer,
                    "transfer_from": from_address,
                    "transfer_to": to_address,
                    "is_candidate_victim_outflow": is_candidate_victim_outflow,
                })

    return transaction_rows, transfer_rows


def summarize_transfers(transfer_rows):
    candidate_rows = [
        row for row in transfer_rows
        if row["is_candidate_victim_outflow"]
    ]
    by_victim = defaultdict(list)
    by_token = defaultdict(list)
    by_spender = defaultdict(list)
    by_day = defaultdict(list)

    for row in candidate_rows:
        by_victim[row["transfer_from"]].append(row)
        by_token[row["token"]].append(row)
        by_spender[row["spender"]].append(row)
        by_day[row["datetime"][:10]].append(row)

    victim_rows = [
        {
            "victim": victim,
            "transfer_count": len(rows),
            "token_count": len({row["token"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        }
        for victim, rows in by_victim.items()
    ]
    token_rows = [
        {
            "token": token,
            "transfer_count": len(rows),
            "victim_count": len({row["transfer_from"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        }
        for token, rows in by_token.items()
    ]
    spender_rows = [
        {
            "spender": spender,
            "transfer_count": len(rows),
            "victim_count": len({row["transfer_from"] for row in rows}),
            "token_count": len({row["token"] for row in rows}),
            "source": rows[0]["spender_source"],
            "creator": rows[0]["spender_creator"],
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        }
        for spender, rows in by_spender.items()
    ]
    daily_rows = [
        {
            "date": day,
            "transfer_count": len(rows),
            "victim_count": len({row["transfer_from"] for row in rows}),
            "token_count": len({row["token"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
        }
        for day, rows in by_day.items()
    ]

    return {
        "victims": sorted(victim_rows, key=lambda row: row["transfer_count"], reverse=True),
        "tokens": sorted(token_rows, key=lambda row: row["victim_count"], reverse=True),
        "spenders": sorted(spender_rows, key=lambda row: row["victim_count"], reverse=True),
        "daily": sorted(daily_rows, key=lambda row: row["date"]),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze victim-side token Transfer logs from phishing contract transaction receipts."
    )
    parser.add_argument(
        "--contracts",
        nargs="*",
        default=KNOWN_PHISHING_CONTRACTS,
        help="Known phishing contract addresses. Defaults to the two case-study contracts.",
    )
    parser.add_argument(
        "--known-only",
        action="store_true",
        help="Only analyze the provided known contracts; skip same-creator candidate deployments.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/victim_receipt_analysis",
        help="Directory for CSV/JSON outputs.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    rpc_url = os.getenv("BSC_RPC_URL")

    if not rpc_url:
        raise RuntimeError("BSC_RPC_URL must be set in .env")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))

    if not w3.is_connected():
        raise RuntimeError("Could not connect to BSC_RPC_URL")

    output_dir = Path(args.output_dir)
    spenders = discover_spenders(args.contracts, not args.known_only)
    spender_rows = [
        {"spender": spender, **metadata}
        for spender, metadata in sorted(spenders.items())
    ]
    print("Spenders analyzed:", len(spenders), flush=True)
    print("Output directory:", output_dir, flush=True)

    write_csv(
        output_dir / "spenders.csv",
        spender_rows,
        ["spender", "source", "creator", "deploy_tx"],
    )

    transaction_rows, transfer_rows = scan_spender_receipts(w3, spenders, output_dir)
    summary = summarize_transfers(transfer_rows)

    write_csv(
        output_dir / "spender_transactions.csv",
        transaction_rows,
        [
            "spender",
            "spender_source",
            "spender_creator",
            "tx_hash",
            "from",
            "to",
            "block_number",
            "timestamp",
            "datetime",
            "transfer_event_count",
        ],
    )
    write_csv(
        output_dir / "observed_token_transfers.csv",
        transfer_rows,
        [
            "spender",
            "spender_source",
            "spender_creator",
            "tx_hash",
            "from",
            "to",
            "block_number",
            "timestamp",
            "datetime",
            "transfer_event_count",
            "token",
            "value_raw",
            "log_index",
            "transfer_from",
            "transfer_to",
            "is_candidate_victim_outflow",
        ],
    )
    write_csv(
        output_dir / "victim_outflow_summary.csv",
        summary["victims"],
        ["victim", "transfer_count", "token_count", "spender_count", "first_seen", "last_seen"],
    )
    write_csv(
        output_dir / "token_outflow_summary.csv",
        summary["tokens"],
        ["token", "transfer_count", "victim_count", "spender_count", "first_seen", "last_seen"],
    )
    write_csv(
        output_dir / "spender_outflow_summary.csv",
        summary["spenders"],
        [
            "spender",
            "transfer_count",
            "victim_count",
            "token_count",
            "source",
            "creator",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "daily_outflow_summary.csv",
        summary["daily"],
        ["date", "transfer_count", "victim_count", "token_count", "spender_count"],
    )

    print("Spender transactions:", len(transaction_rows), flush=True)
    print("Observed token transfers:", len(transfer_rows), flush=True)
    print("Candidate victim outflows:", sum(row["is_candidate_victim_outflow"] for row in transfer_rows), flush=True)
    print("Victims:", len(summary["victims"]), flush=True)
    print("Tokens:", len(summary["tokens"]), flush=True)


if __name__ == "__main__":
    main()
