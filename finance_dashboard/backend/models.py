import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_PATH = os.environ.get("FINANCE_DB_PATH", "/data/finance.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Account(Base):
    __tablename__ = "accounts"

    id = Column(String, primary_key=True, index=True)
    org = Column(String, default="Unknown")
    name = Column(String, default="Account")
    balance = Column(String, default="0.00")
    currency = Column(String, default="USD")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = relationship("Transaction", back_populates="account", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "org": self.org,
            "name": self.name,
            "balance": self.balance,
            "currency": self.currency,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, index=True)
    account_id = Column(String, ForeignKey("accounts.id"), index=True)
    date = Column(Integer)  # Unix timestamp
    amount = Column(String, default="0.00")
    payee = Column(String, default="")
    description = Column(String, default="")
    pending = Column(Boolean, default=False)

    account = relationship("Account", back_populates="transactions")

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account.name if self.account else "Unknown",
            "org": self.account.org if self.account else "Unknown",
            "date": self.date,
            "amount": self.amount,
            "payee": self.payee,
            "description": self.description,
            "pending": 1 if self.pending else 0
        }
