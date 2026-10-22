import os
import base64
import logging
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("finance_dashboard")

KEY_PATH = os.environ.get("FINANCE_KEY_PATH", "/data/.encryption_key")

def get_or_create_key():
    """Generate or load a secure Fernet encryption key based on Supervisor/Add-on environment."""
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()
    
    # Generate a secure random key or derive from a secret salt
    salt = os.urandom(16)
    # Use supervisor token or secret if available, else random bytes
    secret = os.environ.get("SUPERVISOR_TOKEN", os.urandom(32).hex()).encode()
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret))
    
    try:
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "wb") as f:
            f.write(key)
        os.chmod(KEY_PATH, 0o600)
    except Exception as e:
        logger.error("Could not persist finance encryption key (%s).", type(e).__name__)
        raise RuntimeError("Could not persist finance encryption key; refusing to start with an ephemeral key.") from e
        
    return key

FERNET_KEY = get_or_create_key()
cipher = Fernet(FERNET_KEY)

def encrypt_value(plain_text: str) -> str:
    if not plain_text:
        return plain_text
    try:
        return cipher.encrypt(plain_text.encode('utf-8')).decode('utf-8')
    except Exception as e:
        logger.error("Finance data encryption failed (%s).", type(e).__name__)
        raise RuntimeError("Could not encrypt finance data; refusing to store it in plaintext.") from e

def decrypt_value(cipher_text: str) -> str:
    if not cipher_text:
        return cipher_text
    try:
        return cipher.decrypt(cipher_text.encode('utf-8')).decode('utf-8')
    except Exception as e:
        # If it wasn't encrypted or failed, return as-is for backward compatibility
        return cipher_text
