from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from fastapi import Depends, Header, HTTPException, Request, status

from ..auth.policy import ROUTE_AUTH_POLICIES, RouteAuthPolicy
from ..auth.service import AuthenticationError, AuthenticationService
from ..config import AuthMode, StaticCredentialConfig
from ..diagnostics.bindings import FOUNDATION_CODES

_CREDENTIALS_REJECTED = FOUNDATION_CODES["auth.credentials_rejected"]
_POLICY_DENIED = FOUNDATION_CODES["auth.policy_denied"]
_AUTH_DIAGNOSTIC_CODES = {
    "missing_authorization": _CREDENTIALS_REJECTED,
    "invalid_authorization": _CREDENTIALS_REJECTED,
    "invalid_token": _CREDENTIALS_REJECTED,
    "invalid_client": _CREDENTIALS_REJECTED,
    "insufficient_scope": _POLICY_DENIED,
    "remote_client_forbidden": _POLICY_DENIED,
    "exchange_not_permitted": _POLICY_DENIED,
    "client_policy_denied": _POLICY_DENIED,
    "scope_not_allowed": _POLICY_DENIED,
    "client_expiring": _POLICY_DENIED,
    "authenticator_unavailable": FOUNDATION_CODES["auth.capability_unavailable"],
}


class AuthHTTPException(HTTPException):
    def __init__(self, *, status_code: int, detail: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.diagnostic_code = _AUTH_DIAGNOSTIC_CODES.get(detail["code"])


@dataclass(frozen=True)
class ApiPrincipal:
    subject: str
    scopes: frozenset[str]
    client_host: str | None
    kind: str = "static"
    client_id: str | None = None
    token_id: str | None = None

    def has_scope(self, required: str) -> bool:
        return "*" in self.scopes or required in self.scopes

    def require(self, *required_scopes: str) -> None:
        missing = [scope for scope in required_scopes if not self.has_scope(scope)]
        if missing:
            raise AuthHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "insufficient_scope",
                    "message": "Missing required API scope.",
                    "details": {"missing_scopes": missing},
                },
            )


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    return host in {"127.0.0.1", "::1", "localhost"}


def _load_static_credentials() -> tuple[StaticCredentialConfig, ...]:
    from ..main import _APP_CFG  # late import to avoid circular dependency

    if _APP_CFG is None:
        return ()
    auth = _APP_CFG.api.auth
    if auth.mode not in {AuthMode.STATIC, AuthMode.HYBRID}:
        return ()
    return auth.credentials


def _authenticate_token(token: str) -> StaticCredentialConfig | None:
    for credential in _load_static_credentials():
        if hmac.compare_digest(credential.token, token):
            return credential
    return None


def _checked_client_host(request: Request, *, allow_remote: bool) -> str | None:
    client_host = request.client.host if request.client else None
    if allow_remote or _is_loopback(client_host):
        return client_host
    raise AuthHTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "remote_client_forbidden",
            "message": "This API only accepts loopback clients unless remote access is enabled.",
            "details": {"client_host": client_host},
        },
    )


def _authorization_value(authorization: str | None, scheme_name: str) -> str:
    scheme, separator, value = (authorization or "").partition(" ")
    if separator and scheme.lower() == scheme_name.lower() and value.strip():
        return value.strip()
    code = "missing_authorization" if not authorization else "invalid_authorization"
    raise AuthHTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": f"Authorization must use the {scheme_name} scheme."},
        headers={"WWW-Authenticate": scheme_name},
    )


def _auth_service(request: Request) -> AuthenticationService:
    service: AuthenticationService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise AuthHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "authenticator_unavailable", "message": "Exchange authentication is unavailable."},
        )
    return service


def raise_authentication_error(exc: AuthenticationError, scheme: str) -> NoReturn:
    raise AuthHTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
        headers={"WWW-Authenticate": scheme} if exc.status_code == 401 else None,
    ) from exc


async def get_api_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ApiPrincipal:
    from ..main import _APP_CFG  # late import to avoid circular dependency

    client_host = _checked_client_host(request, allow_remote=_APP_CFG.api.allow_remote if _APP_CFG else False)
    raw_token = _authorization_value(authorization, "Bearer")
    mode = _APP_CFG.api.auth.mode if _APP_CFG else AuthMode.STATIC
    exchange_family = raw_token.startswith(("swa_", "swc_"))
    credential = None if mode is AuthMode.HYBRID and exchange_family else _authenticate_token(raw_token)
    if credential is not None and mode in {AuthMode.STATIC, AuthMode.HYBRID}:
        return ApiPrincipal(
            subject=credential.subject,
            scopes=credential.scopes,
            client_host=client_host,
        )

    if mode is AuthMode.STATIC:
        raise AuthHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Bearer token was not recognized."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    service = _auth_service(request)
    try:
        principal = service.authorize_access(raw_token, source_ip=client_host or "", path=request.url.path)
    except AuthenticationError as exc:
        raise_authentication_error(exc, "Bearer")
    return ApiPrincipal(
        subject=principal.subject,
        scopes=principal.scopes,
        client_host=client_host,
        kind="access_token",
        client_id=principal.client_id,
        token_id=principal.token_id,
    )


async def get_client_authentication(
    request: Request,
    authorization: str | None = Header(default=None),
) -> tuple[AuthenticationService, str, str]:
    from ..main import _APP_CFG

    mode = _APP_CFG.api.auth.mode if _APP_CFG else AuthMode.STATIC
    if mode is AuthMode.STATIC:
        raise AuthHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "exchange_not_permitted", "message": "Authentication mode does not permit token exchange."},
        )
    service = _auth_service(request)
    credential = _authorization_value(authorization, "SeasonalClient")
    client_host = _checked_client_host(request, allow_remote=_APP_CFG.api.allow_remote if _APP_CFG else False)
    return service, credential, client_host or ""


def require_scopes(*scopes: str) -> Callable[..., Any]:
    async def _dependency(principal: ApiPrincipal = Depends(get_api_principal)) -> ApiPrincipal:
        principal.require(*scopes)
        return principal

    return _dependency


def route_auth_policy(method: str, path: str) -> RouteAuthPolicy:
    key = (method.strip().upper(), path)
    try:
        return ROUTE_AUTH_POLICIES[key]
    except KeyError as exc:
        raise RuntimeError(f"No authentication policy is declared for {key[0]} {path}.") from exc


def require_route_policy(method: str, path: str) -> Callable[..., Any]:
    policy = route_auth_policy(method, path)
    if policy.public or policy.client_credential:
        raise RuntimeError(f"Public route {method.strip().upper()} {path} must not install an auth dependency.")
    dependency = require_scopes(*sorted(policy.scopes))
    dependency.__dict__["__seasonalweather_auth_policy__"] = policy
    return dependency


get_client_authentication.__dict__["__seasonalweather_auth_policy__"] = ROUTE_AUTH_POLICIES[("POST", "/v1/auth/token")]
