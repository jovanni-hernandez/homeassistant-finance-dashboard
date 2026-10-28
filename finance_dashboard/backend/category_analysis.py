import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from models import SessionLocal, Transaction


DEFAULT_CATEGORY_RULES = [
    {"category": "Transfers", "patterns": ["transfer", "zelle", "venmo", "cash app", "cashapp", "payment to self", "ach xfer", "ach transfer", "external transfer", "internal transfer", "credit card payment"]},
    {"category": "Income", "patterns": ["payroll", "salary", "direct deposit", "paycheck", "ach credit"]},
    {"category": "Groceries", "patterns": ["grocery", "supermarket", "food lion", "whole foods", "trader joe", "costco", "aldi", "kroger", "walmart", "sprouts"]},
    {"category": "Pets", "patterns": ["aspca pet insurance", "banfield", "pet insurance", "veterinary", "vet clinic", "pet hospital", "petco", "petsmart", "chewy"]},
    {"category": "Childcare", "patterns": ["ivymount school", "childcare", "child care", "daycare", "day care", "aftercare", "babysitter", "preschool"]},
    {"category": "Dining", "patterns": ["restaurant", "coffee", "cafe", "mcdonald", "chipotle", "starbucks", "doordash", "uber eats", "ubereats", "grubhub", "pizza"]},
    {"category": "Housing", "patterns": ["rent", "mortgage", "property", "hoa", "landlord", "apartment", "rosegate"]},
    {"category": "Utilities", "patterns": ["electric", "water", "natural gas", "internet", "phone", "utility", "spectrum", "verizon", "xfinity", "att"]},
    {"category": "Transport", "patterns": ["gas station", "fuel", "uber", "lyft", "parking", "toll", "metro", "transit", "shell", "chevron", "exxon"]},
    {"category": "Shopping", "patterns": ["amazon", "target", "costco", "shopping", "store", "walmart", "best buy", "ebay"]},
    {"category": "Travel", "patterns": ["hotel", "airlines", "flight", "travel", "rental car", "car rental", "airbnb", "amtrak"]},
    {"category": "Subscriptions", "patterns": ["netflix", "spotify", "subscription", "prime", "hulu", "apple", "youtube", "google"]},
    {"category": "Fees", "patterns": ["fee", "interest", "late", "overdraft", "atm", "service charge"]},
]


def normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def merchant_key(payee: str, description: str) -> str:
    candidate = normalize(payee or "") or normalize(description or "")
    candidate = re.sub(r"\b\d{4,}\b", "", candidate)
    return re.sub(r"\s+", " ", candidate).strip()


def detect_category(text: str, amount: float, existing: str | None) -> str:
    if existing and existing.strip() and existing != "Uncategorized":
        return existing

    haystack = normalize(text)
    if "rosegate" in haystack:
        return "Housing"
    for category, words in [(item["category"], item["patterns"]) for item in DEFAULT_CATEGORY_RULES]:
        if any(word in haystack for word in words):
            return category

    if amount > 0:
        return "Income"
    return "Uncategorized"


def generate_category_analysis(top_n: int = 25, min_count: int = 2):
    db = SessionLocal()
    try:
        txs = db.query(Transaction).order_by(Transaction.date.desc()).all()

        records = []
        for tx in txs:
            text = f"{tx.payee or ''} {tx.description or ''}".strip()
            amount = float(tx.amount or 0)
            current_category = tx.category or "Uncategorized"
            suggested_category = detect_category(text, amount, current_category)
            records.append(
                {
                    "id": tx.id,
                    "date": tx.date,
                    "amount": amount,
                    "payee": tx.payee or "",
                    "description": tx.description or "",
                    "current_category": current_category,
                    "suggested_category": suggested_category,
                    "merchant_key": merchant_key(tx.payee or "", tx.description or ""),
                }
            )

        if not records:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "transaction_count": 0,
                "current_category_mix": [],
                "proposals": [],
                "uncategorized_candidates": [],
                "rules_json": "[]",
            }

        counts = Counter()
        amount_by_merchant = Counter()
        category_by_merchant = defaultdict(Counter)
        examples_by_merchant = defaultdict(list)

        for rec in records:
            key = rec["merchant_key"]
            counts[key] += 1
            amount_by_merchant[key] += abs(rec["amount"])
            category_by_merchant[key][rec["suggested_category"]] += 1
            if len(examples_by_merchant[key]) < 3:
                examples_by_merchant[key].append(rec)

        proposals = []
        category_patterns = defaultdict(list)
        for key, count in counts.most_common():
            if count < min_count:
                continue

            category = category_by_merchant[key].most_common(1)[0][0]
            if category == "Uncategorized":
                continue

            examples = examples_by_merchant[key]
            merchant_examples = sorted({e["payee"] or e["description"] for e in examples if (e["payee"] or e["description"])} )
            if not merchant_examples:
                continue

            patterns = merchant_examples[:3]
            proposals.append(
                {
                    "category": category,
                    "pattern": patterns[0],
                    "count": count,
                    "amount_total": round(amount_by_merchant[key], 2),
                    "examples": patterns,
                }
            )

            for pattern in patterns:
                if pattern not in category_patterns[category]:
                    category_patterns[category].append(pattern)

        proposals.sort(key=lambda item: (item["count"], item["amount_total"]), reverse=True)
        proposals = proposals[:top_n]

        current_mix = Counter(rec["current_category"] for rec in records)
        uncategorized_candidates = [
            {
                "label": rec["payee"] or rec["description"] or rec["merchant_key"] or rec["id"],
                "suggested_category": rec["suggested_category"],
            }
            for rec in records
            if rec["current_category"] == "Uncategorized"
        ][:top_n]

        rules = []
        for category, patterns in sorted(category_patterns.items()):
            unique_patterns = []
            for pattern in patterns:
                if pattern not in unique_patterns:
                    unique_patterns.append(pattern)
            rules.append({"category": category, "patterns": unique_patterns[:12]})

        rules_json = json.dumps(rules, indent=2)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "transaction_count": len(records),
            "current_category_mix": [
                {"category": category, "count": count} for category, count in current_mix.most_common()
            ],
            "proposals": proposals,
            "uncategorized_candidates": uncategorized_candidates,
            "rules_json": rules_json,
        }
    finally:
        db.close()
