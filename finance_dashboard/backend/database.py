import logging
import json
from sqlalchemy import inspect, text
from models import (
    ACCOUNT_TYPE_UNCLASSIFIED,
    ACCOUNT_TYPE_VALUES,
    engine,
    SessionLocal,
    Base,
    Account,
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
                    account_type=ACCOUNT_TYPE_UNCLASSIFIED,
                    account_type_suggestion=suggestion,
                )
                db.add(db_account)
            else:
                db_account.org = org
                db_account.name = name
                db_account.balance = balance
                db_account.currency = currency
                db_account.account_type_suggestion = suggestion

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
        db.commit()
        return account.to_dict()
    except Exception as e:
        db.rollback()
        logger.error("Error setting account type (%s).", type(e).__name__)
        raise
    finally:
        db.close()


def get_stored_transactions(limit=100):
    db = SessionLocal()
    try:
        transactions = db.query(Transaction).order_by(Transaction.date.desc()).limit(limit).all()
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


def set_transaction_category(transaction_id: str, category: str):
    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx:
            return False
        tx.category = category or "Uncategorized"
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Error setting category for transaction {transaction_id}: {e}")
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
