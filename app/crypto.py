import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _fernet() -> Fernet:
    digest = hashlib.sha256(("model-monitor:" + config.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode() if plain else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("无法解密已保存的密钥，SECRET_KEY 是否被修改过？") from e


def mask(secret: str) -> str:
    if not secret:
        return ""
    return secret[:4] + "****" + secret[-4:] if len(secret) > 10 else "****"
