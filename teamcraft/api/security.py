"""Local session-token lifecycle (M3-01).

The token is the single shared secret between the local CLI/UI bootstrap and
the API.  Design points:

* 256 bits of CSPRNG entropy (``secrets``), URL-safe encoding;
* on-disk storage under the project runtime directory, directory ``0700`` and
  file ``0600``, written atomically (temp + ``os.replace``) so a crash can
  never leave a half-written token;
* rotation is a single active token: after :meth:`SessionTokenStore.rotate`
  the previous token stops verifying immediately;
* verification is constant-time (:func:`hmac.compare_digest`).

The plaintext token file is deliberate: it is the credential store itself (a
local bearer token), and hashing it on disk would not raise the bar for an
attacker who can already read the file — owner-only modes are the boundary.
The token is never accepted from, or written to, URLs or logs.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import tempfile
from hashlib import sha256
from pathlib import Path

TOKEN_BYTES = 32
_MIN_TOKEN_LENGTH = 32
_TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class SessionTokenStore:
    """Issue, persist, verify and rotate the local session token.

    Args:
        path: Optional file for durable storage.  ``None`` keeps the token in
            memory only (useful for tests and embedded use); nothing is
            created until :meth:`ensure` is called.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._token: str | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    @staticmethod
    def generate_token() -> str:
        """Return a fresh 256-bit URL-safe token."""

        return secrets.token_urlsafe(TOKEN_BYTES)

    @classmethod
    def _validate_shape(cls, token: str) -> bool:
        return isinstance(token, str) and _TOKEN_SHAPE_RE.match(token) is not None

    def ensure(self) -> str:
        """Return the active token, creating and persisting one when needed."""

        if self._token is not None:
            return self._token
        if self._path is None:
            self._token = self.generate_token()
            return self._token
        existing = self._read()
        if existing is not None and self._validate_shape(existing):
            self._token = existing
            return self._token
        # Missing, empty, or malformed (tampered/truncated) material is
        # replaced rather than trusted; the old value is unusable anyway.
        return self.rotate()

    def token(self) -> str:
        """Alias for :meth:`ensure`."""

        return self.ensure()

    def _read(self) -> str | None:
        assert self._path is not None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        value = raw.strip()
        return value or None

    def _persist(self, token: str) -> None:
        assert self._path is not None
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_dir(directory)
        handle, temp_name = tempfile.mkstemp(
            dir=str(directory), prefix=".session-token-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self._path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        os.chmod(self._path, 0o600)

    def rotate(self) -> str:
        """Replace the active token with a fresh random one and return it.

        Any previously issued token stops verifying at this point.
        """

        token = self.generate_token()
        if self._path is None:
            self._token = token
            return token
        self._persist(token)
        self._token = token
        return token

    def verify(self, candidate: str | None) -> bool:
        """Constant-time check of ``candidate`` against the active token."""

        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(candidate, self.ensure())

    def token_fingerprint(self) -> str:
        """Non-secret identifier for the active token (for logs/audit)."""

        return sha256(self.ensure().encode("utf-8")).hexdigest()[:12]

    def forget(self) -> None:
        """Drop the in-memory copy (the on-disk file, if any, is kept)."""

        self._token = None


def _restrict_dir(directory: Path) -> None:
    # Windows has no POSIX mode semantics here; best-effort on POSIX.
    if os.name != "posix":
        return
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
