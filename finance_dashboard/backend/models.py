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
    _amount = Column("amount", String, default="0.00")
    _payee = Column("payee", String, default="")
    _description = Column("description", String, default="")
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
