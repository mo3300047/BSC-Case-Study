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
from web3.exceptions import Web3RPCError

from etherscan_client import etherscan_get, is_retryable_api_error

load_dotenv()

KNOWN_PHISHING_CONTRACTS = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

PAGE_SIZE = 10000
MAX_BLOCK = 999999999
UNLIMITED_APPROVAL_THRESHOLD = 2**255

def prefixed_hex(value):
    value_hex = value.hex() if hasattr(value, "hex") else value

    if value_hex.startswith("0x"):
        return value_hex

    return "0x" + value_hex


APPROVAL_TOPIC = prefixed_hex(Web3.keccak(text="Approval(address,address,uint256)"))
TRANSFER_TOPIC = prefixed_hex(Web3.keccak(text="Transfer(address,address,uint256)"))


class RetryableLogScanError(RuntimeError):
    pass


def normalize_address(address):
    return Web3.to_checksum_address(address)


def topic_for_address(address):
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


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
    return prefixed_hex(value)


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


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


def discover_spenders(known_contracts, include_creator_deployments):
    known_contracts = [normalize_address(contract) for contract in known_contracts]
    creations = get_contract_creations(known_contracts)
    spenders = {
        normalize_address(creation["contractAddress"]): {
            "source": "known_phishing",
            "creator": normalize_address(creation["contractCreator"]),
            "deploy_block": int(creation.get("blockNumber") or 0),
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
            if contract in spenders:
                continue

            spenders[contract] = {
                "source": "same_creator_candidate",
                "creator": creator,
                "deploy_block": int(tx["blockNumber"]),
                "deploy_tx": tx["hash"],
            }

    return spenders


def load_block_timestamps(path):
    if not path.exists():
        return {}

    with path.open() as file:
        return {int(block): int(timestamp) for block, timestamp in json.load(file).items()}


def save_block_timestamps(path, timestamps):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as file:
        json.dump(
            {str(block): timestamp for block, timestamp in sorted(timestamps.items())},
            file,
            indent=2,
        )


def get_block_timestamp(w3, block_number, timestamps):
    if block_number not in timestamps:
        block = w3.eth.get_block(block_number)
        timestamps[block_number] = int(block["timestamp"])

    return timestamps[block_number]


def get_logs_chunked(w3, event_filter, start_block, end_block, block_chunk):
    cursor = start_block

    while cursor <= end_block:
        chunk_end = min(cursor + block_chunk - 1, end_block)
        params = dict(event_filter)
        params["fromBlock"] = cursor
        params["toBlock"] = chunk_end

        try:
            logs = w3.eth.get_logs(params)
        except (ValueError, Web3RPCError):
            if cursor == chunk_end:
                raise

            smaller_chunk = max(1, (chunk_end - cursor + 1) // 2)
            yield from get_logs_chunked(w3, event_filter, cursor, chunk_end, smaller_chunk)
            cursor = chunk_end + 1
            continue

        for log in logs:
            yield log

        cursor = chunk_end + 1


def scan_approval_logs_rpc(w3, spenders, start_block, end_block, block_chunk, spender_batch_size):
    rows = []
    spender_topics = {
        topic_for_address(spender): spender
        for spender in spenders
    }

    for topic_batch in chunked(sorted(spender_topics), spender_batch_size):
        event_filter = {
            "topics": [APPROVAL_TOPIC, None, topic_batch],
        }

        for log in get_logs_chunked(w3, event_filter, start_block, end_block, block_chunk):
            owner = address_from_topic(log["topics"][1])
            spender = address_from_topic(log["topics"][2])
            value = int_from_data(log["data"])

            rows.append({
                "token": normalize_address(log["address"]),
                "owner": owner,
                "spender": spender,
                "value_raw": str(value),
                "is_unlimited": value >= UNLIMITED_APPROVAL_THRESHOLD,
                "is_revoke": value == 0,
                "block_number": int_from_rpc_value(log["blockNumber"]),
                "tx_hash": hex_value(log["transactionHash"]),
                "log_index": int_from_rpc_value(log["logIndex"]),
                "spender_source": spenders[spender]["source"],
                "spender_creator": spenders[spender]["creator"],
            })

    return sorted(rows, key=lambda row: (row["block_number"], row["log_index"]))


def etherscan_get_logs(params):
    data = etherscan_get({
        "module": "logs",
        "action": "getLogs",
        **params,
    })

    if data.get("status") == "0" and data.get("message") == "No records found":
        return []

    if is_retryable_api_error(data):
        raise RetryableLogScanError(f"Retryable Etherscan log error: {data}")

    if data.get("status") != "1":
        raise RuntimeError(f"API error while getting logs: {data}")

    return data["result"]


def get_etherscan_logs_chunked(spender_topic, start_block, end_block, block_chunk):
    cursor = start_block
    chunks_scanned = 0

    while cursor <= end_block:
        chunk_end = min(cursor + block_chunk - 1, end_block)
        try:
            logs = etherscan_get_logs({
                "fromBlock": cursor,
                "toBlock": chunk_end,
                "topic0": APPROVAL_TOPIC,
                "topic2": spender_topic,
                "topic0_2_opr": "and",
            })
        except RetryableLogScanError:
            if cursor == chunk_end:
                raise

            smaller_chunk = max(1, (chunk_end - cursor + 1) // 2)
            yield from get_etherscan_logs_chunked(spender_topic, cursor, chunk_end, smaller_chunk)
            cursor = chunk_end + 1
            continue

        if len(logs) >= 1000 and cursor < chunk_end:
            smaller_chunk = max(1, (chunk_end - cursor + 1) // 2)
            yield from get_etherscan_logs_chunked(spender_topic, cursor, chunk_end, smaller_chunk)
        else:
            yield from logs

        chunks_scanned += 1
        if chunks_scanned % 100 == 0:
            print(f"  scanned through block {chunk_end}", flush=True)

        cursor = chunk_end + 1


def scan_approval_logs_etherscan(spenders, start_block, end_block, block_chunk):
    rows = []

    for spender in sorted(spenders):
        print("Scanning approvals for spender:", spender, flush=True)
        spender_topic = topic_for_address(spender)

        for log in get_etherscan_logs_chunked(spender_topic, start_block, end_block, block_chunk):
            owner = address_from_topic(log["topics"][1])
            spender = address_from_topic(log["topics"][2])
            value = int_from_data(log["data"])

            rows.append({
                "token": normalize_address(log["address"]),
                "owner": owner,
                "spender": spender,
                "value_raw": str(value),
                "is_unlimited": value >= UNLIMITED_APPROVAL_THRESHOLD,
                "is_revoke": value == 0,
                "block_number": int_from_rpc_value(log["blockNumber"]),
                "tx_hash": hex_value(log["transactionHash"]),
                "log_index": int_from_rpc_value(log["logIndex"]),
                "spender_source": spenders[spender]["source"],
                "spender_creator": spenders[spender]["creator"],
            })

    return sorted(rows, key=lambda row: (row["block_number"], row["log_index"]))


def add_timestamps(w3, rows, timestamp_cache_path):
    timestamps = load_block_timestamps(timestamp_cache_path)

    for row in rows:
        timestamp = get_block_timestamp(w3, row["block_number"], timestamps)
        row["timestamp"] = timestamp
        row["datetime"] = format_time(timestamp)

    save_block_timestamps(timestamp_cache_path, timestamps)


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_approvals(approval_rows):
    active_approvals = [row for row in approval_rows if not row["is_revoke"]]
    by_owner = defaultdict(list)
    by_token = defaultdict(list)
    by_spender = defaultdict(list)
    by_day = defaultdict(list)

    for row in active_approvals:
        by_owner[row["owner"]].append(row)
        by_token[row["token"]].append(row)
        by_spender[row["spender"]].append(row)
        by_day[row["datetime"][:10]].append(row)

    victim_rows = [
        {
            "owner": owner,
            "approval_count": len(rows),
            "token_count": len({row["token"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
            "unlimited_approval_count": sum(row["is_unlimited"] for row in rows),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        }
        for owner, rows in by_owner.items()
    ]
    token_rows = [
        {
            "token": token,
            "approval_count": len(rows),
            "victim_count": len({row["owner"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
            "unlimited_approval_count": sum(row["is_unlimited"] for row in rows),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        }
        for token, rows in by_token.items()
    ]
    spender_rows = [
        {
            "spender": spender,
            "approval_count": len(rows),
            "victim_count": len({row["owner"] for row in rows}),
            "token_count": len({row["token"] for row in rows}),
            "unlimited_approval_count": sum(row["is_unlimited"] for row in rows),
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
            "approval_count": len(rows),
            "victim_count": len({row["owner"] for row in rows}),
            "token_count": len({row["token"] for row in rows}),
            "spender_count": len({row["spender"] for row in rows}),
            "unlimited_approval_count": sum(row["is_unlimited"] for row in rows),
        }
        for day, rows in by_day.items()
    ]

    return {
        "victims": sorted(victim_rows, key=lambda row: row["approval_count"], reverse=True),
        "tokens": sorted(token_rows, key=lambda row: row["victim_count"], reverse=True),
        "spenders": sorted(spender_rows, key=lambda row: row["victim_count"], reverse=True),
        "daily": sorted(daily_rows, key=lambda row: row["date"]),
    }


def latest_approval_by_owner_token(approval_rows):
    approvals = defaultdict(list)

    for row in approval_rows:
        if row["is_revoke"]:
            continue

        approvals[(row["owner"], row["token"])].append(row)

    for rows in approvals.values():
        rows.sort(key=lambda row: (row["block_number"], row["log_index"]))

    return approvals


def find_latest_approval(approvals, owner, token, block_number, log_index):
    candidates = approvals.get((owner, token), [])
    latest = None

    for row in candidates:
        if (row["block_number"], row["log_index"]) >= (block_number, log_index):
            break
        latest = row

    return latest


def scan_transfer_logs(w3, approval_rows, end_block, block_chunk, owner_batch_size):
    approvals = latest_approval_by_owner_token(approval_rows)
    owners_by_token = defaultdict(set)
    min_block_by_token = {}

    for owner, token in approvals:
        owners_by_token[token].add(owner)
        first_block = approvals[(owner, token)][0]["block_number"]
        min_block_by_token[token] = min(first_block, min_block_by_token.get(token, first_block))

    transfer_rows = []

    for token, owners in sorted(owners_by_token.items()):
        for owner_batch in chunked(sorted(owners), owner_batch_size):
            event_filter = {
                "address": token,
                "topics": [
                    TRANSFER_TOPIC,
                    [topic_for_address(owner) for owner in owner_batch],
                ],
            }

            for log in get_logs_chunked(
                w3,
                event_filter,
                min_block_by_token[token],
                end_block,
                block_chunk,
            ):
                owner = address_from_topic(log["topics"][1])
                approval = find_latest_approval(
                    approvals,
                    owner,
                    token,
                    int(log["blockNumber"]),
                    int(log["logIndex"]),
                )

                if approval is None:
                    continue

                transfer_rows.append({
                    "token": token,
                    "owner": owner,
                    "to": address_from_topic(log["topics"][2]),
                    "value_raw": str(int_from_data(log["data"])),
                    "block_number": int(log["blockNumber"]),
                    "tx_hash": hex_value(log["transactionHash"]),
                    "log_index": int(log["logIndex"]),
                    "approval_tx_hash": approval["tx_hash"],
                    "approval_spender": approval["spender"],
                    "approval_block_number": approval["block_number"],
                    "blocks_after_approval": int(log["blockNumber"]) - approval["block_number"],
                })

    return sorted(transfer_rows, key=lambda row: (row["block_number"], row["log_index"]))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze victim approvals to known and same-creator phishing contracts."
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
        "--start-block",
        type=int,
        help="First block to scan. Defaults to earliest spender deployment block.",
    )
    parser.add_argument(
        "--end-block",
        type=int,
        help="Last block to scan. Defaults to latest chain block.",
    )
    parser.add_argument(
        "--block-chunk",
        type=int,
        default=50000,
        help="Log block range per request.",
    )
    parser.add_argument(
        "--log-source",
        choices=["etherscan", "rpc"],
        default="etherscan",
        help="Use Etherscan logs API or RPC eth_getLogs for Approval scanning.",
    )
    parser.add_argument(
        "--spender-batch-size",
        type=int,
        default=20,
        help="Number of spender topics per Approval log request.",
    )
    parser.add_argument(
        "--trace-transfers",
        action="store_true",
        help="Also scan later Transfer events from approved victims. This can be much heavier.",
    )
    parser.add_argument(
        "--owner-batch-size",
        type=int,
        default=50,
        help="Number of owner topics per Transfer log request when --trace-transfers is set.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/victim_analysis",
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
    end_block = args.end_block or w3.eth.block_number
    deploy_blocks = [
        metadata["deploy_block"]
        for metadata in spenders.values()
        if metadata["deploy_block"]
    ]

    if args.start_block is None and not deploy_blocks:
        raise RuntimeError(
            "Could not infer start block from Etherscan. Pass --start-block explicitly."
        )

    start_block = args.start_block or min(deploy_blocks)

    print("Spenders analyzed:", len(spenders), flush=True)
    print("Block range:", start_block, end_block, flush=True)
    print("Approval log source:", args.log_source, flush=True)
    print("Output directory:", output_dir, flush=True)

    spender_rows = [
        {"spender": spender, **metadata}
        for spender, metadata in sorted(spenders.items())
    ]
    write_csv(
        output_dir / "spenders.csv",
        spender_rows,
        ["spender", "source", "creator", "deploy_block", "deploy_tx"],
    )

    if args.log_source == "rpc":
        approval_rows = scan_approval_logs_rpc(
            w3,
            spenders,
            start_block,
            end_block,
            args.block_chunk,
            args.spender_batch_size,
        )
    else:
        approval_rows = scan_approval_logs_etherscan(
            spenders,
            start_block,
            end_block,
            args.block_chunk,
        )
    add_timestamps(w3, approval_rows, output_dir / "block_timestamps.json")
    summary = summarize_approvals(approval_rows)

    write_csv(
        output_dir / "approvals.csv",
        approval_rows,
        [
            "token",
            "owner",
            "spender",
            "value_raw",
            "is_unlimited",
            "is_revoke",
            "block_number",
            "tx_hash",
            "log_index",
            "timestamp",
            "datetime",
            "spender_source",
            "spender_creator",
        ],
    )
    write_csv(
        output_dir / "victim_summary.csv",
        summary["victims"],
        [
            "owner",
            "approval_count",
            "token_count",
            "spender_count",
            "unlimited_approval_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "token_summary.csv",
        summary["tokens"],
        [
            "token",
            "approval_count",
            "victim_count",
            "spender_count",
            "unlimited_approval_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "spender_summary.csv",
        summary["spenders"],
        [
            "spender",
            "approval_count",
            "victim_count",
            "token_count",
            "unlimited_approval_count",
            "source",
            "creator",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "daily_summary.csv",
        summary["daily"],
        [
            "date",
            "approval_count",
            "victim_count",
            "token_count",
            "spender_count",
            "unlimited_approval_count",
        ],
    )

    if args.trace_transfers:
        transfer_rows = scan_transfer_logs(
            w3,
            approval_rows,
            end_block,
            args.block_chunk,
            args.owner_batch_size,
        )
        add_timestamps(w3, transfer_rows, output_dir / "block_timestamps.json")
        write_csv(
            output_dir / "candidate_transfer_outflows.csv",
            transfer_rows,
            [
                "token",
                "owner",
                "to",
                "value_raw",
                "block_number",
                "tx_hash",
                "log_index",
                "timestamp",
                "datetime",
                "approval_tx_hash",
                "approval_spender",
                "approval_block_number",
                "blocks_after_approval",
            ],
        )

    print("Approval rows:", len(approval_rows), flush=True)
    print("Victims:", len(summary["victims"]), flush=True)
    print("Tokens:", len(summary["tokens"]), flush=True)
    print("Spenders with approvals:", len(summary["spenders"]), flush=True)


if __name__ == "__main__":
    main()
