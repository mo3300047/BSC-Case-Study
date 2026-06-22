import argparse
import csv
import json
import random
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def read_csv(path):
    with path.open() as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_receipt(receipt_dir, tx_hash):
    path = receipt_dir / f"{tx_hash.lower()}.json"

    if not path.exists():
        return None

    with path.open() as file:
        return json.load(file)


def normalize_address(address):
    return address.lower()


def topic_for_address(address):
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def raw_value_hex(value_raw):
    return "0x" + int(value_raw).to_bytes(32, "big").hex()


def decimal_string(value):
    normalized = value.normalize()

    if normalized == normalized.to_integral():
        return format(normalized, "f")

    return format(normalized, "f").rstrip("0").rstrip(".")


def find_exact_transfer_log(row, receipt):
    expected = {
        "address": normalize_address(row["token"]),
        "logIndex": int(row["log_index"]),
        "topic0": TRANSFER_TOPIC,
        "topic1": topic_for_address(row["transfer_from"]),
        "topic2": topic_for_address(row["transfer_to"]),
        "data": raw_value_hex(row["value_raw"]),
    }

    for log in receipt.get("logs", []):
        topics = [topic.lower() for topic in log.get("topics", [])]

        if (
            normalize_address(log.get("address", "")) == expected["address"]
            and int(log.get("logIndex")) == expected["logIndex"]
            and len(topics) >= 3
            and topics[0] == expected["topic0"]
            and topics[1] == expected["topic1"]
            and topics[2] == expected["topic2"]
            and log.get("data", "").lower() == expected["data"]
        ):
            return log

    return None


def validate_row(row, receipt_dir):
    receipt = read_receipt(receipt_dir, row["tx_hash"])
    exact_log = find_exact_transfer_log(row, receipt) if receipt else None
    transfer_from = normalize_address(row["transfer_from"])
    transfer_to = normalize_address(row["transfer_to"])
    tx_from = normalize_address(row["from"])
    tx_to = normalize_address(row["to"])
    spender = normalize_address(row["spender"])
    creator = normalize_address(row["spender_creator"])

    checks = {
        "receipt_found": receipt is not None,
        "receipt_success": bool(receipt and int(receipt.get("status", 0)) == 1),
        "exact_transfer_log_found": exact_log is not None,
        "transfer_from_nonzero": transfer_from != ZERO_ADDRESS,
        "transfer_from_not_spender": transfer_from != spender,
        "transfer_to_not_self": transfer_to != transfer_from,
        "tx_sender_matches_transfer_from": tx_from == transfer_from,
        "transfer_to_matches_tx_to": tx_to == transfer_to,
        "transfer_to_matches_creator": transfer_to == creator,
        "tx_to_matches_creator": tx_to == creator,
    }

    qa_status = "pass"
    if not all(
        checks[key]
        for key in [
            "receipt_found",
            "receipt_success",
            "exact_transfer_log_found",
            "transfer_from_nonzero",
            "transfer_from_not_spender",
            "transfer_to_not_self",
        ]
    ):
        qa_status = "fail"
    elif not checks["tx_sender_matches_transfer_from"]:
        qa_status = "review"

    return {
        **row,
        **{key: str(value) for key, value in checks.items()},
        "qa_status": qa_status,
    }


def select_samples(rows, top_amount_count, top_victim_count, random_count, seed):
    samples = {}

    for row in sorted(rows, key=lambda item: Decimal(item["amount"]), reverse=True)[:top_amount_count]:
        samples[row["tx_hash"], row["log_index"]] = {**row, "sample_reason": "top_amount"}

    by_victim = defaultdict(list)
    for row in rows:
        by_victim[row["transfer_from"]].append(row)

    top_victims = sorted(
        by_victim.items(),
        key=lambda item: sum(Decimal(row["amount"]) for row in item[1]),
        reverse=True,
    )[:top_victim_count]

    for _, victim_rows in top_victims:
        row = max(victim_rows, key=lambda item: Decimal(item["amount"]))
        key = (row["tx_hash"], row["log_index"])
        reason = samples.get(key, {}).get("sample_reason")
        samples[key] = {
            **row,
            "sample_reason": "top_victim" if reason is None else f"{reason};top_victim",
        }

    rng = random.Random(seed)
    for row in rng.sample(rows, min(random_count, len(rows))):
        key = (row["tx_hash"], row["log_index"])
        reason = samples.get(key, {}).get("sample_reason")
        samples[key] = {
            **row,
            "sample_reason": "random" if reason is None else f"{reason};random",
        }

    return list(samples.values())


def summarize(validated_rows):
    total_amount = sum((Decimal(row["amount"]) for row in validated_rows), Decimal(0))
    failed = [row for row in validated_rows if row["qa_status"] == "fail"]
    review = [row for row in validated_rows if row["qa_status"] == "review"]
    passed = [row for row in validated_rows if row["qa_status"] == "pass"]

    return [{
        "row_count": len(validated_rows),
        "pass_count": len(passed),
        "review_count": len(review),
        "fail_count": len(failed),
        "exact_transfer_log_found_count": sum(row["exact_transfer_log_found"] == "True" for row in validated_rows),
        "tx_sender_matches_transfer_from_count": sum(row["tx_sender_matches_transfer_from"] == "True" for row in validated_rows),
        "transfer_to_matches_creator_count": sum(row["transfer_to_matches_creator"] == "True" for row in validated_rows),
        "total_amount": decimal_string(total_amount),
        "review_amount": decimal_string(sum((Decimal(row["amount"]) for row in review), Decimal(0))),
        "fail_amount": decimal_string(sum((Decimal(row["amount"]) for row in failed), Decimal(0))),
    }]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate and sample normalized victim outflows against cached receipts."
    )
    parser.add_argument(
        "--amount-dir",
        default="data/victim_amount_analysis",
        help="Directory containing normalized_victim_outflows.csv.",
    )
    parser.add_argument(
        "--receipt-dir",
        default="data/victim_receipt_analysis/receipts",
        help="Directory containing cached transaction receipt JSON files.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/victim_qa",
        help="Directory for QA outputs.",
    )
    parser.add_argument("--top-amount-count", type=int, default=20)
    parser.add_argument("--top-victim-count", type=int, default=20)
    parser.add_argument("--random-count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260622)

    return parser.parse_args()


def main():
    args = parse_args()
    amount_dir = Path(args.amount_dir)
    receipt_dir = Path(args.receipt_dir)
    output_dir = Path(args.output_dir)
    rows = read_csv(amount_dir / "normalized_victim_outflows.csv")
    validated_rows = [validate_row(row, receipt_dir) for row in rows]
    sample_rows = [
        validate_row(row, receipt_dir)
        for row in select_samples(
            rows,
            args.top_amount_count,
            args.top_victim_count,
            args.random_count,
            args.seed,
        )
    ]
    sample_reasons = {
        (row["tx_hash"], row["log_index"]): row["sample_reason"]
        for row in select_samples(
            rows,
            args.top_amount_count,
            args.top_victim_count,
            args.random_count,
            args.seed,
        )
    }

    for row in sample_rows:
        row["sample_reason"] = sample_reasons[row["tx_hash"], row["log_index"]]

    validation_fields = list(validated_rows[0].keys()) if validated_rows else []
    sample_fields = ["sample_reason"] + validation_fields
    write_csv(output_dir / "validated_outflows.csv", validated_rows, validation_fields)
    write_csv(output_dir / "qa_sampled_outflows.csv", sample_rows, sample_fields)
    write_csv(
        output_dir / "qa_overall_summary.csv",
        summarize(validated_rows),
        [
            "row_count",
            "pass_count",
            "review_count",
            "fail_count",
            "exact_transfer_log_found_count",
            "tx_sender_matches_transfer_from_count",
            "transfer_to_matches_creator_count",
            "total_amount",
            "review_amount",
            "fail_amount",
        ],
    )

    summary = summarize(validated_rows)[0]
    print("Rows validated:", summary["row_count"], flush=True)
    print("Pass:", summary["pass_count"], flush=True)
    print("Review:", summary["review_count"], flush=True)
    print("Fail:", summary["fail_count"], flush=True)
    print("Samples:", len(sample_rows), flush=True)
    print("Output directory:", output_dir, flush=True)


if __name__ == "__main__":
    main()
