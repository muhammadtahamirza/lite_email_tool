"""
Encrypts/decrypts mailbox passwords before they touch the database. Both the
backend (encrypts on save) and the engine (decrypts to actually log in) use
this exact same key — set as ENCRYPTION_KEY in both Render's env and GitHub
Secrets. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
from cryptography.fernet import Fernet

_key = os.getenv("ENCRYPTION_KEY")
if not _key:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "and set it as an env var (Render) / secret (GitHub Actions)."
    )

_fernet = Fernet(_key.encode())


def encrypt_password(plain_password: str) -> str:
    return _fernet.encrypt(plain_password.encode()).decode()


def decrypt_password(encrypted_password: str) -> str:
    return _fernet.decrypt(encrypted_password.encode()).decode()
