import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from security import encrypt_value, decrypt_value

DB_PATH = os.environ.get("FINANCE_DB_PATH", "/data/finance.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

ACCOUNT_TYPE_UNCLASSIFIED = "unclassified"
ACCOUNT_TYPE_VALUES = frozenset({
    ACCOUNT_TYPE_UNCLASSIFIED,
    "bank_account",
    "investment",
    "credit_card",
    "loan",
    "other_asset",
    "other_liability",
})
ASSET_ACCOUNT_TYPES = frozenset({"bank_account", "investment", "other_asset"})
LIABILITY_ACCOUNT_TYPES = frozenset({"credit_card", "loan", "other_liability"})

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True, index=True)
    _value = Column("value", Text, default="")

    @property
    def value(self):
        return decrypt_value(self._value)

    @value.setter
    def value(self, val):
        self._value = encrypt_value(val)

class Account(Base):
    __tablename__ = "accounts"

    id = Column(String, primary_key=True, index=True)
    _org = Column("org", String, default="Unknown")
    _name = Column("name", String, default="Account")
    _balance = Column("balance", String, default="0.00")
    currency = Column(String, default="USD")
    account_type = Column(String, default=ACCOUNT_TYPE_UNCLASSIFIED, nullable=False)
    account_type_suggestion = Column(String, default=ACCOUNT_TYPE_UNCLASSIFIED, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = relationship("Transaction", back_populates="account", cascade="all, delete-orphan")

    @property
    def org(self):
        return decrypt_value(self._org)

    @org.setter
    def org(self, value):
        self._org = encrypt_value(value)

    @property
    def name(self):
        return decrypt_value(self._name)

    @name.setter
    def name(self, value):
        self._name = encrypt_value(value)

    @property
    def balance(self):
        return decrypt_value(self._balance)

    @balance.setter
    def balance(self, value):
        self._balance = encrypt_value(value)

    def to_dict(self):
        return {
            "id": self.id,
            "org": self.org if self.org and self.org.strip().lower() != "unknown" else (self.name or "Account"),
            "name": self.name,
            "balance": self.balance,
            "currency": self.currency,
            "account_type": self.account_type or ACCOUNT_TYPE_UNCLASSIFIED,
            "account_type_suggestion": self.account_type_suggestion or ACCOUNT_TYPE_UNCLASSIFIED,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, index=True)
    account_id = Column(String, ForeignKey("accounts.id"), index=True)
    date = Column(Integer)  # Unix timestamp
    _amount = Column("amount", String, default="0.00")
    _payee = Column("payee", String, default="")
    _description = Column("description", String, default="")
    category = Column(String, default="Uncategorized")
    pending = Column(Boolean, default=False)

    account = relationship("Account", back_populates="transactions")

    @property
    def amount(self):
        return decrypt_value(self._amount)

    @amount.setter
    def amount(self, value):
        self._amount = encrypt_value(value)

    @property
    def payee(self):
        return decrypt_value(self._payee)

    @payee.setter
    def payee(self, value):
        self._payee = encrypt_value(value)

    @property
    def description(self):
        return decrypt_value(self._description)

    @description.setter
    def description(self, value):
        self._description = encrypt_value(value)

    def to_dict(self):
        account_name = "Unknown"
        if self.account:
            if self.account.name and self.account.name.strip().lower() != "unknown":
                account_name = self.account.name
            elif self.account.org and self.account.org.strip().lower() != "unknown":
                account_name = self.account.org
            else:
                account_name = self.account_id or "Unknown"

        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": account_name,
            "org": self.account.org if self.account and self.account.org and self.account.org.strip().lower() != "unknown" else account_name,
            "date": self.date,
            "amount": self.amount,
            "payee": self.payee,
            "description": self.description,
            "category": self.category or "Uncategorized",
            "pending": 1 if self.pending else 0
        }
