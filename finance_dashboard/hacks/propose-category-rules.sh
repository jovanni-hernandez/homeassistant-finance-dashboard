#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${FINANCE_DB_PATH:-/data/finance.db}"
FORMAT="markdown"
TOP_N="${TOP_N:-25}"
MIN_COUNT="${MIN_COUNT:-2}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --db)
      DB_PATH="$2"
      shift 2
      ;;
    --format)
      FORMAT="$2"
      shift 2
      ;;
    --top)
      TOP_N="$2"
      shift 2
      ;;
    --min-count)
      MIN_COUNT="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: hacks/propose-category-rules.sh [--db /data/finance.db] [--format markdown|json] [--top N] [--min-count N]

Reads the encrypted finance database through the app models and proposes categorization rules
based on recurring merchant/payee patterns.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" \
FINANCE_DB_PATH="$DB_PATH" \
TOP_N="$TOP_N" \
MIN_COUNT="$MIN_COUNT" \
FORMAT="$FORMAT" \
python3 - <<'PY'
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import json
import os
import re
import sys

try:
    from models import SessionLocal, Transaction, Account  # type: ignore
except Exception as exc:
    raise SystemExit(
        'Unable to import the finance app models. Run this inside the add-on environment, '\
        'or make sure the backend dependencies are installed. ' + str(exc)
    )

format_name = os.environ.get('FORMAT', 'markdown').lower()
top_n = int(os.environ.get('TOP_N', '25'))
min_count = int(os.environ.get('MIN_COUNT', '2'))
db_path = Path(os.environ.get('FINANCE_DB_PATH', '/data/finance.db'))

if not db_path.exists():
    raise SystemExit(f'Database not found: {db_path}')

transfer_keywords = [
    'transfer', 'zelle', 'venmo', 'cash app', 'cashapp', 'payment to self',
    'ach xfer', 'ach transfer', 'external transfer', 'internal transfer',
]

category_keywords = [
    ('Income', ['payroll', 'salary', 'deposit', 'direct deposit', 'paycheck', 'ach credit']),
    ('Transfers', transfer_keywords),
    ('Groceries', ['grocery', 'supermarket', 'food lion', 'whole foods', 'trader joe', 'costco', 'aldi', 'kroger', 'walmart', 'sprouts']),
    ('Dining', ['restaurant', 'coffee', 'cafe', 'mcdonald', 'chipotle', 'starbucks', 'doordash', 'ubereats', 'grubhub', 'pizza']),
    ('Housing', ['rent', 'mortgage', 'property', 'hoa', 'landlord', 'apartment']),
    ('Utilities', ['electric', 'water', 'gas', 'internet', 'phone', 'utility', 'spectrum', 'verizon', 'xfinity', 'att']),
    ('Transport', ['gas', 'fuel', 'uber', 'lyft', 'parking', 'toll', 'metro', 'transit', 'shell', 'chevron', 'exxon']),
    ('Shopping', ['amazon', 'target', 'costco', 'shopping', 'store', 'walmart', 'best buy', 'ebay']),
    ('Travel', ['hotel', 'airlines', 'flight', 'travel', 'rental car', 'airbnb']),
    ('Subscriptions', ['netflix', 'spotify', 'subscription', 'prime', 'hulu', 'apple', 'youtube', 'google']),
    ('Fees', ['fee', 'interest', 'late', 'overdraft', 'atm', 'service charge']),
]

def normalize(text: str) -> str:
    text = re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())
    return re.sub(r'\s+', ' ', text).strip()

def detect_category(text: str, amount: float, existing: str | None) -> str:
    if existing and existing.strip() and existing != 'Uncategorized':
        return existing
    haystack = normalize(text)
    for category, words in category_keywords:
        if any(word in haystack for word in words):
            return category
    if amount > 0:
        return 'Income'
    return 'Uncategorized'

def merchant_key(payee: str, description: str) -> str:
    candidate = normalize(payee or '') or normalize(description or '')
    candidate = re.sub(r'\b\d{4,}\b', '', candidate)
    return re.sub(r'\s+', ' ', candidate).strip()

db = SessionLocal()
try:
    txs = (
        db.query(Transaction)
        .outerjoin(Account)
        .order_by(Transaction.date.desc())
        .all()
    )

    records = []
    for tx in txs:
        text = f"{tx.payee or ''} {tx.description or ''}".strip()
        amount = float(tx.amount or 0)
        category = tx.category or 'Uncategorized'
        suggested = detect_category(text, amount, category)
        records.append({
            'id': tx.id,
            'date': tx.date,
            'amount': amount,
            'payee': tx.payee or '',
            'description': tx.description or '',
            'account': getattr(tx.account, 'name', None) or getattr(tx.account, 'org', None) or tx.account_id or '',
            'current_category': category,
            'suggested_category': suggested,
            'merchant_key': merchant_key(tx.payee or '', tx.description or ''),
        })

    if not records:
        raise SystemExit('No transactions found.')

    totals = Counter()
    counts = Counter()
    examples = defaultdict(list)
    category_by_merchant = defaultdict(Counter)
    amount_by_merchant = Counter()

    for rec in records:
        key = rec['merchant_key']
        counts[key] += 1
        totals[key] += abs(rec['amount'])
        category_by_merchant[key][rec['suggested_category']] += 1
        amount_by_merchant[key] += abs(rec['amount'])
        if len(examples[key]) < 3:
            examples[key].append(rec)

    proposals = []
    for key, count in counts.most_common():
        if count < min_count:
            continue
        category = category_by_merchant[key].most_common(1)[0][0]
        if category == 'Uncategorized':
            continue
        merchant_examples = examples[key]
        payees = sorted({e['payee'] or e['description'] for e in merchant_examples if (e['payee'] or e['description'])})
        if not payees:
            continue
        pattern = re.escape(payees[0])
        if len(payees) > 1:
            pattern = ' | '.join(re.escape(p) for p in payees[:3])
        proposals.append({
            'category': category,
            'pattern': pattern,
            'count': count,
            'amount_total': round(amount_by_merchant[key], 2),
            'examples': payees[:3],
        })

    proposals.sort(key=lambda item: (item['count'], item['amount_total']), reverse=True)
    proposals = proposals[:top_n]

    if format_name == 'json':
        print(json.dumps({
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'database': str(db_path),
            'transaction_count': len(records),
            'proposals': proposals,
        }, indent=2))
        raise SystemExit(0)

    print(f'# Categorization proposals')
    print(f'- Database: {db_path}')
    print(f'- Transactions scanned: {len(records)}')
    print(f'- Proposals: {len(proposals)}')
    print()

    current = Counter(rec['current_category'] for rec in records)
    print('## Current category mix')
    for category, count in current.most_common():
        print(f'- {category}: {count}')
    print()

    print('## Proposed rules')
    for idx, proposal in enumerate(proposals, 1):
        examples_text = '; '.join(proposal['examples'])
        print(f"{idx}. **{proposal['category']}** — `{proposal['pattern']}`")
        print(f"   - {proposal['count']} transactions, about ${proposal['amount_total']:.2f}")
        if examples_text:
            print(f"   - Examples: {examples_text}")
    print()

    uncategorized = [rec for rec in records if rec['current_category'] == 'Uncategorized']
    if uncategorized:
        print('## Uncategorized candidates to review')
        for rec in uncategorized[:top_n]:
            label = rec['payee'] or rec['description'] or rec['merchant_key'] or rec['id']
            print(f"- {label} → suggested **{rec['suggested_category']}**")
finally:
    db.close()
PY
