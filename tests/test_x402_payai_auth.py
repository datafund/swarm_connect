"""The gateway authenticates to the PayAI facilitator with a PayAI API key (#662).

A PayAI key is a key ID and a secret, a base64 PKCS#8 Ed25519 private key.
@payai/facilitator 2.4.6 signs a 120 s JWT with it and sends it as a bearer
token on /verify, /settle and /supported, re-signing once 30 s or less
remain. These tests generate their own keys; no real key and no network call
is used anywhere (the facilitator is an httpx2 MockTransport).
"""
import asyncio
import base64
import json
import logging
import re
import uuid
from pathlib import Path

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import settings
from app.x402 import check_config, facilitator
from app.x402.facilitator_client import FacilitatorClient
from tests.test_x402_check_config import run_check

PAY_TO = "0x" + "ab" * 20
URL = "https://facilitator.example/x402"
KEY_ID = "test-payai-key-id-0001"
V2_EXACT = {"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}
DEPLOY_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


def _pkcs8_der(key) -> bytes:
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def new_key():
    """A fresh Ed25519 key and its secret in PayAI's form (base64 PKCS#8 DER)."""
    key = Ed25519PrivateKey.generate()
    return key, base64.b64encode(_pkcs8_der(key)).decode()


KEY, SECRET = new_key()
SEED = KEY.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                         serialization.NoEncryption())


def key_material(secret=SECRET, seed=SEED):
    """Every form in which the secret could leak."""
    der = base64.b64decode(secret)
    return [secret, secret[:24], base64.urlsafe_b64encode(der).decode().rstrip("="), der.hex(),
            seed.hex(), base64.b64encode(seed).decode().rstrip("="),
            base64.urlsafe_b64encode(seed).decode().rstrip("=")]


def assert_no_key_material(text, secret=SECRET, seed=SEED):
    for value in key_material(secret, seed):
        assert value not in text
        assert value.lower() not in text.lower()


@pytest.fixture
def x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setattr(settings, "X402_FACILITATOR_URL", URL)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_ID", None, raising=False)
    monkeypatch.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_SECRET", None, raising=False)
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1")
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


@pytest.fixture
def payai(x402):
    x402.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_ID", KEY_ID)
    x402.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_SECRET", SECRET)
    return x402


class Clock:
    def __init__(self, t=1_800_000_000.25):
        self.t = t

    def __call__(self):
        return self.t


def _b64url_json(part: str):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def assert_payai_jwt(token, key=KEY, key_id=KEY_ID, now=None):
    """The token @payai/facilitator 2.4.6 builds, verified with the public key."""
    head, body, _ = token.split(".")
    raw_header = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
    # The package's layout: JSON.stringify, compact, keys in this order.
    assert raw_header == json.dumps({"alg": "EdDSA", "typ": "JWT", "kid": key_id},
                                    separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload = _b64url_json(body)
    assert list(payload) == ["sub", "iss", "iat", "exp", "jti"]
    assert payload["sub"] == key_id and payload["iss"] == "payai-merchant"
    assert type(payload["iat"]) is int and payload["exp"] == payload["iat"] + 120
    assert str(uuid.UUID(payload["jti"])) == payload["jti"]
    if now is not None:
        assert payload["iat"] == int(now)
    decoded = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], issuer="payai-merchant",
                         options={"verify_exp": now is None, "verify_iat": now is None})
    assert decoded == payload
    return payload


# --- the token ---------------------------------------------------------------

def test_token_has_the_payai_header_and_payload_and_verifies():
    clock = Clock()
    auth = facilitator.PayAIAuth(KEY_ID, facilitator.load_payai_private_key(SECRET), clock=clock)
    assert_payai_jwt(auth.token(), now=clock.t)


def test_a_token_signed_with_another_key_does_not_verify():
    auth = facilitator.PayAIAuth(KEY_ID, facilitator.load_payai_private_key(SECRET))
    other, _ = new_key()
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(auth.token(), other.public_key(), algorithms=["EdDSA"])


@pytest.mark.parametrize("form", [
    lambda s: s,
    lambda s: f"  {s}\n",                                         # trimmed
    lambda s: "payai_sk_" + s,                                    # the prefix stripped
    lambda s: " payai_sk_" + s + " ",
    lambda s: s.rstrip("=").replace("+", "-").replace("/", "_"),  # url-safe, no padding
    lambda s: "\ufeff" + s + "\ufeff",                          # JS trim() strips U+FEFF
    lambda s: "\u00a0" + s + "\u00a0\u3000\u2028",              # ...and NBSP, Zs, LS
    lambda s: s[:20] + " \n\t" + s[20:],                         # atob drops ASCII whitespace
])
def test_secret_normalization_matches_the_package(form):
    key = facilitator.load_payai_private_key(form(SECRET))
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    assert raw == SEED


# --- the cache ---------------------------------------------------------------

def test_cache_returns_the_same_token_inside_the_window_and_a_new_one_after():
    clock = Clock()
    start = clock.t
    auth = facilitator.PayAIAuth(KEY_ID, facilitator.load_payai_private_key(SECRET), clock=clock)
    first = auth.token()
    iat = assert_payai_jwt(first, now=start)["iat"]
    # Reused while more than 30 s of the 120 s remain: exp > now + 30.
    for t in (start, iat + 1, iat + 60, iat + 89, iat + 89.999):
        clock.t = t
        assert auth.token() == first
    # At 30 s left it is re-signed, with the same clock for iat and exp.
    clock.t = iat + 90
    second = auth.token()
    assert second != first
    payload = assert_payai_jwt(second, now=clock.t)
    assert payload["iat"] == iat + 90
    assert payload["jti"] != _b64url_json(first.split(".")[1])["jti"]
    clock.t = iat + 100
    assert auth.token() == second


def test_concurrent_requests_share_one_token(payai):
    signed = []
    real_sign = facilitator.PayAIAuth._sign

    def counting(self, now):
        signed.append(now)
        return real_sign(self, now)

    payai.setattr(facilitator.PayAIAuth, "_sign", counting)
    create_headers = facilitator.get_facilitator_client().config["create_headers"]

    async def many():
        return await asyncio.gather(*(create_headers() for _ in range(50)))

    results = asyncio.run(many())
    tokens = {r["verify"]["Authorization"] for r in results}
    assert len(tokens) == 1 and len(signed) == 1


def test_auth_repr_shows_no_key_material():
    auth = facilitator.PayAIAuth(KEY_ID, facilitator.load_payai_private_key(SECRET))
    auth.token()
    assert_no_key_material(repr(auth) + str(auth))
    assert "secret" not in vars(auth) and SECRET not in repr(vars(auth))


# --- the hook ----------------------------------------------------------------

def test_hook_sends_the_jwt_on_verify_settle_supported_and_list(payai):
    config = facilitator.get_facilitator_client().config
    assert config["url"] == URL
    headers = asyncio.run(config["create_headers"]())
    assert set(headers) == {"verify", "settle", "supported", "list"}
    auth = headers["verify"]["Authorization"]
    assert auth.startswith("Bearer ")
    assert_payai_jwt(auth[len("Bearer "):])
    assert all(h == {"Authorization": auth} for h in headers.values())


class _Raw(httpx2.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _supported_answer():
    # A fresh raw stream each time: supported_response() reads it with aiter_raw.
    body = json.dumps({"kinds": [V2_EXACT]}).encode()
    return httpx2.Response(200, headers={"Content-Type": "application/json"}, stream=_Raw(body))


def _payment_and_requirements():
    from app.x402.models_v1 import PaymentPayload, PaymentRequirements
    from tests.test_x402_facilitator_differential import PAYLOAD, REQUIREMENTS
    return PaymentPayload(**PAYLOAD), PaymentRequirements(**REQUIREMENTS)


def _recording_client(config, seen, answer=None):
    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/supported"):
            return _supported_answer()
        return httpx2.Response(200, json=answer or {"isValid": False, "invalidReason": "x", "payer": None})
    return FacilitatorClient(config, transport=httpx2.MockTransport(handler))


def test_requests_to_the_facilitator_carry_a_valid_jwt(payai):
    seen = []
    client = _recording_client(facilitator.get_facilitator_client().config, seen)
    asyncio.run(client.supported())
    asyncio.run(client.supported_response())
    payment, requirements = _payment_and_requirements()
    asyncio.run(client.verify(payment, requirements))
    assert [p for _, p, _ in seen] == ["/x402/supported", "/x402/supported", "/x402/verify"]
    for _, _, auth in seen:
        assert_payai_jwt(auth[len("Bearer "):])


def test_startup_v2_check_sends_the_payai_header(payai):
    payai.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    seen = []
    shared = _recording_client(facilitator.get_facilitator_client().config, seen)
    payai.setattr(facilitator, "_client", shared)
    asyncio.run(facilitator.check_facilitator_v2_support())
    assert len(seen) == 1 and seen[0][1] == "/x402/supported"
    assert_payai_jwt(seen[0][2][len("Bearer "):])


def test_deploy_check_passes_with_payai_and_sends_the_header(payai, capsys):
    payai.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    seen = []
    payai.setattr(facilitator, "_client", _recording_client(facilitator.get_facilitator_client().config, seen))
    assert check_config.main() == check_config.EXIT_OK
    assert len(seen) == 1
    assert_payai_jwt(seen[0][2][len("Bearer "):])
    out = capsys.readouterr()
    assert_no_key_material(out.out + out.err)


# --- validate_x402_config ----------------------------------------------------

def test_payai_over_https_is_accepted(payai, caplog):
    caplog.set_level(logging.INFO, logger="app.x402.facilitator")
    facilitator.validate_x402_config()
    assert "(PayAI API key auth)" in caplog.text
    payai.setattr(settings, "X402_NETWORK", "base")
    facilitator.validate_x402_config()


def _refusal(match):
    with pytest.raises(RuntimeError, match=match) as exc:
        facilitator.validate_x402_config()
    msg = str(exc.value)
    assert msg.startswith(check_config.REFUSAL_PREFIX)
    assert_no_key_material(msg)
    return msg


def test_payai_and_bearer_together_are_refused(payai):
    payai.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", "t")
    msg = _refusal("only one of")
    assert "PayAI" in msg and "X402_FACILITATOR_BEARER_TOKEN" in msg


def test_payai_and_cdp_together_are_refused(payai):
    payai.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "cdp-id")
    payai.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "cdp-secret")
    payai.setattr(settings, "X402_FACILITATOR_URL", "")
    _refusal("only one of")


def test_cdp_and_bearer_message_is_unchanged_without_payai(x402):
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "cdp-id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "cdp-secret")
    x402.setattr(settings, "X402_FACILITATOR_URL", "")
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", "t")
    msg = _refusal("not both")
    assert "set either CDP credentials or X402_FACILITATOR_BEARER_TOKEN, not both" in msg
    assert "PayAI" not in msg


def test_payai_key_id_without_secret_is_refused(payai):
    payai.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_SECRET", "")
    _refusal("X402_FACILITATOR_PAYAI_API_KEY_SECRET is required with X402_FACILITATOR_PAYAI_API_KEY_ID")


def test_payai_secret_without_key_id_is_refused(payai):
    payai.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_ID", None)
    _refusal("X402_FACILITATOR_PAYAI_API_KEY_ID is required with X402_FACILITATOR_PAYAI_API_KEY_SECRET")


def _p256_secret():
    return base64.b64encode(_pkcs8_der(ec.generate_private_key(ec.SECP256R1()))).decode()


MALFORMED = {
    "not base64": "not base64 at all!!",
    "truncated": SECRET[:-8],
    "base64 but not DER": base64.b64encode(b"\x00" * 48).decode(),
    "raw seed, not PKCS#8": base64.b64encode(SEED).decode(),
    "a P-256 key": _p256_secret(),
}


@pytest.mark.parametrize("name", list(MALFORMED))
def test_a_malformed_payai_secret_is_refused_without_echoing_it(payai, name):
    bad = MALFORMED[name]
    payai.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_SECRET", bad)
    msg = _refusal("X402_FACILITATOR_PAYAI_API_KEY_SECRET is malformed")
    assert bad not in msg and bad[:16] not in msg


@pytest.mark.parametrize("name", list(MALFORMED))
def test_the_loader_error_carries_no_key_material(name):
    import traceback
    bad = MALFORMED[name]
    with pytest.raises(ValueError) as exc:
        facilitator.load_payai_private_key(bad)
    text = "".join(traceback.format_exception(exc.type, exc.value, exc.tb)) + repr(exc.value)
    assert "malformed" in str(exc.value)
    assert bad not in text and bad[:16] not in text
    # No chained exception that could carry the input, shown or not.
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None or exc.value.__suppress_context__


def test_plain_http_with_payai_is_refused(payai):
    payai.setattr(settings, "X402_FACILITATOR_URL", "http://facilitator.example.com")
    _refusal("https")


# --- the secret never leaks --------------------------------------------------

def test_the_secret_never_appears_in_logs_errors_or_settings(payai, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    payai.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    facilitator.validate_x402_config()
    seen = []
    payai.setattr(facilitator, "_client", _recording_client(facilitator.get_facilitator_client().config, seen))
    asyncio.run(facilitator.check_facilitator_v2_support())
    assert check_config.main() == check_config.EXIT_OK
    # A refused startup check (401) names the host, never the credential.
    payai.setattr(facilitator, "_client", FacilitatorClient(
        facilitator._config(), transport=httpx2.MockTransport(lambda r: httpx2.Response(401, text="no"))))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(facilitator.check_facilitator_v2_support())
    out = capsys.readouterr()
    text = caplog.text + out.out + out.err + str(exc.value) + repr(settings) + str(settings)
    text += repr(facilitator.get_facilitator_client().config)
    assert_no_key_material(text)


def test_deploy_check_refuses_a_malformed_secret_without_printing_it(tmp_path):
    bad = MALFORMED["a P-256 key"]
    code, out = run_check(tmp_path, {
        "X402_FACILITATOR_PAYAI_API_KEY_ID": KEY_ID, "X402_FACILITATOR_PAYAI_API_KEY_SECRET": bad})
    assert code == 3, out
    assert "X402_FACILITATOR_PAYAI_API_KEY_SECRET is malformed" in out
    assert bad not in out and bad[:16] not in out


def test_deploy_check_with_a_valid_payai_key_prints_no_key_material(tmp_path):
    code, out = run_check(tmp_path, {
        "X402_FACILITATOR_PAYAI_API_KEY_ID": KEY_ID, "X402_FACILITATOR_PAYAI_API_KEY_SECRET": SECRET})
    assert code == 0, out
    assert_no_key_material(out)


# --- without PayAI settings nothing changes ----------------------------------

def _dev_config(url, token=None):
    """What dev's _config() returns for these settings, written out literally."""
    config = {"url": url}
    if token:
        async def create_headers():
            auth = {"Authorization": f"Bearer {token}"}
            return {"verify": auth, "settle": auth, "supported": auth, "list": auth}
        config["create_headers"] = create_headers
    return config


def _captured(config):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), sorted(request.headers.multi_items()),
                     request.content))
        if request.url.path.endswith("/supported"):
            return _supported_answer()
        return httpx2.Response(200, json={"isValid": False, "invalidReason": "x", "payer": None})

    client = FacilitatorClient(config, transport=httpx2.MockTransport(handler))
    payment, requirements = _payment_and_requirements()
    asyncio.run(client.verify(payment, requirements))
    asyncio.run(client.supported())
    asyncio.run(client.supported_response())
    return seen


@pytest.mark.parametrize("token", [None, "a-bearer-token"])
def test_without_payai_requests_equal_devs(x402, token):
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", token)
    config = facilitator._config()
    if token is None:
        assert config == {"url": URL}
    else:
        assert set(config) == {"url", "create_headers"}
        assert asyncio.run(config["create_headers"]()) == asyncio.run(_dev_config(URL, token)["create_headers"]())
    assert _captured(config) == _captured(_dev_config(URL, token))


def test_without_payai_the_ok_log_line_is_unchanged(x402, caplog):
    caplog.set_level(logging.INFO, logger="app.x402.facilitator")
    facilitator.validate_x402_config()
    assert caplog.messages[-1] == f"x402 configuration OK: network=base-sepolia, facilitator={URL}"


def test_payai_settings_default_to_unset():
    from app.core.config import Settings
    s = Settings(SWARM_BEE_API_URL="http://localhost:1")
    assert not s.X402_FACILITATOR_PAYAI_API_KEY_ID
    assert not s.X402_FACILITATOR_PAYAI_API_KEY_SECRET


def test_settings_repr_hides_the_payai_secret():
    from app.core.config import Settings
    s = Settings(SWARM_BEE_API_URL="http://localhost:1", X402_FACILITATOR_PAYAI_API_KEY_ID=KEY_ID,
                 X402_FACILITATOR_PAYAI_API_KEY_SECRET=SECRET)
    assert s.X402_FACILITATOR_PAYAI_API_KEY_SECRET == SECRET
    assert_no_key_material(repr(s) + str(s))


# --- deploy.yml --------------------------------------------------------------

def _env_heredocs(text):
    return re.findall(r"cat > (/opt/swarm_connect(?:_dev)?\.env) << 'EOF'\n(.*?)\n\s*EOF\n", text, re.S)


def test_deploy_renders_the_payai_secrets_like_the_cdp_ones_in_both_jobs():
    docs = dict(_env_heredocs(DEPLOY_YML.read_text()))
    assert set(docs) == {"/opt/swarm_connect.env", "/opt/swarm_connect_dev.env"}
    for body in docs.values():
        lines = [line.strip() for line in body.splitlines()]
        assert "X402_FACILITATOR_CDP_API_KEY_SECRET=${{ secrets.X402_FACILITATOR_CDP_API_KEY_SECRET || '' }}" in lines
        for name in ("X402_FACILITATOR_PAYAI_API_KEY_ID", "X402_FACILITATOR_PAYAI_API_KEY_SECRET"):
            assert f"{name}=${{{{ secrets.{name} || '' }}}}" in lines


# --- review round 1 ----------------------------------------------------------

def _pkcs8_v2(key, public=None) -> bytes:
    """RFC 5958 OneAsymmetricKey (PKCS#8 v2) for an Ed25519 key, built by hand:
    version 1, the Ed25519 OID, the seed in an OCTET STRING, the public key [1]."""
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub = (public or key.public_key()).public_bytes(serialization.Encoding.Raw,
                                                    serialization.PublicFormat.Raw)
    body = (bytes.fromhex("020101" "300506032b6570" "04220420") + seed
            + bytes.fromhex("812100") + pub)
    return bytes([0x30, len(body)]) + body


def test_a_pkcs8_v2_secret_loads_and_signs_like_v1():
    v2 = base64.b64encode(_pkcs8_v2(KEY)).decode()
    key = facilitator.load_payai_private_key(v2)
    assert key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption()) == SEED
    clock = Clock()
    a = facilitator.PayAIAuth(KEY_ID, key, clock=clock)
    b = facilitator.PayAIAuth(KEY_ID, facilitator.load_payai_private_key(SECRET), clock=clock)
    # Ed25519 is deterministic: the same message gives the same signature.
    msg = b"payai"
    assert key.sign(msg) == facilitator.load_payai_private_key(SECRET).sign(msg)
    assert_payai_jwt(a.token(), now=clock.t)
    assert_payai_jwt(b.token(), now=clock.t)


def test_a_pkcs8_v2_secret_with_another_public_key_is_refused():
    other, _ = new_key()
    bad = base64.b64encode(_pkcs8_v2(KEY, public=other.public_key())).decode()
    with pytest.raises(ValueError, match="malformed") as exc:
        facilitator.load_payai_private_key(bad)
    assert_no_key_material(str(exc.value) + repr(exc.value))
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None or exc.value.__suppress_context__


def _v2_variants():
    der = _pkcs8_v2(KEY)
    return {
        "truncated": der[:-5],
        "trailing bytes": der + b"\x00",
        "version 2": der[:4] + b"\x02" + der[5:],
        "another OID": der.replace(bytes.fromhex("2b6570"), bytes.fromhex("2b6571")),
        "31-byte seed": bytes([0x30, der[1] - 1]) + der[2:12] + b"\x04\x21\x04\x1f" + der[16:47] + der[48:],
        "garbage": b"\x30\x51" + b"\xff" * 0x51,
        "outer length too long": b"\x30\x84\xff\xff\xff\xff" + der[2:],
    }


@pytest.mark.parametrize("name", list(_v2_variants()))
def test_a_malformed_pkcs8_v2_secret_is_refused_without_echo(name, payai):
    import traceback
    bad = base64.b64encode(_v2_variants()[name]).decode()
    with pytest.raises(ValueError, match="malformed") as exc:
        facilitator.load_payai_private_key(bad)
    text = "".join(traceback.format_exception(exc.type, exc.value, exc.tb))
    assert bad not in text and bad[:16] not in text
    assert_no_key_material(text)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None or exc.value.__suppress_context__
    payai.setattr(settings, "X402_FACILITATOR_PAYAI_API_KEY_SECRET", bad)
    msg = _refusal("X402_FACILITATOR_PAYAI_API_KEY_SECRET is malformed")
    assert bad[:16] not in msg


@pytest.mark.parametrize("bad", [
    SECRET + "=",                 # excess padding: atob refuses it
    SECRET.rstrip("=") + "===",
    "=" + SECRET,
    SECRET[:10] + "=" + SECRET[10:],
], ids=["one extra", "three", "leading", "inside"])
def test_padding_is_as_strict_as_atob(bad):
    with pytest.raises(ValueError, match="malformed"):
        facilitator.load_payai_private_key(bad)


def test_a_non_ascii_key_id_is_sent_as_utf8_like_json_stringify():
    kid = "cl\u00e9-\u5bc6\u94a5-\U0001f511"
    clock = Clock()
    auth = facilitator.PayAIAuth(kid, facilitator.load_payai_private_key(SECRET), clock=clock)
    token = auth.token()
    head = token.split(".")[0]
    assert "\\u" not in base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)).decode("utf-8")
    payload = assert_payai_jwt(token, key_id=kid, now=clock.t)
    assert payload["sub"] == kid
    assert jwt.get_unverified_header(token)["kid"] == kid
