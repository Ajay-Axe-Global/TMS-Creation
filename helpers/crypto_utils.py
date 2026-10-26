"""
Reversible encryption for secrets we must be able to read back: the user's TMS
password and their saved browser session. Login passwords never come through
here — those are one-way hashes (helpers/jwt_utils.py).

CREDENTIALS_KEY must be a Fernet key. Generate once per environment:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Rotating or losing it makes every stored TMS password and session unreadable.
"""
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIALS_KEY")
    if not key:
        raise RuntimeError("CREDENTIALS_KEY is not set in .env — required to store TMS credentials.")
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Stored secret could not be decrypted — CREDENTIALS_KEY may have changed.")
