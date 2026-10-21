import logging
from models import engine, SessionLocal, Base, Account, Transaction

logger = logging.getLogger("finance_dashboard")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    logger.info("Initializing database tables via SQLAlchemy...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized successfully.")

def save_simplefin_data(data):
    """Save normalized SimpleFIN accounts and transactions into SQLite via SQLAlchemy."""
    db = SessionLocal()
    try:
        accounts = data.get("accounts", [])
        for acc in accounts:
            acc_id = str(acc.get("id"))
            org_raw = acc.get("org", {})
            org = org_raw.get("name") if isinstance(org_raw, dict) else str(acc.get("org", "Unknown"))
            name = acc.get("name", "Account")
            balance = str(acc.get("balance", "0.00"))
            currency = acc.get("currency", "USD")

            db_account = db.query(Account).filter(Account.id == acc_id).first()
            if not db_account:
                db_account = Account(id=acc_id, org=org, name=name, balance=balance, currency=currency)
                db.add(db_account)
            else:
                db_account.org = org
                db_account.name = name
                db_account.balance = balance
                db_account.currency = currency

            transactions = acc.get("transactions", [])
            for tx in transactions:
                tx_id = str(tx.get("id"))
                date = tx.get("date")
                amount = str(tx.get("amount", "0.00"))
                payee = tx.get("payee", "")
                description = tx.get("description", "")
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
                        pending=pending
                    )
                    db.add(db_tx)
                else:
                    db_tx.account_id = acc_id
                    db_tx.date = date
                    db_tx.amount = amount
                    db_tx.payee = payee
                    db_tx.description = description
                    db_tx.pending = pending

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving data to DB: {e}")
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

def get_stored_transactions(limit=100):
    db = SessionLocal()
    try:
        transactions = db.query(Transaction).order_by(Transaction.date.desc()).limit(limit).all()
        return [tx.to_dict() for tx in transactions]
    finally:
        db.close()
