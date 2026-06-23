import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
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
STABLE_TOKENS = {
    "0x55d398326f99059fF775485246999027B3197955",
    "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
}


def read_csv(path):
    if not path.exists():
        return []

    with path.open() as file:
        return list(csv.DictReader(file))


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


def normalize_address(address):
    return Web3.to_checksum_address(address)


def address_key(address):
    return address.lower()


def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def hex_value(value):
    value_hex = value.hex() if hasattr(value, "hex") else value

    if value_hex.startswith("0x"):
        return value_hex

    return "0x" + value_hex


def int_from_data(data):
    data_hex = data.hex() if hasattr(data, "hex") else data

    return int(data_hex, 16)


def int_from_rpc_value(value):
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)

    return int(value)


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


def get_source_code(contract_address):
    data = etherscan_get({
        "module": "contract",
        "action": "getsourcecode",
        "address": contract_address,
    })

    if data.get("status") != "1":
        raise RuntimeError(f"API error while getting source for {contract_address}: {data}")

    return data["result"][0]


def is_successful_contract_creation(tx, creator):
    return (
        tx.get("contractAddress", "").strip()
        and tx.get("from", "").lower() == creator.lower()
        and tx.get("isError") != "1"
        and tx.get("txreceipt_status") != "0"
    )


def is_successful(tx):
    return tx.get("isError") != "1" and tx.get("txreceipt_status") != "0"


def discover_deployments(known_contracts):
    known_contracts = [normalize_address(contract) for contract in known_contracts]
    creations = get_contract_creations(known_contracts)
    deployments = {}

    for creation in creations:
        contract = normalize_address(creation["contractAddress"])
        creator = normalize_address(creation["contractCreator"])
        deployments[contract] = {
            "contract": contract,
            "creator": creator,
            "deploy_tx": creation["txHash"],
            "is_known_phishing": True,
            "marker": "confirmed_known_phishing",
        }

    creators = sorted({item["creator"] for item in deployments.values()})

    for creator in creators:
        for tx in get_transactions(creator):
            if not is_successful_contract_creation(tx, creator):
                continue

            contract = normalize_address(tx["contractAddress"])
            if contract in deployments:
                deployments[contract].update({
                    "deploy_block": int(tx["blockNumber"]),
                    "deploy_time": format_time(tx["timeStamp"]),
                    "deploy_timestamp": int(tx["timeStamp"]),
                })
                continue

            deployments[contract] = {
                "contract": contract,
                "creator": creator,
                "deploy_tx": tx["hash"],
                "deploy_block": int(tx["blockNumber"]),
                "deploy_time": format_time(tx["timeStamp"]),
                "deploy_timestamp": int(tx["timeStamp"]),
                "is_known_phishing": False,
                "marker": "same_creator_candidate",
            }

    return sorted(deployments.values(), key=lambda row: (row["creator"], row["deploy_timestamp"]))


def bytecode_fingerprint(w3, contract, cache_dir):
    cache_path = cache_dir / "bytecode" / f"{address_key(contract)}.json"
    cached = read_json(cache_path, None)

    if cached is not None:
        return cached

    code = w3.eth.get_code(normalize_address(contract))
    code_hex = hex_value(code)
    fingerprint = {
        "contract": normalize_address(contract),
        "code_length": len(code),
        "code_hash": hashlib.sha256(code).hexdigest(),
        "code_prefix": code_hex[:66] if len(code_hex) >= 66 else code_hex,
    }
    write_json(cache_path, fingerprint)

    return fingerprint


def load_infrastructure_labels(receiver_dir, downstream_dir, next_hop_dir):
    labels = {}

    for row in read_csv(receiver_dir / "receiver_summary.csv"):
        labels[address_key(row["transfer_to"])] = "known_receiver"

    for row in read_csv(downstream_dir / "downstream_recipient_all_tokens_summary.csv"):
        labels[address_key(row["recipient"])] = "known_downstream"

    for row in read_csv(next_hop_dir / "next_hop_recipient_summary.csv"):
        labels[address_key(row["recipient"])] = "known_next_hop"

    return labels


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


def address_from_topic(topic):
    topic_hex = topic.hex() if hasattr(topic, "hex") else topic

    return normalize_address("0x" + topic_hex[-40:])


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
        })

    return rows


def amount_from_raw(value_raw, decimals=18):
    return Decimal(value_raw) / (Decimal(10) ** int(decimals))


def decimal_string(value):
    normalized = value.normalize()

    if normalized == normalized.to_integral():
        return format(normalized, "f")

    return format(normalized, "f").rstrip("0").rstrip(".")


def analyze_contract_activity(w3, deployment, infrastructure_labels, cache_dir, token_decimals):
    contract = deployment["contract"]
    txs = deployment.get("_cached_txs") or [tx for tx in get_transactions(contract) if is_successful(tx)]
    source = get_source_code(contract)
    transfer_rows = []
    receiver_hits = set()

    for index, tx in enumerate(txs, start=1):
        if index % 100 == 0:
            print(f"  receipts scanned: {index}/{len(txs)}", flush=True)

        receipt = get_receipt(w3, tx["hash"], cache_dir)

        for transfer in parse_transfer_logs(receipt):
            from_address = transfer["from"]
            to_address = transfer["to"]
            is_candidate_victim_outflow = (
                address_key(from_address) != address_key(contract)
                and address_key(from_address) != ZERO_ADDRESS
                and int(transfer["value_raw"]) > 0
            )
            transfer_row = {
                **transfer,
                "contract": contract,
                "tx_hash": tx["hash"],
                "transfer_from": from_address,
                "transfer_to": to_address,
                "is_candidate_victim_outflow": is_candidate_victim_outflow,
            }
            transfer_rows.append(transfer_row)

            if is_candidate_victim_outflow and address_key(to_address) in infrastructure_labels:
                receiver_hits.add(address_key(to_address))

    candidate_outflows = [
        row for row in transfer_rows
        if row["is_candidate_victim_outflow"]
    ]
    stable_outflows = [
        row for row in candidate_outflows
        if address_key(row["token"]) in {address_key(token) for token in STABLE_TOKENS}
    ]
    total_stable_amount = sum(
        (amount_from_raw(row["value_raw"], token_decimals.get(address_key(row["token"]), 18)) for row in stable_outflows),
        Decimal(0),
    )

    return {
        "successful_tx_count": len(txs),
        "transfer_event_count": len(transfer_rows),
        "candidate_outflow_count": len(candidate_outflows),
        "candidate_victim_count": len({address_key(row["transfer_from"]) for row in candidate_outflows}),
        "stable_outflow_count": len(stable_outflows),
        "stable_outflow_amount": decimal_string(total_stable_amount),
        "known_infrastructure_hits": len(receiver_hits),
        "known_infrastructure_addresses": ";".join(sorted(receiver_hits)),
        "contract_name": source.get("ContractName", ""),
        "source_verified": source.get("ABI", "") not in ("", "Contract source code not verified"),
        "compiler_version": source.get("CompilerVersion", ""),
        "transfer_rows": transfer_rows,
    }


def classify_risk(deployment, fingerprint, known_fingerprints, activity):
    if deployment["is_known_phishing"]:
        return "confirmed_known_phishing"

    if activity["successful_tx_count"] == "":
        return "not_scanned"

    bytecode_match = fingerprint["code_hash"] in known_fingerprints
    has_outflows = activity["candidate_outflow_count"] > 0
    has_stable_outflows = activity["stable_outflow_count"] > 0
    infra_hit = activity["known_infrastructure_hits"] > 0

    if bytecode_match and (has_stable_outflows or infra_hit):
        return "high_confidence_phishing"

    if has_stable_outflows or infra_hit or (bytecode_match and has_outflows):
        return "suspicious"

    if activity["successful_tx_count"] == 0:
        return "inactive_candidate"

    if has_outflows:
        return "low_signal_activity"

    return "inactive_candidate"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze contracts deployed by known phishing creators."
    )
    parser.add_argument(
        "--output-dir",
        default="data/candidate_contract_analysis",
        help="Directory for candidate contract outputs.",
    )
    parser.add_argument(
        "--receiver-dir",
        default="data/receiver_analysis",
    )
    parser.add_argument(
        "--downstream-dir",
        default="data/receiver_downstream",
    )
    parser.add_argument(
        "--next-hop-dir",
        default="data/downstream_next_hop",
    )
    parser.add_argument(
        "--skip-receipt-scan",
        action="store_true",
        help="Only build deployment catalog and bytecode/source metadata.",
    )
    parser.add_argument(
        "--min-tx-for-receipt-scan",
        type=int,
        default=1,
        help="Only scan receipts when successful tx count is at least this value.",
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
    cache_dir = output_dir / "cache"
    infrastructure_labels = load_infrastructure_labels(
        Path(args.receiver_dir),
        Path(args.downstream_dir),
        Path(args.next_hop_dir),
    )
    token_decimals = {
        address_key(row["token"]): int(row["token_decimals"])
        for row in read_csv(Path("data/victim_amount_analysis/token_amount_summary.csv"))
    }
    deployments = discover_deployments(KNOWN_PHISHING_CONTRACTS)
    known_fingerprints = set()
    fingerprints = {}

    for deployment in deployments:
        if deployment["is_known_phishing"]:
            fingerprint = bytecode_fingerprint(w3, deployment["contract"], cache_dir)
            fingerprints[address_key(deployment["contract"])] = fingerprint
            known_fingerprints.add(fingerprint["code_hash"])

    for deployment in deployments:
        if deployment["is_known_phishing"]:
            continue

        fingerprint = bytecode_fingerprint(w3, deployment["contract"], cache_dir)
        fingerprints[address_key(deployment["contract"])] = fingerprint

    catalog_rows = []
    transfer_rows = []

    for deployment in deployments:
        contract = deployment["contract"]
        fingerprint = fingerprints[address_key(contract)]
        bytecode_matches_known = fingerprint["code_hash"] in known_fingerprints

        if args.skip_receipt_scan:
            source = get_source_code(contract)
            activity = {
                "successful_tx_count": "",
                "transfer_event_count": "",
                "candidate_outflow_count": "",
                "candidate_victim_count": "",
                "stable_outflow_count": "",
                "stable_outflow_amount": "",
                "known_infrastructure_hits": "",
                "known_infrastructure_addresses": "",
                "contract_name": source.get("ContractName", ""),
                "source_verified": "",
                "compiler_version": source.get("CompilerVersion", ""),
                "transfer_rows": [],
            }
        else:
            print(f"Analyzing contract {contract}", flush=True)
            contract_txs = [tx for tx in get_transactions(contract) if is_successful(tx)]
            tx_count = len(contract_txs)

            if tx_count < args.min_tx_for_receipt_scan:
                source = get_source_code(contract)
                activity = {
                    "successful_tx_count": tx_count,
                    "transfer_event_count": 0,
                    "candidate_outflow_count": 0,
                    "candidate_victim_count": 0,
                    "stable_outflow_count": 0,
                    "stable_outflow_amount": "0",
                    "known_infrastructure_hits": 0,
                    "known_infrastructure_addresses": "",
                    "contract_name": source.get("ContractName", ""),
                    "source_verified": source.get("ABI", "") not in ("", "Contract source code not verified"),
                    "compiler_version": source.get("CompilerVersion", ""),
                    "transfer_rows": [],
                }
            else:
                activity = analyze_contract_activity(
                    w3,
                    {**deployment, "_cached_txs": contract_txs},
                    infrastructure_labels,
                    cache_dir,
                    token_decimals,
                )

        risk_label = classify_risk(deployment, fingerprint, known_fingerprints, activity)
        catalog_rows.append({
            "contract": contract,
            "creator": deployment["creator"],
            "marker": deployment["marker"],
            "risk_label": risk_label,
            "deploy_time": deployment.get("deploy_time", ""),
            "deploy_block": deployment.get("deploy_block", ""),
            "deploy_tx": deployment["deploy_tx"],
            "bytecode_hash": fingerprint["code_hash"],
            "bytecode_length": fingerprint["code_length"],
            "bytecode_matches_known_phishing": bytecode_matches_known,
            "contract_name": activity["contract_name"],
            "source_verified": activity["source_verified"],
            "compiler_version": activity["compiler_version"],
            "successful_tx_count": activity["successful_tx_count"],
            "transfer_event_count": activity["transfer_event_count"],
            "candidate_outflow_count": activity["candidate_outflow_count"],
            "candidate_victim_count": activity["candidate_victim_count"],
            "stable_outflow_count": activity["stable_outflow_count"],
            "stable_outflow_amount": activity["stable_outflow_amount"],
            "known_infrastructure_hits": activity["known_infrastructure_hits"],
            "known_infrastructure_addresses": activity["known_infrastructure_addresses"],
        })
        transfer_rows.extend(activity["transfer_rows"])

    risk_summary = defaultdict(int)

    for row in catalog_rows:
        risk_summary[row["risk_label"]] += 1

    write_csv(
        output_dir / "candidate_contract_catalog.csv",
        catalog_rows,
        list(catalog_rows[0].keys()) if catalog_rows else [],
    )
    write_csv(
        output_dir / "candidate_contract_transfers.csv",
        transfer_rows,
        [
            "contract",
            "token",
            "from",
            "to",
            "value_raw",
            "tx_hash",
            "transfer_from",
            "transfer_to",
            "is_candidate_victim_outflow",
        ],
    )
    write_csv(
        output_dir / "risk_summary.csv",
        [{"risk_label": label, "contract_count": count} for label, count in sorted(risk_summary.items())],
        ["risk_label", "contract_count"],
    )

    print("Contracts analyzed:", len(catalog_rows), flush=True)
    print("Risk summary:", dict(risk_summary), flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
