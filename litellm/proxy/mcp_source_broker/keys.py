import hashlib
import hmac
import secrets
from typing import Final
from uuid import NAMESPACE_URL, uuid5


class Keys:
    def __init__(self, pepper: str) -> None:
        if len(pepper.encode()) < 32:
            raise ValueError("pepper must be at least 32 bytes")
        self.pepper: Final = pepper.encode()

    def _hmac(self, name: str, *parts: str) -> bytes:
        return hmac.new(self.pepper, "\0".join((name, *parts)).encode(), hashlib.sha256).digest()

    def enrollment_id(self, source_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, self._hmac("enrollment", source_id).hex()))

    @staticmethod
    def mint_token() -> str:
        return secrets.token_urlsafe(32)

    def token_hash(self, grant_id: str, token: str) -> bytes:
        return self._hmac("enrollment-token", grant_id, token)

    def password(self, grant_id: str, connector_id: str) -> str:
        return self._hmac("connector", grant_id, connector_id).hex()

    def digest(self, grant_id: str, connector_id: str) -> bytes:
        return self._hmac("credential", grant_id, self.password(grant_id, connector_id))

    @staticmethod
    def username(connector_id: str) -> str:
        return f"source-{connector_id.replace('-', '')[:24]}"

    def gateway_ciphertext(self, key: str) -> str:
        return self._hmac("gateway", key).hex()
