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


def build_receiver_token_pairs(receiver_rows):
    pairs = []

    for row in receiver_rows:
        for token_symbol in row["token_symbols"].split(";"):
            pairs.append({
                "receiver": row["transfer_to"],
                "receiver_label": row["receiver_label"],
                "observed_in_amount": row["amount"],
                "observed_transfer_count": row["transfer_count"],
                "observed_victim_count": row["victim_count"],
                "first_observed_in": row["first_seen"],
                "last_observed_in": row["last_seen"],
                "token_symbol": token_symbol,
            })

    return pairs


def add_token_addresses(pairs, token_rows):
    token_by_symbol = {
        row["token_symbol"]: row
        for row in token_rows
    }

    for pair in pairs:
        token = token_by_symbol[pair["token_symbol"]]
        pair["token"] = token["token"]
        pair["token_decimals"] = token["token_decimals"]

    return pairs


def normalize_transfer(tx, receiver, token_symbol, token_decimals, observed_first_block):
    amount = amount_from_raw(tx["value"], token_decimals)
    direction = "incoming"

    if address_key(tx["from"]) == address_key(receiver):
        direction = "outgoing"

    return {
        "receiver": receiver,
        "direction": direction,
        "token": tx["contractAddress"],
        "token_symbol": token_symbol,
        "token_decimals": tx.get("tokenDecimal", token_decimals),
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


def sum_amount(rows):
    return sum((Decimal(row["amount"]) for row in rows), Decimal(0))


def label_counterparty(address, receiver_labels, victim_addresses):
    key = address_key(address)

    if key in receiver_labels:
        return receiver_labels[key]

    if key in victim_addresses:
        return "known_victim"

    return "downstream_recipient"


def summarize_receiver_flows(pairs, transfer_rows):
    rows_by_receiver = defaultdict(list)

    for row in transfer_rows:
        rows_by_receiver[address_key(row["receiver"])].append(row)

    summaries = []
    for pair in pairs:
        rows = rows_by_receiver[address_key(pair["receiver"])]
        outgoing = [row for row in rows if row["direction"] == "outgoing" and row["is_after_observed_start"]]
        incoming = [row for row in rows if row["direction"] == "incoming" and row["is_after_observed_start"]]
        observed_in = Decimal(pair["observed_in_amount"])
        outgoing_amount = sum_amount(outgoing)
        incoming_amount = sum_amount(incoming)

        summaries.append({
            "receiver": pair["receiver"],
            "receiver_label": pair["receiver_label"],
            "token": pair["token"],
            "token_symbol": pair["token_symbol"],
            "observed_victim_in_amount": pair["observed_in_amount"],
            "all_incoming_after_start_amount": decimal_string(incoming_amount),
            "outgoing_after_start_amount": decimal_string(outgoing_amount),
            "net_after_observed_in": decimal_string(observed_in - outgoing_amount),
            "outgoing_transfer_count": len(outgoing),
            "outgoing_counterparty_count": len({address_key(row["to"]) for row in outgoing}),
            "observed_victim_count": pair["observed_victim_count"],
            "first_observed_in": pair["first_observed_in"],
            "last_observed_in": pair["last_observed_in"],
        })

    return sorted(summaries, key=lambda row: Decimal(row["observed_victim_in_amount"]), reverse=True)


def summarize_downstream_recipients(transfer_rows, receiver_labels, victim_addresses):
    outgoing = [
        row for row in transfer_rows
        if row["direction"] == "outgoing" and row["is_after_observed_start"]
    ]
    groups = defaultdict(list)

    for row in outgoing:
        groups[(row["to"], row["token_symbol"])].append(row)

    summaries = []
    for (recipient, token_symbol), rows in groups.items():
        summaries.append({
            "recipient": recipient,
            "recipient_label": label_counterparty(recipient, receiver_labels, victim_addresses),
            "token_symbol": token_symbol,
            "amount": decimal_string(sum_amount(rows)),
            "transfer_count": len(rows),
            "source_receiver_count": len({address_key(row["receiver"]) for row in rows}),
            "source_receivers": ";".join(sorted({row["receiver"] for row in rows})),
            "first_seen": min(row["timestamp"] for row in rows),
            "last_seen": max(row["timestamp"] for row in rows),
        })

    return sorted(summaries, key=lambda row: Decimal(row["amount"]), reverse=True)


def summarize_downstream_recipients_all_tokens(transfer_rows, receiver_labels, victim_addresses):
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
            "recipient_label": label_counterparty(recipient, receiver_labels, victim_addresses),
            "amount": decimal_string(sum_amount(rows)),
            "transfer_count": len(rows),
            "token_count": len({row["token"] for row in rows}),
            "token_symbols": ";".join(sorted({row["token_symbol"] for row in rows})),
            "source_receiver_count": len({address_key(row["receiver"]) for row in rows}),
            "source_receivers": ";".join(sorted({row["receiver"] for row in rows})),
            "first_seen": min(row["datetime"] for row in rows),
            "last_seen": max(row["datetime"] for row in rows),
        })

    return sorted(summaries, key=lambda row: Decimal(row["amount"]), reverse=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace downstream token transfers from victim outflow receivers."
    )
    parser.add_argument(
        "--receiver-dir",
        default="data/receiver_analysis",
        help="Directory containing receiver_summary.csv.",
    )
    parser.add_argument(
        "--amount-dir",
        default="data/victim_amount_analysis",
        help="Directory containing token_amount_summary.csv and normalized_victim_outflows.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/receiver_downstream",
        help="Directory for downstream tracing outputs.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    receiver_dir = Path(args.receiver_dir)
    amount_dir = Path(args.amount_dir)
    output_dir = Path(args.output_dir)
    receiver_rows = read_csv(receiver_dir / "receiver_summary.csv")
    token_rows = read_csv(amount_dir / "token_amount_summary.csv")
    victim_rows = read_csv(amount_dir / "normalized_victim_outflows.csv")
    victim_addresses = {address_key(row["transfer_from"]) for row in victim_rows}
    receiver_labels = {
        address_key(row["transfer_to"]): row["receiver_label"]
        for row in receiver_rows
    }
    pairs = add_token_addresses(build_receiver_token_pairs(receiver_rows), token_rows)
    transfer_rows = []

    for pair in pairs:
        print("Fetching token transfers:", pair["receiver"], pair["token_symbol"], flush=True)
        observed_first_block = min(
            int(row["block_number"])
            for row in victim_rows
            if address_key(row["transfer_to"]) == address_key(pair["receiver"])
            and address_key(row["token"]) == address_key(pair["token"])
        )
        for tx in get_token_transfers(pair["receiver"], pair["token"]):
            transfer_rows.append(
                normalize_transfer(
                    tx,
                    pair["receiver"],
                    pair["token_symbol"],
                    pair["token_decimals"],
                    observed_first_block,
                )
            )

    receiver_flow_rows = summarize_receiver_flows(pairs, transfer_rows)
    downstream_rows = summarize_downstream_recipients(
        transfer_rows,
        receiver_labels,
        victim_addresses,
    )
    downstream_all_token_rows = summarize_downstream_recipients_all_tokens(
        transfer_rows,
        receiver_labels,
        victim_addresses,
    )
    outgoing_rows = [
        row for row in transfer_rows
        if row["direction"] == "outgoing" and row["is_after_observed_start"]
    ]
    overall_rows = [{
        "receiver_count": len(receiver_rows),
        "receiver_token_pair_count": len(pairs),
        "downstream_recipient_count": len({address_key(row["to"]) for row in outgoing_rows}),
        "outgoing_transfer_count": len(outgoing_rows),
        "outgoing_amount": decimal_string(sum_amount(outgoing_rows)),
        "top_downstream_recipient": downstream_all_token_rows[0]["recipient"] if downstream_all_token_rows else "",
        "top_downstream_amount": downstream_all_token_rows[0]["amount"] if downstream_all_token_rows else "0",
    }]

    write_csv(
        output_dir / "receiver_token_transfers.csv",
        transfer_rows,
        [
            "receiver",
            "direction",
            "token",
            "token_symbol",
            "token_decimals",
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
        output_dir / "receiver_flow_summary.csv",
        receiver_flow_rows,
        [
            "receiver",
            "receiver_label",
            "token",
            "token_symbol",
            "observed_victim_in_amount",
            "all_incoming_after_start_amount",
            "outgoing_after_start_amount",
            "net_after_observed_in",
            "outgoing_transfer_count",
            "outgoing_counterparty_count",
            "observed_victim_count",
            "first_observed_in",
            "last_observed_in",
        ],
    )
    write_csv(
        output_dir / "downstream_recipient_summary.csv",
        downstream_rows,
        [
            "recipient",
            "recipient_label",
            "token_symbol",
            "amount",
            "transfer_count",
            "source_receiver_count",
            "source_receivers",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "downstream_recipient_all_tokens_summary.csv",
        downstream_all_token_rows,
        [
            "recipient",
            "recipient_label",
            "amount",
            "transfer_count",
            "token_count",
            "token_symbols",
            "source_receiver_count",
            "source_receivers",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "downstream_overall_summary.csv",
        overall_rows,
        [
            "receiver_count",
            "receiver_token_pair_count",
            "downstream_recipient_count",
            "outgoing_transfer_count",
            "outgoing_amount",
            "top_downstream_recipient",
            "top_downstream_amount",
        ],
    )

    print("Downstream recipients:", overall_rows[0]["downstream_recipient_count"], flush=True)
    print("Outgoing amount:", overall_rows[0]["outgoing_amount"], flush=True)
    print("Top downstream recipient:", overall_rows[0]["top_downstream_recipient"], flush=True)
    print("Top downstream amount:", overall_rows[0]["top_downstream_amount"], flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
