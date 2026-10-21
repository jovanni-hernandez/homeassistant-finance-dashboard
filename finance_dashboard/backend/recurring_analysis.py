from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from models import SessionLocal, Transaction


SUBSCRIPTION_KEYWORDS = [
    'netflix', 'spotify', 'subscription', 'prime', 'hulu', 'apple', 'youtube', 'google',
    'adobe', 'microsoft', 'icloud', 'one drive', 'onedrive', 'patreon', 'walmart+', 'instacart',
]

RECURRING_BILL_KEYWORDS = [
    'rent', 'mortgage', 'hoa', 'electric', 'water', 'gas', 'internet', 'phone', 'insurance',
    'loan', 'credit card', 'membership', 'gym', 'tuition', 'utility', 'service charge',
]

RECURRING_INCOME_KEYWORDS = [
    'payroll', 'salary', 'paycheck', 'direct deposit', 'deposit', 'ach credit', 'stipend', 'benefit',
]

TRANSFER_KEYWORDS = [
    'transfer', 'zelle', 'venmo', 'cash app', 'cashapp', 'payment to self', 'ach xfer',
    'ach transfer', 'external transfer', 'internal transfer',
]


def normalize(text: str) -> str:
    return ' '.join(''.join(ch.lower() if ch.isalnum() else ' ' for ch in (text or '')).split())


def merchant_key(payee: str, description: str) -> str:
    candidate = normalize(payee or '') or normalize(description or '')
    parts = [part for part in candidate.split() if not part.isdigit() and len(part) < 8]
    return ' '.join(parts).strip()


def _days_between(a: int, b: int) -> float:
    return abs(float(b) - float(a)) / 86400.0


def _cadence_label(days: float | None) -> str:
    if days is None:
        return 'unknown'
    if 5 <= days <= 9:
        return 'weekly'
    if 12 <= days <= 18:
        return 'biweekly'
    if 25 <= days <= 35:
        return 'monthly'
    if 40 <= days <= 60:
        return 'every 6–8 weeks'
    if 75 <= days <= 110:
        return 'quarterly'
    if 320 <= days <= 400:
        return 'annual'
    return f'every ~{round(days)} days'


def _inferred_kind(text: str, amount: float) -> str:
    if any(keyword in text for keyword in SUBSCRIPTION_KEYWORDS):
        return 'subscription'
    if amount > 0 and any(keyword in text for keyword in RECURRING_INCOME_KEYWORDS):
        return 'recurring income'
    if any(keyword in text for keyword in RECURRING_BILL_KEYWORDS):
        return 'recurring bill'
    return 'recurring payment'


def _is_transfer_like(text: str, category: str | None) -> bool:
    if category and category.strip().lower() == 'transfers':
        return True
    return any(keyword in text for keyword in TRANSFER_KEYWORDS)


def _confidence(count: int, cadence_days: float | None, amount_stability: float, keyword_hit: bool, kind: str) -> int:
    score = 20 + min(count, 8) * 8
    if cadence_days is not None:
        if kind == 'subscription' and 25 <= cadence_days <= 35:
            score += 20
        elif kind == 'recurring income' and cadence_days >= 10:
            score += 12
        elif kind == 'recurring bill' and cadence_days >= 20:
            score += 14
        else:
            score += 8
    score += max(0, 20 - int(amount_stability * 40))
    if keyword_hit:
        score += 10
    return max(0, min(100, score))


def generate_recurring_analysis(top_n: int = 40, min_count: int = 2):
    db = SessionLocal()
    try:
        txs = db.query(Transaction).order_by(Transaction.date.asc()).all()
        grouped = defaultdict(list)

        for tx in txs:
            amount = float(tx.amount or 0)
            if amount == 0 or not tx.date:
                continue

            text = normalize(f"{tx.payee or ''} {tx.description or ''}")
            if not text or _is_transfer_like(text, tx.category):
                continue

            key = merchant_key(tx.payee or '', tx.description or '')
            if not key:
                continue

            grouped[key].append(
                {
                    'id': tx.id,
                    'date': int(tx.date),
                    'amount': amount,
                    'abs_amount': abs(amount),
                    'payee': tx.payee or '',
                    'description': tx.description or '',
                    'category': tx.category or 'Uncategorized',
                    'account_name': tx.account.name if tx.account and tx.account.name else (tx.account_id or 'Account'),
                    'account_id': tx.account_id,
                    'text': text,
                }
            )

        items = []
        for key, entries in grouped.items():
            entries.sort(key=lambda item: item['date'])
            count = len(entries)
            if count < min_count:
                continue

            intervals = [_days_between(entries[idx]['date'], entries[idx + 1]['date']) for idx in range(count - 1)]
            span_days = _days_between(entries[0]['date'], entries[-1]['date']) if count > 1 else 0.0
            cadence_days = median(intervals) if intervals else None
            interval_jitter = max((abs(interval - cadence_days) for interval in intervals), default=0.0) if cadence_days is not None else 0.0

            amounts = [entry['abs_amount'] for entry in entries]
            median_amount = median(amounts)
            if median_amount <= 0:
                continue
            amount_stability = max(abs(amount - median_amount) for amount in amounts) / median_amount

            kind = _inferred_kind(' '.join(entry['text'] for entry in entries), entries[-1]['amount'])
            keyword_hit = kind != 'recurring payment'

            recurring = False
            if cadence_days is not None:
                recurring = interval_jitter <= max(3.0, cadence_days * 0.35)
                if cadence_days >= 20 and count >= 2:
                    recurring = recurring or span_days >= cadence_days * 1.5
            if not recurring and count >= 3 and span_days >= 45:
                recurring = True

            if not recurring:
                continue

            confidence = _confidence(count, cadence_days, amount_stability, keyword_hit, kind)
            first_seen = datetime.fromtimestamp(entries[0]['date'], tz=timezone.utc).isoformat()
            last_seen = datetime.fromtimestamp(entries[-1]['date'], tz=timezone.utc).isoformat()
            next_expected = None
            if cadence_days is not None:
                next_expected = datetime.fromtimestamp(entries[-1]['date'] + int(round(cadence_days * 86400)), tz=timezone.utc).isoformat()

            items.append(
                {
                    'merchant_key': key,
                    'label': entries[-1]['payee'] or entries[-1]['description'] or key,
                    'kind': kind,
                    'cadence_days': round(cadence_days, 1) if cadence_days is not None else None,
                    'cadence_label': _cadence_label(cadence_days),
                    'count': count,
                    'span_days': round(span_days, 1),
                    'first_seen': first_seen,
                    'last_seen': last_seen,
                    'next_expected': next_expected,
                    'average_amount': round(median_amount, 2),
                    'min_amount': round(min(amounts), 2),
                    'max_amount': round(max(amounts), 2),
                    'amount_stability': round(amount_stability, 3),
                    'confidence': confidence,
                    'examples': [
                        {
                            'date': datetime.fromtimestamp(entry['date'], tz=timezone.utc).isoformat(),
                            'amount': round(entry['amount'], 2),
                            'payee': entry['payee'],
                            'description': entry['description'],
                            'account_name': entry['account_name'],
                        }
                        for entry in entries[-3:]
                    ],
                    'account_names': sorted({entry['account_name'] for entry in entries}),
                }
            )

        items.sort(key=lambda item: (item['confidence'], item['count'], item['average_amount']), reverse=True)
        items = items[:top_n]

        summary = {
            'subscription_count': sum(1 for item in items if item['kind'] == 'subscription'),
            'recurring_bill_count': sum(1 for item in items if item['kind'] == 'recurring bill'),
            'recurring_income_count': sum(1 for item in items if item['kind'] == 'recurring income'),
            'other_count': sum(1 for item in items if item['kind'] == 'recurring payment'),
        }

        return {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'transaction_count': len(txs),
            'recurring_count': len(items),
            'summary': summary,
            'items': items,
        }
    finally:
        db.close()
