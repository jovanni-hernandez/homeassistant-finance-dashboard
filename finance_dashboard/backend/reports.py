"""Year-end account balance reports and downloadable formats."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func

from models import Account, AccountBalanceSnapshot, AccountDataCoverage, SessionLocal, Transaction


ACCOUNT_TYPE_LABELS = {
    "unclassified": "Unclassified",
    "bank_account": "Bank / cash",
    "investment": "Investment",
    "credit_card": "Credit card debt",
    "loan": "Loan",
    "other_asset": "Other asset",
    "other_liability": "Other liability",
}


def _year_boundary(year: int, timezone_name: str) -> tuple[datetime, ZoneInfo]:
    if isinstance(year, bool) or not isinstance(year, int) or year < 1900 or year > 9998:
        raise ValueError("Choose a calendar year between 1900 and 9998.")
    try:
        zone = ZoneInfo(str(timezone_name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Choose a valid IANA time zone.") from exc
    if year > datetime.now(zone).year:
        raise ValueError("Choose the current or a completed calendar year.")
    return datetime(year + 1, 1, 1, tzinfo=zone), zone


def _decimal(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("A balance or transaction amount is not a valid number.") from exc
    if not amount.is_finite():
        raise ValueError("A balance or transaction amount is not a finite number.")
    return amount


def _covered(intervals, start: int, end: int) -> bool:
    """Return true only when complete provider ranges cover the whole interval."""
    if end <= start:
        return True
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return False


def _account_label(account: Account) -> str:
    return account.nickname or account.name or "Account"


def _unavailable(account: Account, status: str, note: str) -> dict:
    account_type = account.account_type or "unclassified"
    return {
        "provider": account.org or "Unknown",
        "account_name": _account_label(account),
        "account_type": account_type,
        "account_type_label": ACCOUNT_TYPE_LABELS.get(account_type, "Unclassified"),
        "currency": (account.currency or "USD").upper(),
        "balance": None,
        "status": status,
        "status_note": note,
        "balance_as_of": None,
        "history_available": False,
    }


def build_year_end_report(year: int, timezone_name: str = "UTC") -> dict:
    """Build a verified historical balance or current-year snapshot report."""
    boundary, zone = _year_boundary(year, timezone_name)
    generated_at = datetime.now(zone)
    current_year = year == generated_at.year
    boundary_ts = int(boundary.timestamp())
    generated_ts = int(generated_at.timestamp())
    year_start_ts = int(datetime(year, 1, 1, tzinfo=zone).timestamp())
    accounts_out = []
    db = SessionLocal()
    try:
        accounts = db.query(Account).order_by(Account._org, Account._name, Account.id).all()
        snapshots = db.query(AccountBalanceSnapshot).order_by(AccountBalanceSnapshot.captured_at).all()
        coverage_rows = db.query(AccountDataCoverage).all()
        snapshots_by_account = {}
        coverage_by_account = {}
        complete_coverage_by_account = {}
        for snapshot in snapshots:
            snapshots_by_account.setdefault(str(snapshot.account_id), []).append(snapshot)
        for row in coverage_rows:
            coverage_by_account.setdefault(str(row.account_id), []).append((int(row.start_ts), int(row.end_ts)))
            if row.status == "complete":
                complete_coverage_by_account.setdefault(str(row.account_id), []).append((int(row.start_ts), int(row.end_ts)))

        for account in accounts:
            account_id = str(account.id)
            account_snapshots = snapshots_by_account.get(account_id, [])
            account_type = account.account_type or "unclassified"
            account_fields = {
                "provider": account.org or "Unknown",
                "account_name": _account_label(account),
                "account_type": account_type,
                "account_type_label": ACCOUNT_TYPE_LABELS.get(account_type, "Unclassified"),
            }
            if not account_snapshots:
                unavailable = _unavailable(
                    account,
                    "unavailable",
                    "No dated balance snapshot is available yet. A successful account sync is needed before this balance can be reconstructed.",
                )
                unavailable["history_available"] = False
                accounts_out.append(unavailable)
                continue

            if current_year:
                current_snapshots = [
                    row for row in account_snapshots
                    if year_start_ts <= row.captured_at.replace(tzinfo=timezone.utc).timestamp() <= generated_ts
                ]
                if not current_snapshots:
                    unavailable = _unavailable(
                        account,
                        "unavailable",
                        "No balance snapshot from this calendar year is available for this account.",
                    )
                    accounts_out.append(unavailable)
                    continue
                snapshot = max(current_snapshots, key=lambda row: row.captured_at)
                snapshot_as_of = snapshot.captured_at.replace(tzinfo=timezone.utc).astimezone(zone).isoformat()
                try:
                    balance = _decimal(snapshot.balance)
                except (ValueError, TypeError, InvalidOperation):
                    row = _unavailable(
                        account,
                        "unverified",
                        "The current-year balance snapshot could not be validated, so no value is shown.",
                    )
                    row.update(account_fields)
                    row["history_available"] = True
                    row["balance_as_of"] = snapshot_as_of
                    accounts_out.append(row)
                    continue
                accounts_out.append({
                    **account_fields,
                    "currency": (snapshot.currency or account.currency or "USD").upper(),
                    "balance": format(balance, "f"),
                    "status": "verified",
                    "status_note": "Latest saved SimpleFIN balance snapshot in this calendar year; this is not a projection to December 31.",
                    "balance_as_of": snapshot_as_of,
                    "history_available": True,
                })
                continue

            intervals = complete_coverage_by_account.get(account_id, [])
            candidates = sorted(
                account_snapshots,
                key=lambda row: abs(row.captured_at.replace(tzinfo=timezone.utc).timestamp() - boundary_ts),
            )
            snapshot = None
            anchor_ts = None
            for candidate in candidates:
                candidate_ts = int(candidate.captured_at.replace(tzinfo=timezone.utc).timestamp())
                lower, upper = sorted((candidate_ts, boundary_ts))
                if _covered(intervals, lower, upper):
                    snapshot = candidate
                    anchor_ts = candidate_ts
                    break
            if snapshot is None:
                snapshot = candidates[0]
                anchor_ts = int(snapshot.captured_at.replace(tzinfo=timezone.utc).timestamp())
                lower, upper = sorted((anchor_ts, boundary_ts))
                snapshot_year = snapshot.captured_at.replace(tzinfo=timezone.utc).astimezone(zone).year
                has_saved_history = snapshot_year == year or any(
                    start < upper and end > lower
                    for start, end in coverage_by_account.get(account_id, [])
                )
                if not has_saved_history and upper > lower:
                    has_saved_history = db.query(Transaction.id).filter(
                        Transaction.account_id == account_id,
                        Transaction.date >= lower,
                        Transaction.date <= upper,
                    ).first() is not None
                unavailable = _unavailable(
                    account,
                    "unverified" if has_saved_history else "unavailable",
                    (
                        "Saved history exists, but verified transaction coverage does not span the full period between the balance snapshot and December 31."
                        if has_saved_history
                        else "No saved transaction or coverage history connects this balance snapshot to December 31."
                    ),
                )
                unavailable.update(account_fields)
                unavailable["history_available"] = has_saved_history
                accounts_out.append(unavailable)
                continue

            try:
                balance = _decimal(snapshot.balance)
                if anchor_ts != boundary_ts:
                    transaction_query = db.query(Transaction).filter(
                        Transaction.account_id == account_id,
                        Transaction.pending.is_(False),
                        Transaction.date >= lower,
                        Transaction.date <= upper,
                    )
                    transactions = transaction_query.all()
                    movement = Decimal("0")
                    for transaction in transactions:
                        transaction_ts = int(transaction.date or 0)
                        if anchor_ts < boundary_ts:
                            # The snapshot is before year-end; the Dec. 31 balance includes
                            # posted transactions after the snapshot and before Jan. 1.
                            if anchor_ts < transaction_ts < boundary_ts:
                                movement += _decimal(transaction.amount)
                        else:
                            # The snapshot is after year-end; reverse transactions dated
                            # on/after Jan. 1 through the snapshot.
                            if boundary_ts <= transaction_ts <= anchor_ts:
                                movement += _decimal(transaction.amount)
                    balance = balance + movement if anchor_ts < boundary_ts else balance - movement
            except (ValueError, TypeError, InvalidOperation):
                accounts_out.append(_unavailable(
                    account,
                    "unverified",
                    "A stored balance or transaction amount could not be validated, so no year-end value is shown.",
                ))
                continue

            accounts_out.append({
                **account_fields,
                "currency": (snapshot.currency or account.currency or "USD").upper(),
                "balance": format(balance, "f"),
                "status": "verified",
                "status_note": "Calculated from a saved account balance and provider-verified posted transactions.",
                "balance_as_of": datetime.fromtimestamp(boundary_ts - 1, zone).date().isoformat(),
                "history_available": True,
            })
    finally:
        db.close()

    accounts_out.sort(key=lambda row: (
        _provider_key(row.get("provider")),
        str(row.get("account_name", "")).casefold(),
        str(row.get("currency", "USD")),
    ))
    if current_year:
        as_of = generated_at.date().isoformat()
        title = "Current account balances"
        methodology = (
            "Current-year balances use each account's latest saved SimpleFIN balance snapshot from this calendar year. "
            "Snapshot dates are shown per account; values are not projected to December 31. Currencies are not converted."
        )
        report_note = "This current-year view is derived from connected-account data, not a bank statement or certified filing."
    else:
        as_of = f"December 31, {year}"
        title = "Year-end account balances"
        methodology = (
            "Year-end balances are calculated from the nearest saved SimpleFIN balance snapshot and posted transactions. "
            "A value is marked verified only when successful provider coverage spans the full adjustment period. "
            "Pending transactions are excluded. Balances retain their provider-reported sign; currencies are not converted."
        )
        report_note = "This report is derived from connected-account data and is not a bank statement or certified filing."
    return {
        "year": year,
        "current_year": current_year,
        "title": title,
        "timezone": zone.key,
        "as_of": as_of,
        "accounts": accounts_out,
        "methodology": methodology,
        "report_note": report_note,
    }


def get_available_year_end_report_years(timezone_name: str = "UTC") -> dict:
    """List years with a saved balance snapshot and relevant linked account history."""
    try:
        zone = ZoneInfo(str(timezone_name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Choose a valid IANA time zone.") from exc
    current_year = datetime.now(zone).year
    db = SessionLocal()
    try:
        bounds = [
            db.query(func.min(AccountBalanceSnapshot.captured_at), func.max(AccountBalanceSnapshot.captured_at)).one(),
            db.query(func.min(AccountDataCoverage.start_ts), func.max(AccountDataCoverage.end_ts)).one(),
            db.query(func.min(Transaction.date), func.max(Transaction.date)).one(),
        ]
    finally:
        db.close()

    timestamps = []
    for start, end in bounds:
        for value in (start, end):
            if value is None:
                continue
            if isinstance(value, datetime):
                timestamps.append(value.replace(tzinfo=timezone.utc).timestamp())
            else:
                timestamps.append(float(value))
    if not timestamps:
        return {"years": [], "timezone": zone.key}

    first_year = max(1900, datetime.fromtimestamp(min(timestamps), zone).year - 1)
    available = []
    for candidate_year in range(current_year, first_year - 1, -1):
        report = build_year_end_report(candidate_year, zone.key)
        if any(account.get("history_available") for account in report["accounts"]):
            available.append(candidate_year)
    return {"years": available, "timezone": zone.key}


def _provider_key(value: str) -> str:
    return " ".join(str(value or "Unknown").split()).casefold()


def group_year_end_accounts(accounts: list[dict], group_accounts: bool) -> list[dict]:
    if not group_accounts:
        return [{**account, "member_accounts": [account]} for account in accounts]

    groups = {}
    result = []
    for account in accounts:
        account_type = account.get("account_type") or "unclassified"
        # Unknown classifications do not establish that two accounts are alike.
        if account_type == "unclassified":
            result.append({**account, "member_accounts": [account]})
            continue
        key = (
            _provider_key(account.get("provider")),
            account_type,
            str(account.get("currency") or "USD").upper(),
        )
        groups.setdefault(key, []).append(account)

    for members in groups.values():
        if len(members) == 1:
            result.append({**members[0], "member_accounts": members})
            continue
        verified = all(member.get("status") == "verified" for member in members)
        total = sum((_decimal(member["balance"]) for member in members), Decimal("0")) if verified else None
        first = members[0]
        as_of_values = {member.get("balance_as_of") for member in members}
        result.append({
            "provider": first.get("provider", "Unknown"),
            "account_name": f"{first.get('account_type_label', 'Account')} accounts ({len(members)})",
            "account_type": first.get("account_type", "unclassified"),
            "account_type_label": first.get("account_type_label", "Unclassified"),
            "currency": first.get("currency", "USD"),
            "balance": format(total, "f") if total is not None else None,
            "status": "verified" if verified else "unverified",
            "status_note": (
                "Grouped total of the listed accounts. Snapshot times may differ; see account detail."
                if verified and len(as_of_values) > 1
                else "Grouped total of the listed accounts."
                if verified
                else "Grouped total is not shown because one or more component accounts are not verified."
            ),
            "balance_as_of": next(iter(as_of_values)) if len(as_of_values) == 1 else "Multiple snapshot times",
            "history_available": any(member.get("history_available") for member in members),
            "member_accounts": members,
        })
    return sorted(result, key=lambda row: (
        _provider_key(row.get("provider")),
        str(row.get("account_type_label", "" )).casefold(),
        str(row.get("account_name", "")).casefold(),
        str(row.get("currency", "USD")),
    ))


def prepare_year_end_report(year: int, timezone_name: str = "UTC", group_accounts: bool = False) -> dict:
    report = build_year_end_report(year, timezone_name)
    report["group_accounts"] = bool(group_accounts)
    report["rows"] = group_year_end_accounts(report["accounts"], bool(group_accounts))
    return report


def _safe_excel_text(value) -> str:
    # Explicit string cell types prevent account names from becoming formulas.
    return str(value or "")


def create_excel_report(report: dict) -> bytes:
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Year-end summary"
    details = workbook.create_sheet("Account details")
    header_fill = PatternFill("solid", fgColor="0F4C81")
    header_font = Font(color="FFFFFF", bold=True)

    summary.append([f"{report['title']} — {report['year']}"])
    summary.append([f"As of {report['as_of']} · {report['timezone']}"])
    summary.append([report["report_note"]])
    summary.append([])
    summary.append(["Provider", "Account / group", "Account type", "Currency", "Balance", "Balance as of", "Status", "Included accounts", "Coverage / source note"])
    for row in report["rows"]:
        members = row.get("member_accounts", [])
        member_names = "; ".join(member.get("account_name", "Account") for member in members)
        values = [
            _safe_excel_text(row.get("provider")),
            _safe_excel_text(row.get("account_name")),
            _safe_excel_text(row.get("account_type_label")),
            _safe_excel_text(row.get("currency")),
            _decimal(row["balance"]) if row.get("balance") is not None else None,
            _safe_excel_text(row.get("balance_as_of")),
            _safe_excel_text(row.get("status")),
            _safe_excel_text(member_names),
            _safe_excel_text(row.get("status_note")),
        ]
        summary.append(values)
        for column in range(1, len(values) + 1):
            summary.cell(summary.max_row, column).data_type = "n" if column == 5 and values[4] is not None else "s"
    summary.append([])
    summary.append([report["methodology"]])
    summary.freeze_panes = "A6"

    details.append([f"{report['title']} details — {report['year']}"])
    details.append([f"As of {report['as_of']} · {report['timezone']}"])
    details.append([])
    details.append(["Provider", "Account", "Account type", "Currency", "Balance", "Balance as of", "Status", "Coverage / source note"])
    for account in report["accounts"]:
        values = [
            _safe_excel_text(account.get("provider")),
            _safe_excel_text(account.get("account_name")),
            _safe_excel_text(account.get("account_type_label")),
            _safe_excel_text(account.get("currency")),
            _decimal(account["balance"]) if account.get("balance") is not None else None,
            _safe_excel_text(account.get("balance_as_of")),
            _safe_excel_text(account.get("status")),
            _safe_excel_text(account.get("status_note")),
        ]
        details.append(values)
        for column in range(1, len(values) + 1):
            details.cell(details.max_row, column).data_type = "n" if column == 5 and values[4] is not None else "s"
    details.append([])
    details.append([report["methodology"]])
    details.freeze_panes = "A4"

    for sheet, header_row, widths in (
        (summary, 5, [24, 32, 22, 12, 18, 28, 16, 42, 65]),
        (details, 4, [24, 32, 22, 12, 18, 28, 16, 72]),
    ):
        for cell in sheet[header_row]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        for row in sheet.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=cell.column in (2, 8, 9))
                if cell.column == 5 and isinstance(cell.value, (int, float, Decimal)):
                    cell.number_format = '#,##0.00;[Red](#,##0.00);-'
    summary.auto_filter.ref = f"A5:I{max(5, 5 + len(report['rows']))}"
    details.auto_filter.ref = f"A4:H{max(4, 4 + len(report['accounts']))}"

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _pdf_text(value) -> str:
    text = str(value or "")
    # Helvetica is a standard PDF font; replace unsupported glyphs rather than failing export.
    text = text.encode("cp1252", "replace").decode("cp1252")
    return escape(text).replace("\n", "<br/>")


def create_pdf_report(report: dict) -> bytes:
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(letter),
        rightMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title=f"{report['title']} {report['year']}",
        author="Personal Finance Dashboard",
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("ReportSmall", parent=styles["BodyText"], fontSize=7.5, leading=9)
    title = ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=19, leading=23, spaceAfter=5)
    right = ParagraphStyle("ReportRight", parent=small, alignment=TA_RIGHT)
    story = [
        Paragraph(f"{_pdf_text(report['title'])} — {report['year']}", title),
        Paragraph(f"As of {_pdf_text(report['as_of'])} · {_pdf_text(report['timezone'])}", styles["BodyText"]),
        Spacer(1, 8),
        Paragraph(_pdf_text(report["report_note"]), small),
        Spacer(1, 10),
    ]
    table_data = [["Provider", "Account / group", "Account type", "Currency", "Balance", "Balance as of", "Status", "Included accounts", "Coverage / source note"]]
    for row in report["rows"]:
        names = "; ".join(member.get("account_name", "Account") for member in row.get("member_accounts", []))
        table_data.append([
            Paragraph(_pdf_text(row.get("provider")), small),
            Paragraph(_pdf_text(row.get("account_name")), small),
            Paragraph(_pdf_text(row.get("account_type_label")), small),
            Paragraph(_pdf_text(row.get("currency")), small),
            Paragraph(_pdf_text(row.get("balance") if row.get("balance") is not None else "Not available"), right),
            Paragraph(_pdf_text(row.get("balance_as_of") or "—"), small),
            Paragraph(_pdf_text(row.get("status", "unavailable")).title(), small),
            Paragraph(_pdf_text(names), small),
            Paragraph(_pdf_text(row.get("status_note")), small),
        ])
    table = Table(table_data, colWidths=[0.95 * inch, 1.25 * inch, 0.75 * inch, 0.5 * inch, 0.7 * inch, 1.05 * inch, 0.6 * inch, 1.75 * inch, 2.3 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f4c81")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    if report["group_accounts"]:
        story.extend([Spacer(1, 12), Paragraph("Individual account detail", styles["Heading2"])])
        detail_data = [["Provider", "Account", "Type", "Currency", "Balance", "Balance as of", "Status", "Coverage / source note"]]
        for account in report["accounts"]:
            detail_data.append([
                Paragraph(_pdf_text(account.get("provider")), small),
                Paragraph(_pdf_text(account.get("account_name")), small),
                Paragraph(_pdf_text(account.get("account_type_label")), small),
                Paragraph(_pdf_text(account.get("currency")), small),
                Paragraph(_pdf_text(account.get("balance") if account.get("balance") is not None else "Not available"), right),
                Paragraph(_pdf_text(account.get("balance_as_of") or "—"), small),
                Paragraph(_pdf_text(account.get("status", "unavailable")).title(), small),
                Paragraph(_pdf_text(account.get("status_note")), small),
            ])
        detail_table = Table(detail_data, colWidths=[0.95 * inch, 1.2 * inch, 0.7 * inch, 0.45 * inch, 0.7 * inch, 1.05 * inch, 0.6 * inch, 3.3 * inch], repeatRows=1)
        detail_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ]))
        story.extend([detail_table, Spacer(1, 10)])
    story.extend([Spacer(1, 10), Paragraph(_pdf_text(report["methodology"]), small)])
    document.build(story)
    return output.getvalue()
