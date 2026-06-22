import argparse
import csv
import json
import os
from collections import defaultdict
from decimal import Decimal, getcontext
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
getcontext().prec = 80

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


def read_csv(path):
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


def decimal_to_string(value):
    normalized = value.normalize()

    if normalized == normalized.to_integral():
        return format(normalized, "f")

    return format(normalized, "f").rstrip("0").rstrip(".")


def get_token_metadata(w3, token_address):
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_ABI,
    )

    metadata = {
        "token": Web3.to_checksum_address(token_address),
        "name": "",
        "symbol": "",
        "decimals": None,
    }

    for key in ("name", "symbol", "decimals"):
        try:
            metadata[key] = getattr(contract.functions, key)().call()
        except Exception as exc:
            metadata[key] = ""
            metadata[f"{key}_error"] = exc.__class__.__name__

    if metadata["decimals"] == "":
        raise RuntimeError(f"Could not read decimals for token {token_address}: {metadata}")

    metadata["decimals"] = int(metadata["decimals"])

    return metadata


def load_token_metadata(w3, token_addresses, cache_path):
    cached = read_json(cache_path, {})
    metadata_by_token = {}

    for token in sorted(token_addresses):
        checksum_token = Web3.to_checksum_address(token)
        cached_metadata = cached.get(checksum_token)

        if cached_metadata is None:
            cached_metadata = get_token_metadata(w3, checksum_token)
            cached[checksum_token] = cached_metadata
            write_json(cache_path, cached)

        metadata_by_token[checksum_token] = cached_metadata

    return metadata_by_token


def amount_from_raw(value_raw, decimals):
    return Decimal(value_raw) / (Decimal(10) ** int(decimals))


def is_true(value):
    return str(value).lower() == "true"


def normalize_transfer_rows(rows, metadata_by_token):
    normalized_rows = []

    for row in rows:
        token = Web3.to_checksum_address(row["token"])
        metadata = metadata_by_token[token]
        amount = amount_from_raw(row["value_raw"], metadata["decimals"])

        normalized_rows.append({
            **row,
            "token": token,
            "token_name": metadata["name"],
            "token_symbol": metadata["symbol"],
            "token_decimals": metadata["decimals"],
            "amount": decimal_to_string(amount),
        })

    return normalized_rows


def sum_amount(rows):
    return sum(Decimal(row["amount"]) for row in rows)


def summarize(rows, group_fields, extra_fields=None):
    groups = defaultdict(list)
    extra_fields = extra_fields or []

    for row in rows:
        key = tuple(row[field] for field in group_fields)
        groups[key].append(row)

    summary_rows = []
    for key, group_rows in groups.items():
        summary = {
            field: value
            for field, value in zip(group_fields, key)
        }
        summary.update({
            "amount": decimal_to_string(sum_amount(group_rows)),
            "transfer_count": len(group_rows),
            "victim_count": len({row["transfer_from"] for row in group_rows}),
            "token_count": len({row["token"] for row in group_rows}),
            "spender_count": len({row["spender"] for row in group_rows}),
            "first_seen": min(row["datetime"] for row in group_rows),
            "last_seen": max(row["datetime"] for row in group_rows),
        })

        for field in extra_fields:
            summary[field] = group_rows[0][field]

        summary_rows.append(summary)

    return sorted(
        summary_rows,
        key=lambda row: (Decimal(row["amount"]), row["transfer_count"]),
        reverse=True,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Normalize victim outflow raw token amounts and write amount summaries."
    )
    parser.add_argument(
        "--input-dir",
        default="data/victim_receipt_analysis",
        help="Directory containing observed_token_transfers.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/victim_amount_analysis",
        help="Directory for normalized CSV outputs.",
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

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    input_path = input_dir / "observed_token_transfers.csv"
    rows = [
        row for row in read_csv(input_path)
        if is_true(row["is_candidate_victim_outflow"])
    ]
    token_addresses = {row["token"] for row in rows}
    metadata_by_token = load_token_metadata(
        w3,
        token_addresses,
        output_dir / "token_metadata.json",
    )
    normalized_rows = normalize_transfer_rows(rows, metadata_by_token)

    detail_fields = list(normalized_rows[0].keys()) if normalized_rows else []
    write_csv(output_dir / "normalized_victim_outflows.csv", normalized_rows, detail_fields)
    write_csv(
        output_dir / "token_amount_summary.csv",
        summarize(
            normalized_rows,
            ["token", "token_symbol"],
            ["token_name", "token_decimals"],
        ),
        [
            "token",
            "token_symbol",
            "token_name",
            "token_decimals",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "spender_amount_summary.csv",
        summarize(normalized_rows, ["spender"], ["spender_source", "spender_creator"]),
        [
            "spender",
            "spender_source",
            "spender_creator",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "victim_amount_summary.csv",
        summarize(normalized_rows, ["transfer_from"]),
        [
            "transfer_from",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "daily_amount_summary.csv",
        summarize(
            [
                {**row, "date": row["datetime"][:10]}
                for row in normalized_rows
            ],
            ["date"],
        ),
        [
            "date",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )

    total_amount = decimal_to_string(sum_amount(normalized_rows))
    write_csv(
        output_dir / "overall_summary.csv",
        [
            {
                "amount": total_amount,
                "transfer_count": len(normalized_rows),
                "victim_count": len({row["transfer_from"] for row in normalized_rows}),
                "token_count": len({row["token"] for row in normalized_rows}),
                "spender_count": len({row["spender"] for row in normalized_rows}),
                "first_seen": min(row["datetime"] for row in normalized_rows),
                "last_seen": max(row["datetime"] for row in normalized_rows),
            }
        ],
        [
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )
    print("Candidate victim outflows:", len(normalized_rows), flush=True)
    print("Tokens:", len(metadata_by_token), flush=True)
    print("Total normalized amount:", total_amount, flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
