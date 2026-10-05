"""OpenID Connect (OIDC) support for the web dashboard.

This module implements the authorization-code flow with PKCE for Authentik
and any other OIDC-compliant provider. It relies on the provider's discovery
document and JWKS endpoint to validate ID tokens locally.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urljoin

import httpx
from fastapi import HTTPException, Request, status
from jose import exceptions as jose_exceptions  # type: ignore[import-untyped]
from jose import jwk as jose_jwk  # type: ignore[import-untyped]
from jose import jwt as jose_jwt  # type: ignore[import-untyped]

from bot.secrets_store import SecretsStore


class OIDCProvider:
    """Lightweight OIDC client using discovery, PKCE, and local JWT validation.

    Configuration comes from environment variables (authoritative) with a
    fallback to the dashboard-managed secrets store, so OIDC can be set up
    entirely from the app once the env file no longer defines these values.
    """

    # Supported signature algorithms for ID tokens. We deliberately exclude
    # symmetric algorithms (HS256/HS384/HS512) to avoid key-derivation issues.
    SUPPORTED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

    def __init__(self) -> None:
        self._discovery: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._discovered_at: Optional[datetime] = None

    def _setting(self, field: str) -> str:
        """Return an effective OIDC setting: env var wins, secrets file falls back."""
        return SecretsStore().resolve("oidc").get(field) or ""

    @property
    def enabled(self) -> bool:
        return self._setting("enabled") == "true"

    @property
    def issuer_url(self) -> str:
        url = self._setting("issuer_url").strip()
        if not url and self.enabled:
            raise RuntimeError("OIDC_ISSUER_URL is required when OIDC is enabled")
        if url and not url.endswith("/"):
            url = url + "/"
        return url

    @property
    def client_id(self) -> str:
        client_id = self._setting("client_id").strip()
        if not client_id and self.enabled:
            raise RuntimeError("OIDC_CLIENT_ID is required when OIDC is enabled")
        return client_id

    @property
    def client_secret(self) -> str:
        return self._setting("client_secret").strip()

    @property
    def redirect_uri(self) -> str:
        return self._setting("redirect_uri").strip()

    @property
    def scopes(self) -> str:
        return self._setting("scopes").strip() or "openid email profile"

    def _discovery_url(self) -> str:
        return urljoin(self.issuer_url, ".well-known/openid-configuration")

    def _redirect_uri(self, request: Request) -> str:
        if self.redirect_uri:
            return self.redirect_uri
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{scheme}://{host}/auth/callback"

    async def _fetch_json(self, url: str) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Could not reach OIDC provider: {exc}",
            ) from exc

    async def _get_discovery(self) -> Dict[str, Any]:
        if self._discovery is None or self._discovered_at is None:
            self._discovery = await self._fetch_json(self._discovery_url())
            self._discovered_at = datetime.now(timezone.utc)
            return self._discovery

        # Refresh discovery document every hour to pick up key rotations.
        if datetime.now(timezone.utc) - self._discovered_at > timedelta(hours=1):
            self._discovery = await self._fetch_json(self._discovery_url())
            self._discovered_at = datetime.now(timezone.utc)
        return self._discovery

    async def _get_jwks(self) -> Dict[str, Any]:
        if self._jwks is not None:
            return self._jwks

        discovery = await self._get_discovery()
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC discovery document does not contain a jwks_uri",
            )
        self._jwks = await self._fetch_json(jwks_uri)
        return self._jwks

    @staticmethod
    def _generate_code_verifier() -> str:
        """Generate a PKCE code verifier (43-128 chars, URL-safe)."""
        return secrets.token_urlsafe(48)  # 48 bytes -> 64 chars

    @staticmethod
    def _generate_code_challenge(verifier: str) -> str:
        """Generate the S256 code challenge for a verifier."""
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @staticmethod
    def _generate_state() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _generate_nonce() -> str:
        return secrets.token_urlsafe(32)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="OIDC authentication is not enabled",
            )

    async def build_authorization_url(self, request: Request) -> tuple[str, str, str, str]:
        """Return (authorize_url, state, nonce, code_verifier)."""
        self._require_enabled()
        discovery = await self._get_discovery()
        authorize_endpoint = discovery.get("authorization_endpoint")
        if not authorize_endpoint:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC discovery document does not contain an authorization_endpoint",
            )

        state = self._generate_state()
        nonce = self._generate_nonce()
        code_verifier = self._generate_code_verifier()
        code_challenge = self._generate_code_challenge(code_verifier)
        redirect_uri = self._redirect_uri(request)

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": self.scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in authorize_endpoint else "?"
        return (
            f"{authorize_endpoint}{separator}{urlencode(params)}",
            state,
            nonce,
            code_verifier,
        )

    async def exchange_code(self, request: Request, code: str, code_verifier: str) -> Dict[str, Any]:
        """Exchange an authorization code for tokens at the provider's token endpoint."""
        self._require_enabled()
        discovery = await self._get_discovery()
        token_endpoint = discovery.get("token_endpoint")
        if not token_endpoint:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC discovery document does not contain a token_endpoint",
            )

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri(request),
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    token_endpoint,
                    data=payload,
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"OIDC token exchange failed: {exc}",
            ) from exc

    async def validate_id_token(self, token: str) -> Dict[str, Any]:
        """Validate an ID token's signature and claims."""
        self._require_enabled()
        try:
            header = jose_jwt.get_unverified_header(token)
        except jose_exceptions.JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid ID token header: {exc}",
            ) from exc

        kid = header.get("kid")
        header_alg = header.get("alg")
        jwks = await self._get_jwks()
        keys = jwks.get("keys", [])
        key_data = None

        if kid:
            matches = [k for k in keys if k.get("kid") == kid]
            if not matches:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="OIDC token signed with unknown key",
                )
            if len(matches) > 1:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="OIDC JWKS contains ambiguous key identifiers",
                )
            key_data = matches[0]
        else:
            # Only accept a token without a kid when the provider publishes exactly
            # one compatible key, so the signing key is unambiguous.
            compatible = [
                k for k in keys
                if not header_alg or not k.get("alg") or k.get("alg") == header_alg
            ]
            if len(compatible) == 1:
                key_data = compatible[0]
            elif not keys:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="OIDC provider returned no signing keys",
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="OIDC token missing key ID with ambiguous JWKS",
                )

        # Reject algorithm mismatches before signature verification.
        if header_alg and key_data.get("alg") and key_data["alg"] != header_alg:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OIDC token algorithm does not match signing key",
            )

        if header_alg not in self.SUPPORTED_ALGORITHMS:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OIDC token uses an unsupported signing algorithm",
            )

        try:
            key = jose_jwk.construct(key_data, algorithm=header_alg)
            claims = jose_jwt.decode(
                token,
                key,
                algorithms=self.SUPPORTED_ALGORITHMS,
                issuer=self.issuer_url,
                audience=self.client_id,
                options={
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "require_exp": True,
                    "require_iat": True,
                },
            )
        except jose_exceptions.JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"ID token validation failed: {exc}",
            ) from exc

        return claims

    @staticmethod
    def extract_username(claims: Dict[str, Any]) -> str:
        """Pick a human-readable username from validated ID token claims."""
        for key in ("preferred_username", "email"):
            value = claims.get(key)
            if value and isinstance(value, str) and value.strip():
                return value.strip()
        return claims.get("sub", "oidc-user")


oidc_provider = OIDCProvider()
