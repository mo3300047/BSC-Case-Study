import argparse
import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, getcontext
from pathlib import Path

from requests import RequestException

from etherscan_client import etherscan_get

getcontext().prec = 80

PAGE_SIZE = 10000
MAX_BLOCK = 999999999


def read_csv(path):
    with path.open() as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def decimal_string(value):
    normalized = value.normalize()

    if normalized == normalized.to_integral():
        return format(normalized, "f")

    return format(normalized, "f").rstrip("0").rstrip(".")


def amount_from_raw(value_raw, decimals):
    return Decimal(value_raw) / (Decimal(10) ** int(decimals))


def address_key(address):
    return address.lower()


def format_time(timestamp):
    return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def sum_amount(rows):
    return sum((Decimal(row["amount"]) for row in rows), Decimal(0))


def get_token_transfers(address, token):
    transfers = []
    page = 1

    while True:
        try:
            data = etherscan_get({
                "module": "account",
                "action": "tokentx",
                "contractaddress": token,
                "address": address,
                "startblock": 0,
                "endblock": MAX_BLOCK,
                "page": page,
                "offset": PAGE_SIZE,
                "sort": "asc",
            })
        except RequestException as exc:
            raise RuntimeError(
                f"Network request failed for {address}/{token}: {exc.__class__.__name__}"
            ) from None

        if data.get("status") == "0" and data.get("message") == "No transactions found":
            return transfers

        if data.get("status") != "1":
            raise RuntimeError(f"API error for {address}/{token}: {data}")

        page_transfers = data["result"]
        transfers.extend(page_transfers)

        if len(page_transfers) < PAGE_SIZE:
            return transfers

        page += 1


def token_rows_by_symbol(token_rows):
    return {
        row["token_symbol"]: row
        for row in token_rows
    }


def build_targets(recipient_rows, receiver_transfer_rows, token_rows, top_count, min_amount):
    token_by_symbol = token_rows_by_symbol(token_rows)
    targets = []

    for recipient_row in recipient_rows:
        if len(targets) >= top_count:
            break

        if Decimal(recipient_row["amount"]) < Decimal(str(min_amount)):
            continue

        recipient = recipient_row["recipient"]
        for token_symbol in recipient_row["token_symbols"].split(";"):
            matching_inflows = [
                row for row in receiver_transfer_rows
                if address_key(row["to"]) == address_key(recipient)
                and row["token_symbol"] == token_symbol
                and row["direction"] == "outgoing"
                and row["is_after_observed_start"] == "True"
            ]

            if not matching_inflows:
                continue

            token = token_by_symbol[token_symbol]
            targets.append({
                "recipient": recipient,
                "recipient_rank_amount": recipient_row["amount"],
                "recipient_total_token_symbols": recipient_row["token_symbols"],
                "source_receiver_count": recipient_row["source_receiver_count"],
                "source_receivers": recipient_row["source_receivers"],
                "token": token["token"],
                "token_symbol": token_symbol,
                "token_decimals": token["token_decimals"],
                "observed_receiver_in_amount": decimal_string(sum_amount(matching_inflows)),
                "observed_receiver_in_transfer_count": len(matching_inflows),
                "observed_first_block": min(int(row["block_number"]) for row in matching_inflows),
                "observed_first_seen": min(row["datetime"] for row in matching_inflows),
                "observed_last_seen": max(row["datetime"] for row in matching_inflows),
            })

    return targets


def normalize_transfer(tx, target, observed_first_block):
    amount = amount_from_raw(tx["value"], target["token_decimals"])
    direction = "incoming"

    if address_key(tx["from"]) == address_key(target["recipient"]):
        direction = "outgoing"

    return {
        "target_recipient": target["recipient"],
        "direction": direction,
        "token": target["token"],
        "token_symbol": target["token_symbol"],
        "amount": decimal_string(amount),
        "value_raw": tx["value"],
        "from": tx["from"],
        "to": tx["to"],
        "counterparty": tx["to"] if direction == "outgoing" else tx["from"],
        "block_number": int(tx["blockNumber"]),
        "timestamp": int(tx["timeStamp"]),
        "datetime": format_time(tx["timeStamp"]),
        "tx_hash": tx["hash"],
        "is_after_observed_start": int(tx["blockNumber"]) >= observed_first_block,
    }


def summarize_targets(targets, transfer_rows):
    rows_by_target = defaultdict(list)

    for row in transfer_rows:
        rows_by_target[(address_key(row["target_recipient"]), row["token_symbol"])].append(row)

    summaries = []
    for target in targets:
        rows = rows_by_target[(address_key(target["recipient"]), target["token_symbol"])]
        incoming = [row for row in rows if row["direction"] == "incoming" and row["is_after_observed_start"]]
        outgoing = [row for row in rows if row["direction"] == "outgoing" and row["is_after_observed_start"]]
        observed_in = Decimal(target["observed_receiver_in_amount"])
        outgoing_amount = sum_amount(outgoing)

        summaries.append({
            "recipient": target["recipient"],
            "token_symbol": target["token_symbol"],
            "observed_receiver_in_amount": target["observed_receiver_in_amount"],
            "all_incoming_after_start_amount": decimal_string(sum_amount(incoming)),
            "outgoing_after_start_amount": decimal_string(outgoing_amount),
            "net_after_observed_in": decimal_string(observed_in - outgoing_amount),
            "outgoing_transfer_count": len(outgoing),
            "outgoing_counterparty_count": len({address_key(row["to"]) for row in outgoing}),
            "observed_receiver_in_transfer_count": target["observed_receiver_in_transfer_count"],
            "source_receiver_count": target["source_receiver_count"],
            "source_receivers": target["source_receivers"],
            "observed_first_seen": target["observed_first_seen"],
            "observed_last_seen": target["observed_last_seen"],
        })

    return sorted(summaries, key=lambda row: Decimal(row["observed_receiver_in_amount"]), reverse=True)


def summarize_next_hop_recipients(transfer_rows):
    outgoing = [
        row for row in transfer_rows
        if row["direction"] == "outgoing" and row["is_after_observed_start"]
    ]
    groups = defaultdict(list)

    for row in outgoing:
        groups[row["to"]].append(row)

    summaries = []
    for recipient, rows in groups.items():
        summaries.append({
            "recipient": recipient,
            "amount": decimal_string(sum_amount(rows)),
            "transfer_count": len(rows),
            "token_count": len({row["token"] for row in rows}),
            "token_symbols": ";".join(sorted({row["token_symbol"] for row in rows})),
            "source_downstream_count": len({address_key(row["target_recipient"]) for row in rows}),
            "source_downstreams": ";".join(sorted({row["target_recipient"] for row in rows})),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        })

    return sorted(summaries, key=lambda row: Decimal(row["amount"]), reverse=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace next-hop token transfers from top downstream recipients."
    )
    parser.add_argument(
        "--downstream-dir",
        default="data/receiver_downstream",
        help="Directory containing downstream recipient summaries.",
    )
    parser.add_argument(
        "--amount-dir",
        default="data/victim_amount_analysis",
        help="Directory containing token_amount_summary.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/downstream_next_hop",
        help="Directory for next-hop tracing outputs.",
    )
    parser.add_argument(
        "--top-count",
        type=int,
        default=10,
        help="Number of top downstream recipients to trace.",
    )
    parser.add_argument(
        "--min-amount",
        type=Decimal,
        default=Decimal("10000"),
        help="Minimum aggregate downstream recipient amount to trace.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    downstream_dir = Path(args.downstream_dir)
    amount_dir = Path(args.amount_dir)
    output_dir = Path(args.output_dir)
    recipient_rows = read_csv(downstream_dir / "downstream_recipient_all_tokens_summary.csv")
    receiver_transfer_rows = read_csv(downstream_dir / "receiver_token_transfers.csv")
    token_rows = read_csv(amount_dir / "token_amount_summary.csv")
    targets = build_targets(
        recipient_rows,
        receiver_transfer_rows,
        token_rows,
        args.top_count,
        args.min_amount,
    )
    transfer_rows = []

    for target in targets:
        print("Fetching next-hop token transfers:", target["recipient"], target["token_symbol"], flush=True)

        for tx in get_token_transfers(target["recipient"], target["token"]):
            transfer_rows.append(
                normalize_transfer(
                    tx,
                    target,
                    target["observed_first_block"],
                )
            )

    target_summary_rows = summarize_targets(targets, transfer_rows)
    next_hop_rows = summarize_next_hop_recipients(transfer_rows)
    outgoing_rows = [
        row for row in transfer_rows
        if row["direction"] == "outgoing" and row["is_after_observed_start"]
    ]
    overall_rows = [{
        "target_recipient_count": len({address_key(target["recipient"]) for target in targets}),
        "target_token_pair_count": len(targets),
        "next_hop_recipient_count": len(next_hop_rows),
        "outgoing_transfer_count": len(outgoing_rows),
        "outgoing_amount": decimal_string(sum_amount(outgoing_rows)),
        "top_next_hop_recipient": next_hop_rows[0]["recipient"] if next_hop_rows else "",
        "top_next_hop_amount": next_hop_rows[0]["amount"] if next_hop_rows else "0",
    }]

    write_csv(
        output_dir / "next_hop_token_transfers.csv",
        transfer_rows,
        [
            "target_recipient",
            "direction",
            "token",
            "token_symbol",
            "amount",
            "value_raw",
            "from",
            "to",
            "counterparty",
            "block_number",
            "timestamp",
            "datetime",
            "tx_hash",
            "is_after_observed_start",
        ],
    )
    write_csv(
        output_dir / "target_downstream_flow_summary.csv",
        target_summary_rows,
        [
            "recipient",
            "token_symbol",
            "observed_receiver_in_amount",
            "all_incoming_after_start_amount",
            "outgoing_after_start_amount",
            "net_after_observed_in",
            "outgoing_transfer_count",
            "outgoing_counterparty_count",
            "observed_receiver_in_transfer_count",
            "source_receiver_count",
            "source_receivers",
            "observed_first_seen",
            "observed_last_seen",
        ],
    )
    write_csv(
        output_dir / "next_hop_recipient_summary.csv",
        next_hop_rows,
        [
            "recipient",
            "amount",
            "transfer_count",
            "token_count",
            "token_symbols",
            "source_downstream_count",
            "source_downstreams",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "next_hop_overall_summary.csv",
        overall_rows,
        [
            "target_recipient_count",
            "target_token_pair_count",
            "next_hop_recipient_count",
            "outgoing_transfer_count",
            "outgoing_amount",
            "top_next_hop_recipient",
            "top_next_hop_amount",
        ],
    )

    print("Targets:", overall_rows[0]["target_recipient_count"], flush=True)
    print("Next-hop recipients:", overall_rows[0]["next_hop_recipient_count"], flush=True)
    print("Outgoing amount:", overall_rows[0]["outgoing_amount"], flush=True)
    print("Top next-hop recipient:", overall_rows[0]["top_next_hop_recipient"], flush=True)
    print("Top next-hop amount:", overall_rows[0]["top_next_hop_amount"], flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
