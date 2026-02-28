import base64
import hashlib
import logging
from lenny.configs import LENNY_SEED, LENNY_EMAIL_ENCRYPTION_SALT
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256

logger = logging.getLogger(__name__)

def encode_book_path(book_id: str, format=".epub") -> str:
    """This should be moved to a general utils.py within core"""
    if not "." in book_id:
        book_id += format
    path = f"s3://bookshelf/{book_id}"
    logger.info(f"path: {path}")
    encoded = base64.b64encode(path.encode()).decode()
    return encoded.replace('/', '_').replace('+', '-').replace('=', '')

def hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()

_DERIVED_KEY_CACHE = {}

def _get_derived_key():
    """Derives and caches the encryption key to avoid expensive PBKDF2 on every request."""
    if 'key' in _DERIVED_KEY_CACHE:
        return _DERIVED_KEY_CACHE['key']
    
    if not LENNY_SEED:
        raise ValueError("LENNY_SEED is not set")
    if not LENNY_EMAIL_ENCRYPTION_SALT:
        raise ValueError("LENNY_EMAIL_ENCRYPTION_SALT is not set")
    
    salt = LENNY_EMAIL_ENCRYPTION_SALT.encode('utf-8')
    # 100k iterations is expensive (From ~40ms to <0.1s), so we cache the key here 
    key = PBKDF2(LENNY_SEED.encode('utf-8'), salt, dkLen=32, count=100000, hmac_hash_module=SHA256)
    _DERIVED_KEY_CACHE['key'] = key
    return key

def _get_cipher(nonce=None):
    key = _get_derived_key()
    if nonce is not None:
        return AES.new(key, AES.MODE_GCM, nonce=nonce)
    return AES.new(key, AES.MODE_GCM)

def encrypt_email(email: str) -> str:
    """Encrypts email using AES-GCM and returns base64 string with 'v1:' prefix."""
    try:
        cipher = _get_cipher()
        ciphertext, tag = cipher.encrypt_and_digest(email.encode('utf-8'))
        # Payload: nonce (16) + tag (16) + ciphertext
        payload = cipher.nonce + tag + ciphertext
        encoded = base64.urlsafe_b64encode(payload).decode('utf-8')
        return f"v1:{encoded}"
    except Exception as e:
        logger.error(f"Failed to encrypt email: {type(e).__name__}")
        raise

def decrypt_email(encrypted_data: str) -> str:
    """Decrypts email encoded with version prefix (v1:) or legacy (v0)."""
    try:
        if encrypted_data.startswith("v1:"):
            payload_str = encrypted_data[3:]
        else:
            # Legacy data (no prefix)
            payload_str = encrypted_data

        # Normalize base64 padding for legacy payloads
        pad = (-len(payload_str)) % 4
        payload_str += '=' * pad
        decoded = base64.urlsafe_b64decode(payload_str)
        if len(decoded) < 32:
            raise ValueError("Invalid encrypted payload: must be at least 32 bytes")

        nonce = decoded[:16]
        tag = decoded[16:32]
        ciphertext = decoded[32:]
        
        cipher = _get_cipher(nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to decrypt email: {type(e).__name__}")
        raise
