import sqlite3
import os

DB_PATH = os.environ.get("FINANCE_DB_PATH", "/data/finance.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Accounts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            org TEXT,
            name TEXT,
            balance TEXT,
            currency TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Transactions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT,
            date INTEGER,
            amount TEXT,
            payee TEXT,
            description TEXT,
            pending INTEGER,
            FOREIGN KEY (account_id) REFERENCES accounts (id)
        )
    """)
    
    conn.commit()
    conn.close()

def save_simplefin_data(data):
    """Save normalized SimpleFIN accounts and transactions into SQLite."""
    conn = get_db()
    cursor = conn.cursor()
    
    accounts = data.get("accounts", [])
    for acc in accounts:
        acc_id = str(acc.get("id"))
        org = acc.get("org", {}).get("name", acc.get("org", "Unknown"))
        name = acc.get("name", "Account")
        balance = str(acc.get("balance", "0.00"))
        currency = acc.get("currency", "USD")
        
        cursor.execute("""
            INSERT INTO accounts (id, org, name, balance, currency, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                org=excluded.org,
                name=excluded.name,
                balance=excluded.balance,
                currency=excluded.currency,
                updated_at=CURRENT_TIMESTAMP
        """, (acc_id, org, name, balance, currency))
        
        # Save transactions for this account if present
        transactions = acc.get("transactions", [])
        for tx in transactions:
            tx_id = str(tx.get("id"))
            date = tx.get("date")
            amount = str(tx.get("amount", "0.00"))
            payee = tx.get("payee", "")
            description = tx.get("description", "")
            pending = 1 if tx.get("pending") else 0
            
            cursor.execute("""
                INSERT INTO transactions (id, account_id, date, amount, payee, description, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    account_id=excluded.account_id,
                    date=excluded.date,
                    amount=excluded.amount,
                    payee=excluded.payee,
                    description=excluded.description,
                    pending=excluded.pending
            """, (tx_id, acc_id, date, amount, payee, description, pending))
            
    conn.commit()
    conn.close()

def get_stored_accounts():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_stored_transactions(limit=100):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT t.*, a.name as account_name, a.org FROM transactions t JOIN accounts a ON t.account_id = a.id ORDER BY t.date DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
