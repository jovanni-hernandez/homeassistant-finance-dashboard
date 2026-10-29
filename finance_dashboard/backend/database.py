import logging
import json
import re
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from sqlalchemy import inspect, text
from models import (
    ACCOUNT_TYPE_UNCLASSIFIED,
    ACCOUNT_TYPE_SOURCE_AUTO,
    ACCOUNT_TYPE_SOURCE_MANUAL,
    ACCOUNT_TYPE_VALUES,
    engine,
    SessionLocal,
    Base,
    Account,
    ManualAsset,
    Transaction,
    Setting,
)

logger = logging.getLogger("finance_dashboard")


def init_db():
    logger.info("Initializing database tables via SQLAlchemy...")
    Base.metadata.create_all(bind=engine)
    _ensure_transaction_category_column()
    _ensure_account_type_columns()
    logger.info("Database initialized successfully.")


def _ensure_transaction_category_column():
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "category" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN category TEXT DEFAULT 'Uncategorized'"))
        logger.info("Added missing transactions.category column.")


def _ensure_account_type_columns():
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("accounts")}
    with engine.begin() as conn:
        if "account_type" not in columns:
            conn.execute(text(
                "ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'unclassified'"
            ))
            logger.info("Added accounts.account_type; existing accounts need classification.")
        if "account_type_suggestion" not in columns:
            conn.execute(text(
                "ALTER TABLE accounts ADD COLUMN account_type_suggestion TEXT NOT NULL DEFAULT 'unclassified'"
            ))
            logger.info("Added accounts.account_type_suggestion.")
        if "account_type_source" not in columns:
            conn.execute(text(
                "ALTER TABLE accounts ADD COLUMN account_type_source TEXT NOT NULL DEFAULT 'auto'"
            ))
            logger.info("Added accounts.account_type_source.")
        if "nickname" not in columns:
            conn.execute(text("ALTER TABLE accounts ADD COLUMN nickname TEXT NOT NULL DEFAULT ''"))
            logger.info("Added accounts.nickname.")


def save_simplefin_data(data):
    """Save normalized SimpleFIN accounts and transactions into SQLite via SQLAlchemy."""
    db = SessionLocal()
    try:
        accounts = data.get("accounts", [])
        for acc in accounts:
            acc_id = str(acc.get("id"))
            org_raw = acc.get("org", {})
            org = org_raw.get("name") if isinstance(org_raw, dict) else str(acc.get("org", ""))
            name = acc.get("name", "Account")
            if not org or org.strip().lower() == "unknown":
                org = name or "Account"
            balance = str(acc.get("balance", "0.00"))
            currency = acc.get("currency", "USD")
            suggestion = str(acc.get("account_type_suggestion") or ACCOUNT_TYPE_UNCLASSIFIED)
            if suggestion not in ACCOUNT_TYPE_VALUES:
                suggestion = ACCOUNT_TYPE_UNCLASSIFIED

            db_account = db.query(Account).filter(Account.id == acc_id).first()
            if not db_account:
                db_account = Account(
                    id=acc_id,
                    org=org,
                    name=name,
                    balance=balance,
                    currency=currency,
                    account_type=suggestion,
                    account_type_suggestion=suggestion,
                    account_type_source=ACCOUNT_TYPE_SOURCE_AUTO,
                )
                db.add(db_account)
            else:
                db_account.org = org
                db_account.name = name
                db_account.balance = balance
                db_account.currency = currency
                db_account.account_type_suggestion = suggestion
                if db_account.account_type_source != ACCOUNT_TYPE_SOURCE_MANUAL:
                    db_account.account_type = suggestion
                    db_account.account_type_source = ACCOUNT_TYPE_SOURCE_AUTO

            transactions = acc.get("transactions", [])
            for tx in transactions:
                tx_id = str(tx.get("id"))
                date = tx.get("date")
                amount = str(tx.get("amount", "0.00"))
                payee = tx.get("payee", "")
                description = tx.get("description", "")
                category = tx.get("category", "Uncategorized")
                pending = bool(tx.get("pending"))

                db_tx = db.query(Transaction).filter(Transaction.id == tx_id).first()
                if not db_tx:
                    db_tx = Transaction(
                        id=tx_id,
                        account_id=acc_id,
                        date=date,
                        amount=amount,
                        payee=payee,
                        description=description,
                        category=category,
                        pending=pending,
                    )
                    db.add(db_tx)
                else:
                    db_tx.account_id = acc_id
                    db_tx.date = date
                    db_tx.amount = amount
                    db_tx.payee = payee
                    db_tx.description = description
                    if not db_tx.category or db_tx.category == "Uncategorized":
                        db_tx.category = category
                    db_tx.pending = pending

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving data to DB: {e}")
        raise e
    finally:
        db.close()


def get_setting(key: str):
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        return setting.value if setting else None
    finally:
        db.close()


def set_setting(key: str, value: str):
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        if not setting:
            setting = Setting(key=key, value=value)
            db.add(setting)
        else:
            setting.value = value
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error setting value for {key}: {e}")
        raise e
    finally:
        db.close()


def get_stored_accounts():
    db = SessionLocal()
    try:
        accounts = db.query(Account).all()
        return [acc.to_dict() for acc in accounts]
    finally:
        db.close()


def set_account_type(account_id: str, account_type: str):
    normalized_type = str(account_type or "").strip()
    if normalized_type not in ACCOUNT_TYPE_VALUES:
        raise ValueError("Choose a supported account type.")

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.id == str(account_id)).first()
        if not account:
            return None
        account.account_type = normalized_type
        account.account_type_source = ACCOUNT_TYPE_SOURCE_MANUAL
        db.commit()
        return account.to_dict()
    except Exception as e:
        db.rollback()
        logger.error("Error setting account type (%s).", type(e).__name__)
        raise
    finally:
        db.close()


def set_account_nickname(account_id: str, nickname: str):
    normalized_nickname = str(nickname or "").strip()
    if len(normalized_nickname) > 80:
        raise ValueError("Account nickname must be 80 characters or fewer.")

    db = SessionLocal()
    try:
        account = db.query(Account).filter(Account.id == str(account_id)).first()
        if not account:
            return None
        account.nickname = normalized_nickname
        db.commit()
        return account.to_dict()
    except Exception as e:
        db.rollback()
        logger.error("Error setting account nickname (%s).", type(e).__name__)
        raise
    finally:
        db.close()


def _normalize_manual_asset(name: str, value: str, currency: str):
    normalized_name = str(name or "").strip()
    if not normalized_name or len(normalized_name) > 100:
        raise ValueError("Asset name must contain 1 to 100 characters.")

    try:
        normalized_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Asset value must be a valid number.") from exc
    if not normalized_value.is_finite() or normalized_value < 0:
        raise ValueError("Asset value must be a non-negative number.")
    if normalized_value != 0 and normalized_value.adjusted() > 30:
        raise ValueError("Asset value is too large.")
    if normalized_value.as_tuple().exponent < -18:
        raise ValueError("Asset value has too many decimal places.")
    value_string = format(normalized_value, "f")
    if len(value_string) > 40:
        raise ValueError("Asset value is too large.")

    normalized_currency = str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("Currency must be a three-letter code, such as USD.")

    return normalized_name, value_string, normalized_currency


def get_manual_assets():
    db = SessionLocal()
    try:
        return [asset.to_dict() for asset in db.query(ManualAsset).order_by(ManualAsset.created_at.asc()).all()]
    finally:
        db.close()


def create_manual_asset(name: str, value: str, currency: str = "USD"):
    normalized_name, normalized_value, normalized_currency = _normalize_manual_asset(name, value, currency)
    db = SessionLocal()
    try:
        asset = ManualAsset(
            id=str(uuid4()),
            name=normalized_name,
            value=normalized_value,
            currency=normalized_currency,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset.to_dict()
    except Exception as exc:
        db.rollback()
        logger.error("Error creating manual asset (%s).", type(exc).__name__)
        raise
    finally:
        db.close()


def update_manual_asset(asset_id: str, name: str, value: str, currency: str = "USD"):
    normalized_name, normalized_value, normalized_currency = _normalize_manual_asset(name, value, currency)
    db = SessionLocal()
    try:
        asset = db.query(ManualAsset).filter(ManualAsset.id == str(asset_id)).first()
        if not asset:
            return None
        asset.name = normalized_name
        asset.value = normalized_value
        asset.currency = normalized_currency
        db.commit()
        db.refresh(asset)
        return asset.to_dict()
    except Exception as exc:
        db.rollback()
        logger.error("Error updating manual asset (%s).", type(exc).__name__)
        raise
    finally:
        db.close()


def delete_manual_asset(asset_id: str):
    db = SessionLocal()
    try:
        asset = db.query(ManualAsset).filter(ManualAsset.id == str(asset_id)).first()
        if not asset:
            return False
        db.delete(asset)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error deleting manual asset (%s).", type(exc).__name__)
        raise
    finally:
        db.close()


def get_stored_transactions(limit=500, offset=0):
    db = SessionLocal()
    try:
        transactions = db.query(Transaction).order_by(Transaction.date.desc()).offset(offset).limit(limit).all()
        return [tx.to_dict() for tx in transactions]
    finally:
        db.close()


def get_account_count():
    db = SessionLocal()
    try:
        return db.query(Account).count()
    finally:
        db.close()


def get_transaction_count():
    db = SessionLocal()
    try:
        return db.query(Transaction).count()
    finally:
        db.close()


def set_transaction_category(
    transaction_id: str,
    category: str,
    apply_to_future_matches: bool = False,
    apply_to_existing_matches: bool = False,
    rule_match_source: str = "merchant",
    default_category_rules: list[dict] | None = None,
):
    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx:
            return False

        if apply_to_existing_matches and not apply_to_future_matches:
            raise ValueError("Updating existing matches requires saving a future-match rule too.")

        normalized_category = str(category or "Uncategorized").strip() or "Uncategorized"
        tx.category = normalized_category
        updated_existing_count = 0
        rule_patterns = []
        if apply_to_future_matches:
            merchant_pattern = str(tx.payee or "").strip()
            description_pattern = str(tx.description or "").strip()
            if rule_match_source == "merchant":
                rule_patterns = [merchant_pattern] if merchant_pattern else []
            elif rule_match_source == "description":
                rule_patterns = [description_pattern] if description_pattern else []
            elif rule_match_source == "both":
                rule_patterns = list(dict.fromkeys(pattern for pattern in (merchant_pattern, description_pattern) if pattern))
            else:
                raise ValueError("Choose merchant, description, or both for the category rule.")
            if not rule_patterns:
                raise ValueError("This transaction has no selected payee or description to match in future transactions.")
            if any(len(pattern) > 200 for pattern in rule_patterns):
                raise ValueError("The selected payee or description is too long to use as a category rule.")

            setting = db.query(Setting).filter(Setting.key == "category_rules_json").first()
            if setting and setting.value:
                try:
                    rules = json.loads(setting.value)
                except json.JSONDecodeError as exc:
                    raise ValueError("Saved category rules are invalid. Fix them in Settings before adding a rule.") from exc
                if not isinstance(rules, list) or len(rules) > 100:
                    raise ValueError("Saved category rules are invalid or have reached the rule limit.")
            else:
                rules = json.loads(json.dumps(default_category_rules or []))

            normalized_patterns = {pattern.casefold() for pattern in rule_patterns}
            preserved_rules = []
            for rule in rules:
                if not isinstance(rule, dict):
                    raise ValueError("Saved category rules are invalid. Fix them in Settings before adding a rule.")
                patterns = rule.get("patterns", [])
                if not isinstance(patterns, list):
                    raise ValueError("Saved category rules are invalid. Fix them in Settings before adding a rule.")
                remaining_patterns = [
                    existing for existing in patterns
                    if not isinstance(existing, str) or existing.strip().casefold() not in normalized_patterns
                ]
                if len(remaining_patterns) != len(patterns):
                    if not remaining_patterns:
                        continue
                    rule = {**rule, "patterns": remaining_patterns}
                preserved_rules.append(rule)

            if len(preserved_rules) >= 100:
                raise ValueError("The category rule limit has been reached. Remove a rule in Settings before adding another.")
            rules = [{"category": normalized_category, "patterns": rule_patterns}, *preserved_rules]
            rules_json = json.dumps(rules, ensure_ascii=False)
            if setting:
                setting.value = rules_json
            else:
                db.add(Setting(key="category_rules_json", value=rules_json))

            if apply_to_existing_matches:
                for existing_tx in db.query(Transaction).filter(Transaction.id != tx.id).all():
                    haystack = f"{existing_tx.payee or ''} {existing_tx.description or ''}".casefold()
                    if any(pattern.casefold() in haystack for pattern in rule_patterns) and (existing_tx.category or "Uncategorized") != normalized_category:
                        existing_tx.category = normalized_category
                        updated_existing_count += 1

        db.commit()
        return {"updated_existing_count": updated_existing_count}
    except Exception as e:
        db.rollback()
        logger.error("Error setting transaction category (%s).", type(e).__name__)
        raise e
    finally:
        db.close()


def get_category_rules_value():
    return get_setting("category_rules_json")


def set_category_rules_value(value: str):
    set_setting("category_rules_json", value)


def get_recurring_rules_value():
    return get_setting("recurring_rules_json")


def set_recurring_rules_value(value: str):
    set_setting("recurring_rules_json", value)
