import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from security import encrypt_value, decrypt_value

DB_PATH = os.environ.get("FINANCE_DB_PATH", "/data/finance.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

ACCOUNT_TYPE_UNCLASSIFIED = "unclassified"
ACCOUNT_TYPE_SOURCE_AUTO = "auto"
ACCOUNT_TYPE_SOURCE_MANUAL = "manual"
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
    _nickname = Column("nickname", String, default="")
    _balance = Column("balance", String, default="0.00")
    currency = Column(String, default="USD")
    account_type = Column(String, default=ACCOUNT_TYPE_UNCLASSIFIED, nullable=False)
    account_type_suggestion = Column(String, default=ACCOUNT_TYPE_UNCLASSIFIED, nullable=False)
    account_type_source = Column(String, default=ACCOUNT_TYPE_SOURCE_AUTO, nullable=False)
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
    def nickname(self):
        return decrypt_value(self._nickname)

    @nickname.setter
    def nickname(self, value):
        self._nickname = encrypt_value(value)

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
            "nickname": self.nickname or "",
            "balance": self.balance,
            "currency": self.currency,
            "account_type": self.account_type or ACCOUNT_TYPE_UNCLASSIFIED,
            "account_type_suggestion": self.account_type_suggestion or ACCOUNT_TYPE_UNCLASSIFIED,
            "account_type_source": self.account_type_source or ACCOUNT_TYPE_SOURCE_AUTO,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class ManualAsset(Base):
    __tablename__ = "manual_assets"

    id = Column(String, primary_key=True, index=True)
    _name = Column("name", String, default="Asset")
    _value = Column("value", String, default="0.00")
    currency = Column(String, default="USD")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def name(self):
        return decrypt_value(self._name)

    @name.setter
    def name(self, value):
        self._name = encrypt_value(value)

    @property
    def value(self):
        return decrypt_value(self._value)

    @value.setter
    def value(self, value):
        self._value = encrypt_value(value)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "value": self.value,
            "currency": self.currency or "USD",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
            "account_nickname": self.account.nickname if self.account else "",
            "org": self.account.org if self.account and self.account.org and self.account.org.strip().lower() != "unknown" else account_name,
            "date": self.date,
            "amount": self.amount,
            "payee": self.payee,
            "description": self.description,
            "category": self.category or "Uncategorized",
            "pending": 1 if self.pending else 0
        }


class AccountDataCoverage(Base):
    """Audit of which account/date windows were requested and verified."""
    __tablename__ = "account_data_coverage"
    __table_args__ = (
        UniqueConstraint("account_id", "start_ts", "end_ts", name="uq_account_coverage_window"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    start_ts = Column(Integer, nullable=False)
    end_ts = Column(Integer, nullable=False)
    status = Column(String, default="pending", nullable=False)
    source = Column(String, default="backfill", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    error_code = Column(String, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    retry_after = Column(DateTime, nullable=True)
