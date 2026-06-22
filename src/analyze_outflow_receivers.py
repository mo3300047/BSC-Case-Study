import argparse
import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


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


def sum_amount(rows):
    return sum((Decimal(row["amount"]) for row in rows), Decimal(0))


def address_key(address):
    return address.lower()


def load_spender_labels(spender_rows):
    labels = {}

    for row in spender_rows:
        labels[address_key(row["spender"])] = "known_phishing_spender"
        labels[address_key(row["creator"])] = "known_phishing_creator"

    return labels


def label_receiver(receiver, rows, labels):
    key = address_key(receiver)

    if key in labels:
        return labels[key]

    victim_addresses = {address_key(row["transfer_from"]) for row in rows}
    spender_addresses = {address_key(row["spender"]) for row in rows}
    creator_addresses = {address_key(row["spender_creator"]) for row in rows}

    if key in victim_addresses:
        return "also_victim"

    if key in spender_addresses:
        return "known_phishing_spender"

    if key in creator_addresses:
        return "known_phishing_creator"

    return "receiver"


def summarize_groups(rows, group_fields, labels=None):
    groups = defaultdict(list)

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
            "amount": decimal_string(sum_amount(group_rows)),
            "transfer_count": len(group_rows),
            "victim_count": len({row["transfer_from"] for row in group_rows}),
            "token_count": len({row["token"] for row in group_rows}),
            "spender_count": len({row["spender"] for row in group_rows}),
            "first_seen": min(row["datetime"] for row in group_rows),
            "last_seen": max(row["datetime"] for row in group_rows),
        })

        if group_fields == ["transfer_to"]:
            summary["receiver_label"] = label_receiver(key[0], rows, labels or {})
            summary["token_symbols"] = ";".join(sorted({row["token_symbol"] for row in group_rows}))
            summary["spenders"] = ";".join(sorted({row["spender"] for row in group_rows}))

        summary_rows.append(summary)

    return sorted(
        summary_rows,
        key=lambda row: (Decimal(row["amount"]), row["transfer_count"]),
        reverse=True,
    )


def build_receiver_token_rows(rows, receiver_labels):
    groups = defaultdict(list)

    for row in rows:
        key = (row["transfer_to"], row["token"], row["token_symbol"])
        groups[key].append(row)

    summary_rows = []
    for (receiver, token, symbol), group_rows in groups.items():
        summary_rows.append({
            "transfer_to": receiver,
            "receiver_label": receiver_labels[address_key(receiver)],
            "token": token,
            "token_symbol": symbol,
            "amount": decimal_string(sum_amount(group_rows)),
            "transfer_count": len(group_rows),
            "victim_count": len({row["transfer_from"] for row in group_rows}),
            "spender_count": len({row["spender"] for row in group_rows}),
            "first_seen": min(row["datetime"] for row in group_rows),
            "last_seen": max(row["datetime"] for row in group_rows),
        })

    return sorted(
        summary_rows,
        key=lambda row: (Decimal(row["amount"]), row["transfer_count"]),
        reverse=True,
    )


def build_top_receiver_details(rows, receiver_rows, top_count):
    top_receivers = {row["transfer_to"] for row in receiver_rows[:top_count]}
    detail_rows = [
        row for row in rows
        if row["transfer_to"] in top_receivers
    ]

    return sorted(
        detail_rows,
        key=lambda row: (row["transfer_to"], Decimal(row["amount"])),
        reverse=True,
    )


def top_receiver_amount(receiver_rows, count):
    return sum(
        (Decimal(row["amount"]) for row in receiver_rows[:count]),
        Decimal(0),
    )


def amount_share(part, total):
    if total == 0:
        return Decimal(0)

    return part / total


def parse_args():
    parser = argparse.ArgumentParser(
        description="Aggregate normalized victim outflows by receiver address."
    )
    parser.add_argument(
        "--amount-dir",
        default="data/victim_amount_analysis",
        help="Directory containing normalized_victim_outflows.csv.",
    )
    parser.add_argument(
        "--receipt-dir",
        default="data/victim_receipt_analysis",
        help="Directory containing spenders.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/receiver_analysis",
        help="Directory for receiver analysis outputs.",
    )
    parser.add_argument(
        "--top-receiver-detail-count",
        type=int,
        default=20,
        help="Number of top receivers to include in detail output.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    amount_dir = Path(args.amount_dir)
    receipt_dir = Path(args.receipt_dir)
    output_dir = Path(args.output_dir)
    rows = read_csv(amount_dir / "normalized_victim_outflows.csv")
    spender_rows = read_csv(receipt_dir / "spenders.csv")
    labels = load_spender_labels(spender_rows)
    receiver_rows = summarize_groups(rows, ["transfer_to"], labels)
    receiver_labels = {
        address_key(row["transfer_to"]): row["receiver_label"]
        for row in receiver_rows
    }
    receiver_token_rows = build_receiver_token_rows(rows, receiver_labels)
    daily_receiver_rows = summarize_groups(
        [{**row, "date": row["datetime"][:10]} for row in rows],
        ["date", "transfer_to"],
        labels,
    )
    top_receiver_details = build_top_receiver_details(
        rows,
        receiver_rows,
        args.top_receiver_detail_count,
    )
    total_amount = sum_amount(rows)
    top1_amount = Decimal(receiver_rows[0]["amount"]) if receiver_rows else Decimal(0)
    top3_amount = top_receiver_amount(receiver_rows, 3)
    top5_amount = top_receiver_amount(receiver_rows, 5)
    overall_rows = [{
        "receiver_count": len(receiver_rows),
        "amount": decimal_string(total_amount),
        "transfer_count": len(rows),
        "victim_count": len({row["transfer_from"] for row in rows}),
        "token_count": len({row["token"] for row in rows}),
        "spender_count": len({row["spender"] for row in rows}),
        "top_receiver": receiver_rows[0]["transfer_to"] if receiver_rows else "",
        "top_receiver_amount": decimal_string(top1_amount),
        "top_receiver_share": decimal_string(amount_share(top1_amount, total_amount)),
        "top3_amount": decimal_string(top3_amount),
        "top3_share": decimal_string(amount_share(top3_amount, total_amount)),
        "top5_amount": decimal_string(top5_amount),
        "top5_share": decimal_string(amount_share(top5_amount, total_amount)),
    }]

    write_csv(
        output_dir / "receiver_summary.csv",
        receiver_rows,
        [
            "transfer_to",
            "receiver_label",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "first_seen",
            "last_seen",
            "token_symbols",
            "spenders",
        ],
    )
    write_csv(
        output_dir / "receiver_token_summary.csv",
        receiver_token_rows,
        [
            "transfer_to",
            "receiver_label",
            "token",
            "token_symbol",
            "amount",
            "transfer_count",
            "victim_count",
            "spender_count",
            "first_seen",
            "last_seen",
        ],
    )
    write_csv(
        output_dir / "daily_receiver_summary.csv",
        daily_receiver_rows,
        [
            "date",
            "transfer_to",
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
        output_dir / "top_receiver_outflows.csv",
        top_receiver_details,
        list(top_receiver_details[0].keys()) if top_receiver_details else [],
    )
    write_csv(
        output_dir / "receiver_overall_summary.csv",
        overall_rows,
        [
            "receiver_count",
            "amount",
            "transfer_count",
            "victim_count",
            "token_count",
            "spender_count",
            "top_receiver",
            "top_receiver_amount",
            "top_receiver_share",
            "top3_amount",
            "top3_share",
            "top5_amount",
            "top5_share",
        ],
    )

    print("Receivers:", len(receiver_rows), flush=True)
    print("Total amount:", overall_rows[0]["amount"], flush=True)
    print("Top receiver:", overall_rows[0]["top_receiver"], flush=True)
    print("Top receiver amount:", overall_rows[0]["top_receiver_amount"], flush=True)
    print("Top receiver share:", overall_rows[0]["top_receiver_share"], flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
