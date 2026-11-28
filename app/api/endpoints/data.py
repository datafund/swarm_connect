# app/api/endpoints/data.py
import asyncio
import base64
import functools
import json
import logging
import re
import time
from urllib.parse import quote, unquote
from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, File, UploadFile
from fastapi.responses import Response
import httpx2

from app.x402.settlement import settle_payment
from app.x402.openapi_docs import paid_route_openapi
from app.api.models.data import (
    DataUploadRequest,
    DataUploadResponse,
    DataDownloadResponse,
    ManifestUploadResponse,
    UploadTiming,
    ManifestUploadTiming
)
from typing import Optional
from app.services.swarm_api import (
    upload_data_to_swarm,
    download_data_from_swarm,
    download_collection_file,
    CollectionPathNotFound,
    DownloadTooLargeError,
    get_batch_expiry,
    upload_collection_to_swarm,
    inspect_tar,
    TarRefused,
    TarSummary,
    validate_stamp_for_upload,
    check_upload_failure_reason,
    StampValidationError,
    REDUNDANCY_LEVELS
)
from app.core.config import settings
from app.core.io_lane import IOLane
from app.services.http_client import exc_text
from app.services.stamp_ownership import stamp_ownership_manager
from app.services.signed_auth import (
    OwnerProofError,
    OwnerProofReservation,
    confirm_owner_proof,
    release_owner_proof,
    reserve_owner_proof,
    verify_owner_proof,
)
from app.services.spend_budget import spend_certainly_did_not_happen
from app.services.provenance import (
    get_provenance_service,
    DocumentValidationError,
    NotaryNotEnabledError
)
from app.services.signing import NotConfiguredError
from app.services.metrics import (
    uploads_total, upload_bytes_total, downloads_total, notary_signatures_total,
)

logger = logging.getLogger(__name__)

# Manifest TAR parsing runs on its own two threads (#694), not the default
# executor: a burst of uploads then waits its turn instead of taking every
# worker thread, and at most two parses compete with the event loop for the GIL.
_tar_lane = IOLane(2, "manifest-tar-parse")

# Slack allowed on Content-Length to cover the multipart envelope that wraps an
# uploaded file. 8 KB is far more than a boundary plus part headers need, and
# far less than any size that would matter for the limit itself.
MULTIPART_ENVELOPE_ALLOWANCE = 8 * 1024

router = APIRouter()


def _build_server_timing_header(timing_dict: dict) -> str:
    """
    Build W3C Server-Timing header value from timing dictionary.

    Format: metric;dur=value, metric2;dur=value2
    Example: file-read;dur=0.5, bee-upload;dur=123.4, total;dur=124.1
    """
    parts = []
    for key, value in timing_dict.items():
        if value is not None:
            # Convert snake_case to kebab-case for header
            metric_name = key.replace("_", "-")
            parts.append(f"{metric_name};dur={value:.2f}")
    return ", ".join(parts)


SNIFF_FULL_PARSE_BYTES = 1024 * 1024


def _is_utf8_prefix(data: bytes, size: int = 64 * 1024) -> bool:
    """Whether the first `size` bytes decode as UTF-8 (a cut multi-byte char at the end is fine)."""
    import codecs
    try:
        codecs.getincrementaldecoder("utf-8")().decode(data[:size], final=len(data) <= size)
        return True
    except UnicodeDecodeError:
        return False


def _too_large(reference: str, e: Exception) -> HTTPException:
    downloads_total.labels(status="too_large").inc()
    logger.warning(f"Download of {reference} refused: {exc_text(e)}")
    return HTTPException(
        status_code=413,
        detail={
            "code": "DOWNLOAD_TOO_LARGE",
            "message": (f"This content is larger than the gateway's download limit of "
                        f"{settings.MAX_DOWNLOAD_SIZE_MB} MB. Fetch it from a Swarm node directly."),
            "max_size_mb": settings.MAX_DOWNLOAD_SIZE_MB,
        },
    )


def _download_timeout(reference: str) -> HTTPException:
    downloads_total.labels(status="error").inc()
    logger.warning(f"Download of {reference} exceeded {settings.DOWNLOAD_TIMEOUT_SECONDS}s")
    return HTTPException(status_code=504, detail="Fetching the content from the Swarm network took too long.")


def _detect_content_type_and_filename(data_bytes: bytes, reference: str) -> tuple[str, str]:
    """
    Detect content type and generate user-friendly filename for downloads.

    Args:
        data_bytes: The downloaded data
        reference: Swarm reference hash

    Returns:
        Tuple of (content_type, filename)
    """
    # Try to detect if it's JSON. A full parse only for small bodies: parsing a
    # large JSON document of many small values costs many times its size in
    # memory and blocks the event loop; beyond that, the leading bracket decides.
    if len(data_bytes) <= SNIFF_FULL_PARSE_BYTES:
        try:
            json.loads(data_bytes.decode('utf-8'))
            # It's valid JSON - use JSON content type and provenance filename
            short_ref = reference[:8]  # First 8 chars of reference for filename
            return "application/json", f"provenance-{short_ref}.json"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    elif data_bytes[:1024].lstrip()[:1] in (b"{", b"[") and _is_utf8_prefix(data_bytes):
        return "application/json", f"provenance-{reference[:8]}.json"

    # Check for common binary file signatures
    if data_bytes.startswith(b'\x89PNG'):
        return "image/png", f"image-{reference[:8]}.png"
    elif data_bytes.startswith(b'\xFF\xD8\xFF'):
        return "image/jpeg", f"image-{reference[:8]}.jpg"
    elif data_bytes.startswith(b'%PDF'):
        return "application/pdf", f"document-{reference[:8]}.pdf"
    elif data_bytes.startswith(b'GIF8'):
        return "image/gif", f"image-{reference[:8]}.gif"

    # Check if it's likely text
    if _is_utf8_prefix(data_bytes):
        return "text/plain", f"text-{reference[:8]}.txt"

    # Default to binary with .bin extension
    return "application/octet-stream", f"data-{reference[:8]}.bin"


# The 404 of /data/{reference} and /data/{reference}/json (#700). Bee answers a
# collection uploaded without an index document exactly as it answers a
# reference that does not exist, so the hint names both and claims neither.
# Kept a plain string with its old prefix, as clients read it.
def _not_found_detail(reference: str) -> str:
    return (f"Data not found for reference {reference}. If it is a collection uploaded "
            f"without an index document, download a file inside it with "
            f"GET /api/v1/data/{reference}/<path>.")


# Collection paths (#700). Starlette has already percent-decoded the URL path
# once; a segment that decodes once more to "." or ".." (sent as %252e%252e) is
# refused too, as are control characters, backslashes and empty segments.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _path_segments(path: str) -> list[str]:
    """The segments of a collection path, or ValueError naming what is wrong."""
    if not path:
        raise ValueError("The path is empty.")
    if len(path.encode("utf-8", "surrogateescape")) > settings.MANIFEST_MAX_PATH_BYTES:
        raise ValueError(f"The path is longer than {settings.MANIFEST_MAX_PATH_BYTES} bytes.")
    segments = path.split("/")
    for segment in segments:
        if segment == "":
            raise ValueError("The path has an empty segment (a leading, doubled or extra slash).")
        for form in (segment, unquote(segment)):
            if form in (".", ".."):
                raise ValueError("The path may not contain '.' or '..' segments.")
            if "\\" in form:
                raise ValueError("The path may not contain backslashes.")
            if _CONTROL_CHARS.search(form):
                raise ValueError("The path may not contain control characters.")
        if "/" in unquote(segment):
            raise ValueError("The path may not contain an encoded slash.")
    return segments


# A Content-Type Bee sends is passed through only when it is a plain
# type/subtype with optional parameters; anything else becomes octet-stream.
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(\s*;[\x20-\x7e]*)?$")
_BEE_FILENAME = re.compile(r'filename="([^"]*)"')


def _collection_content_type(bee_value: Optional[str]) -> str:
    value = (bee_value or "").strip()
    return value if _MEDIA_TYPE.match(value) else "application/octet-stream"


def _collection_filename(bee_disposition: Optional[str], segments: list[str]) -> str:
    """Bee's own file name (it names the index document a directory request
    served), else the last path segment."""
    match = _BEE_FILENAME.search(bee_disposition or "")
    if match:
        name = match.group(1)
        try:
            # httpx2 decodes a header it cannot read as ASCII or UTF-8 as
            # latin-1; Bee sends the name as UTF-8.
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        if name and "/" not in name and not _CONTROL_CHARS.search(name):
            return name
    return segments[-1]


def _attachment(filename: str) -> str:
    """Content-Disposition: attachment, with an ASCII fallback name and the
    exact name as RFC 6266 filename* when they differ."""
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in filename)
    value = f'attachment; filename="{fallback}"'
    if fallback != filename:
        value += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return value


# Set as the collection's index document when the archive has it at its root.
ROOT_INDEX_DOCUMENT = "index.html"


def _document_path(value: str, single_segment: bool = False) -> list[str]:
    """Check an index_document / error_document name. It goes to Bee in a
    header, so it must be printable ASCII; otherwise the rules of a download
    path, without a trailing slash.

    Bee appends the index document to every directory it serves, and refuses
    an upload whose index document has a slash (Bee 2.8.1 answers 500 "could
    not store directory", measured), so an index document is one name."""
    if not value.isascii():
        raise ValueError("the path must be plain ASCII.")
    if value.endswith("/"):
        raise ValueError("the path must name a file, not a directory.")
    segments = _path_segments(value)
    if single_segment and len(segments) > 1:
        raise ValueError("the index document must be a file at the root of the archive, without a '/'.")
    return segments


def _path_invalid(message: str) -> HTTPException:
    downloads_total.labels(status="error").inc()
    return HTTPException(status_code=400, detail={"code": "PATH_INVALID", "message": message})


def _check_stamp_access(
    request: Request,
    stamp_id: str,
    owner_timestamp: Optional[str],
    owner_signature: Optional[str],
) -> Optional[OwnerProofReservation]:
    """Raise unless the caller may upload to stamp_id.

    The caller is identified by the x402 payer on a paid request, or by a
    signed owner proof (#384). The proof is only examined when the payer alone
    is not enough, so a proof is not touched by a request that did not need it.

    Returns the reservation of the owner proof that granted access, or None.
    The caller settles it through _upload_to_bee and releases it in a finally
    block (#470).
    """
    x402_payer = getattr(request.state, 'x402_payer', None)
    x402_mode = getattr(request.state, 'x402_mode', None)
    reservation = None
    allowed, reason = stamp_ownership_manager.check_access(stamp_id, x402_payer, x402_mode)
    if not allowed and (owner_timestamp or owner_signature):
        try:
            proof = verify_owner_proof(stamp_id, owner_timestamp, owner_signature)
            allowed, reason = stamp_ownership_manager.check_access(stamp_id, proof.signer, x402_mode)
            # Reserved only when it grants access, so non-owners' proofs do not
            # fill the set. Spent only once Bee has stored the upload.
            if allowed:
                reservation = reserve_owner_proof(proof)
        except OwnerProofError as e:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "OWNER_PROOF_INVALID",
                    "message": f"Owner proof rejected: {e}",
                    "stamp_id": stamp_id
                }
            )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "STAMP_OWNERSHIP_DENIED",
                "message": f"Access denied: {reason}",
                "stamp_id": stamp_id
            }
        )
    return reservation


async def _upload_to_bee(reservation: Optional[OwnerProofReservation], upload):
    """Await the Bee upload and settle the owner proof that granted it (#470).

    The proof is spent once Bee returns a reference. If the upload fails, the
    proof is given back only when Bee certainly stored nothing (it could not be
    reached, or it answered 4xx). A timeout, a 5xx or a cancellation may have
    stored the data, so the proof stays spent.
    """
    try:
        reference = await upload
    except BaseException as exc:
        if spend_certainly_did_not_happen(exc):
            release_owner_proof(reservation)
        else:
            confirm_owner_proof(reservation)
        raise
    confirm_owner_proof(reservation)
    return reference


_OWNER_TS_DESC = "Owner proof: unix seconds, signed in X-Owner-Signature. See 'Owned stamps' above."
_OWNER_SIG_DESC = (
    "Owner proof: EIP-191 personal_sign of "
    "'swarm-connect-owner-upload:<stamp_id lowercase>:<X-Owner-Timestamp>'."
)


@router.post("/", response_model=DataUploadResponse, openapi_extra=paid_route_openapi([200]))
async def upload_data(
    request: Request,
    stamp_id: str = Query(..., pattern=r"^[a-fA-F0-9]{64}$"),
    content_type: str = "application/json",
    validate_stamp: bool = False,
    deferred: bool = False,
    include_timing: bool = False,
    redundancy: Optional[int] = Query(default=None, ge=0, le=4),
    sign: Optional[str] = None,
    file: UploadFile = File(...),
    x_owner_timestamp: Optional[str] = Header(None, alias="X-Owner-Timestamp", description=_OWNER_TS_DESC),
    x_owner_signature: Optional[str] = Header(None, alias="X-Owner-Signature", description=_OWNER_SIG_DESC),
):
    """
    Upload data to the Swarm network via the configured Bee node.

    **x402 Payment** (when gateway has x402 enabled):
    This endpoint requires payment OR free tier access. Check `GET /health` for availability.
    - **Free tier**: Add header `X-Payment-Mode: free` (rate limited)
    - **Paid**: Include x402 payment header (higher rate limit)
    - Without either header, returns **HTTP 402** with payment instructions and free tier info

    **Owned stamps** (x402 enabled): a stamp bought with a payment belongs to the
    paying wallet, and only that wallet may upload to it. Prove it is you either
    by paying for the upload from that wallet, or for free (`X-Payment-Mode: free`,
    free-tier rate limit applies) with an owner proof:
    - `X-Owner-Timestamp`: current unix time in seconds (valid for 5 minutes)
    - `X-Owner-Signature`: EIP-191 `personal_sign` by the owner wallet of
      `swarm-connect-owner-upload:<stamp_id in lowercase>:<timestamp>`
    - Each proof buys one upload; sign a new one per upload. It is spent only
      when Bee stores the upload. If the request fails before that, you can
      retry with the same proof while it is fresh. It is a bearer credential
      for one upload (not bound to the file): do not log or share it.
      A rejected proof
      returns **401** `OWNER_PROOF_INVALID`; a valid proof from another wallet
      returns **403** `STAMP_OWNERSHIP_DENIED`.
    - Downloads (`GET /api/v1/data/{reference}`) are always free — no headers needed

    **Requirements**:
    - Upload as **multipart/form-data** with a `file` field
    - Valid `stamp_id` query parameter required
    - Optional `content_type` parameter (defaults to application/json)
    - Optional `validate_stamp` parameter (defaults to false)
    - Optional `deferred` parameter (defaults to false)
    - Optional `include_timing` parameter (defaults to false)
    - Optional `redundancy` parameter (0-4, defaults to 2=strong)
    - Optional `sign` parameter ("notary" to add gateway notary signature)

    **Stamp Validation** (opt-in with `validate_stamp=true`):
    When enabled, validates the stamp before upload:
    - Returns 400 if stamp is at 100% utilization (full)
    - Returns 400 if stamp is not usable (expired, invalid)
    - Returns 404 if stamp is not found

    **Redundancy Levels** (erasure coding for data durability):
    - `0` = None: No erasure coding (0% chunk loss tolerance)
    - `1` = Medium: 1% chunk loss tolerance
    - `2` = Strong: 5% chunk loss tolerance (default)
    - `3` = Insane: 10% chunk loss tolerance
    - `4` = Paranoid: 50% chunk loss tolerance

    Higher redundancy levels increase storage cost but improve data durability.
    See: https://docs.ethswarm.org/docs/concepts/DISC/erasure-coding/

    **Deferred Mode** (opt-in with `deferred=true`):
    - `deferred=false` (default): Direct upload - chunks uploaded directly to network,
      ensuring immediate availability. Safer for gateway use cases.
    - `deferred=true`: Deferred upload - data goes to local node first, then syncs to
      network asynchronously. Faster upload response but data may not be immediately
      retrievable from the network.

    **Performance Timing** (opt-in with `include_timing=true`):
    When enabled, includes timing breakdown in response and Server-Timing header:
    - `stamp_validate_ms`: Time validating stamp (only if validate_stamp=true)
    - `file_read_ms`: Time reading uploaded file
    - `bee_upload_ms`: Time uploading to Bee node
    - `total_ms`: Total request processing time

    **Notary Signing** (opt-in with `sign=notary`):
    When enabled, the gateway adds a cryptographic signature to prove data existed at upload time.
    - Requires `NOTARY_ENABLED=true` and `NOTARY_PRIVATE_KEY` to be configured
    - Document must be JSON with a `data` field: `{"data": {...}, "signatures": [...]}`
    - Gateway adds a notary signature to the signatures array
    - Check `/api/v1/notary/info` for notary availability and public address
    - Returns 400 if document format is invalid or notary is not configured

    **Usage Examples**:
    ```bash
    # Upload with free tier (when x402 is enabled)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123" \\
         -H "X-Payment-Mode: free" \\
         -F "file=@data.json"

    # Upload JSON file (direct mode, default)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&content_type=application/json" \\
         -F "file=@data.json"

    # Upload with deferred mode (faster response)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&deferred=true" \\
         -F "file=@data.json"

    # Upload with pre-validation
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&validate_stamp=true" \\
         -F "file=@data.json"

    # Upload with timing information
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&include_timing=true" \\
         -F "file=@data.json"

    # Upload binary file
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&content_type=image/png" \\
         -F "file=@image.png"

    # Upload with notary signing (requires NOTARY_ENABLED=true)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&sign=notary" \\
         -F "file=@provenance-doc.json"
    ```

    **Supported Content Types**: JSON, text, images, PDFs, or any binary data.

    **SWIP-Compliant JSON Example**:
    ```json
    {
        "content_hash": "sha256:9f86d...",
        "provenance_standard": "DaTA v1.0.0",
        "encryption": "none",
        "data": { ... provenance data ... },
        "stamp_id": "0xfe2f..."
    }
    ```
    """
    # The ownership registry is keyed in lowercase; the query accepts either
    # case (#473).
    stamp_id = stamp_id.lower()
    start_time = time.perf_counter()
    stamp_validate_ms = None
    file_read_ms = None
    bee_upload_ms = None
    # The owner proof that granted access, if any. Released in the finally
    # block unless the Bee upload settled it, so a request refused or failed
    # before Bee does not spend the proof (#470).
    owner_proof = None

    try:
        # Reject an over-sized body before anything else.
        #
        # This ran after the stamp ownership check, so an over-sized upload with
        # an unrecognised stamp answered 403 STAMP_OWNERSHIP_DENIED and the
        # documented 413 was unreachable — the caller was told the wrong thing
        # about their request. Size does not depend on who is asking, and the
        # check is a header comparison against a registry lookup, so it belongs
        # first on both counts.
        #
        # Content-Length covers the whole multipart envelope — boundary, part
        # headers, trailer — not just the file, so it is compared against the
        # limit plus an allowance for that wrapper. Without it a file of exactly
        # MAX_UPLOAD_SIZE_MB was always rejected, putting the real ceiling a few
        # hundred bytes below the documented one and making it shrink as the
        # filename grew.
        #
        # This cannot reject early despite reading like it should: FastAPI parses
        # the multipart form during dependency resolution, so the body is already
        # in memory by the time this line runs. It is a coarse guard, not a fast
        # path. The exact limit is enforced on the file's own length further down.
        max_size = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_size + MULTIPART_ENVELOPE_ALLOWANCE:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": f"Upload exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB} MB.",
                    "max_size_mb": settings.MAX_UPLOAD_SIZE_MB
                }
            )

        # Check stamp ownership
        if settings.X402_ENABLED:
            owner_proof = _check_stamp_access(request, stamp_id, x_owner_timestamp, x_owner_signature)

        # Validate redundancy level if provided
        if redundancy is not None and redundancy not in REDUNDANCY_LEVELS:
            valid_levels = ", ".join(f"{k}={v}" for k, v in REDUNDANCY_LEVELS.items())
            raise HTTPException(
                status_code=400,
                detail=f"Invalid redundancy level {redundancy}. Must be 0-4 ({valid_levels})"
            )

        # Optional pre-upload stamp validation. Always done for a paid upload:
        # the payment is settled just before the upload, so a stamp Bee would
        # refuse (missing, expired, full) must be caught while it is still free.
        if validate_stamp or getattr(request.state, "x402_mode", None) == "paid":
            stamp_start = time.perf_counter()
            try:
                await validate_stamp_for_upload(stamp_id, local_only=not validate_stamp)
            except StampValidationError as e:
                # Build structured error response
                detail = {
                    "code": e.code,
                    "message": e.message,
                    "suggestion": e.suggestion,
                    "stamp_id": stamp_id
                }
                if e.stamp_data:
                    detail["stamp_status"] = {
                        "exists": True,
                        "local": e.stamp_data.get("local", False),
                        "usable": e.stamp_data.get("usable"),
                        "utilizationPercent": e.stamp_data.get("utilizationPercent"),
                        "utilizationStatus": e.stamp_data.get("utilizationStatus"),
                        "batchTTL": e.stamp_data.get("batchTTL"),
                        "expectedExpiration": e.stamp_data.get("expectedExpiration")
                    }
                if e.status == "not_found":
                    raise HTTPException(status_code=404, detail=detail)
                else:
                    raise HTTPException(status_code=400, detail=detail)
            stamp_validate_ms = (time.perf_counter() - stamp_start) * 1000


        # Read file content as bytes
        file_start = time.perf_counter()
        data_bytes = await file.read()
        file_read_ms = (time.perf_counter() - file_start) * 1000

        if len(data_bytes) > max_size:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": f"Upload exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB} MB.",
                    "max_size_mb": settings.MAX_UPLOAD_SIZE_MB
                }
            )

        # Optional notary signing
        notarized = False
        if sign == "notary":
            try:
                provenance_service = get_provenance_service()
                signed_doc = provenance_service.sign_document(data_bytes)
                data_bytes = signed_doc.raw_json.encode('utf-8')
                content_type = "application/json"  # Signed documents are always JSON
                logger.info(f"Document signed by notary, signatures count: {len(signed_doc.signatures)}")
                # Counted once the signed document is stored, below: a success
                # counted here, before the upload, let the Notarized Share KPI
                # exceed 100% whenever a signed upload then failed (#518).
                notarized = True
            except NotaryNotEnabledError as e:
                notary_signatures_total.labels(status="error").inc()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "NOTARY_NOT_ENABLED",
                        "message": str(e),
                        "suggestion": "Set NOTARY_ENABLED=true to enable notary signing"
                    }
                )
            except NotConfiguredError as e:
                notary_signatures_total.labels(status="error").inc()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "NOTARY_NOT_CONFIGURED",
                        "message": str(e),
                        "suggestion": "Set NOTARY_PRIVATE_KEY to configure the notary signing key"
                    }
                )
            except DocumentValidationError as e:
                notary_signatures_total.labels(status="error").inc()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INVALID_DOCUMENT_FORMAT",
                        "message": str(e),
                        "suggestion": "Document must be JSON with a 'data' field: {\"data\": {...}, \"signatures\": [...]}"
                    }
                )
            except Exception:
                # Signing failed unexpectedly (e.g. SigningServiceError from
                # the key). Count it as a notary error here, then let the outer
                # handler answer and count the upload error once.
                notary_signatures_total.labels(status="error").inc()
                raise
        elif sign is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_SIGN_OPTION",
                    "message": f"Invalid sign option: {sign}",
                    "suggestion": "Use sign=notary for gateway notary signing"
                }
            )

        # Upload to Swarm
        bee_start = time.perf_counter()
        await settle_payment(request)
        reference = await _upload_to_bee(owner_proof, upload_data_to_swarm(
            data=data_bytes,
            stamp_id=stamp_id,
            content_type=content_type,
            deferred=deferred,
            redundancy_level=redundancy
        ))
        bee_upload_ms = (time.perf_counter() - bee_start) * 1000

        total_ms = (time.perf_counter() - start_time) * 1000

        # Build response
        timing = None
        if include_timing:
            timing = UploadTiming(
                stamp_validate_ms=stamp_validate_ms,
                file_read_ms=file_read_ms,
                bee_upload_ms=bee_upload_ms,
                total_ms=total_ms
            )

        uploads_total.labels(status="success").inc()
        upload_bytes_total.inc(len(data_bytes))
        if notarized:
            notary_signatures_total.labels(status="success").inc()

        response = DataUploadResponse(
            reference=reference,
            message=f"File '{file.filename}' uploaded successfully",
            timing=timing,
            expires_at=await get_batch_expiry(stamp_id),
        )

        # Always add Server-Timing header (useful for browser devtools)
        timing_dict = {
            "stamp_validate_ms": stamp_validate_ms,
            "file_read_ms": file_read_ms,
            "bee_upload_ms": bee_upload_ms,
            "total_ms": total_ms
        }
        server_timing = _build_server_timing_header(timing_dict)

        return Response(
            content=response.model_dump_json(),
            media_type="application/json",
            headers={"Server-Timing": server_timing}
        )

    except HTTPException:
        uploads_total.labels(status="error").inc()
        raise  # Re-raise HTTP exceptions as-is
    except httpx2.HTTPError as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Swarm API error during upload: {exc_text(e)}")
        # Check if the failure was due to stamp issues
        failure_info = await check_upload_failure_reason(stamp_id, str(e))
        if failure_info:
            # Return structured error response
            detail = {
                "code": failure_info.get("code"),
                "message": failure_info.get("message"),
                "suggestion": failure_info.get("suggestion"),
                "stamp_id": stamp_id,
                "stamp_status": failure_info.get("stamp_status")
            }
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=502, detail="Failed to upload data to Swarm. The Bee node may be unavailable.")
    except ValueError as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Data processing error during upload: {exc_text(e)}")
        raise HTTPException(status_code=400, detail="Data upload error. Please check your request and try again.")
    except Exception as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Unexpected error during upload: {exc_text(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during upload")
    finally:
        release_owner_proof(owner_proof)


@router.get("/{reference}")
async def download_data(
    reference: str = Path(..., description="Swarm reference hash of the data to download", pattern=r"^[a-fA-F0-9]{64,128}$")
):
    """
    Download data from the Swarm network as a file (triggers browser download).

    **Always free** — no payment or headers required, even when x402 is enabled.

    **Use case**: End users downloading files, browser integration

    **Features**:
    - User-friendly filenames (provenance-abc12345.json, image-def67890.png, etc.)
    - Auto-detected content types (application/json, image/png, text/plain, etc.)
    - Proper download headers for browsers
    - Direct binary streaming (no JSON wrapper)
    """
    try:
        # Download from Swarm
        data_bytes = await download_data_from_swarm(reference)

        # Detect content type and generate user-friendly filename
        content_type, filename = _detect_content_type_and_filename(data_bytes, reference)

        downloads_total.labels(status="success").inc()

        # Return raw data with user-friendly filename
        return Response(
            content=data_bytes,
            media_type=content_type,
            headers={
                "Content-Length": str(len(data_bytes)),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Swarm-Reference": reference
            }
        )

    except FileNotFoundError as e:
        downloads_total.labels(status="error").inc()
        logger.warning(f"Data not found for reference {reference}: {exc_text(e)}")
        raise HTTPException(status_code=404, detail=_not_found_detail(reference))
    except DownloadTooLargeError as e:
        raise _too_large(reference, e)
    except asyncio.TimeoutError:
        raise _download_timeout(reference)
    except httpx2.HTTPError as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Swarm API error during download: {exc_text(e)}")
        raise HTTPException(status_code=502, detail="Failed to download data from Swarm. The Bee node may be unavailable.")
    except Exception as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Unexpected error during download: {exc_text(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during download")



@router.get("/{reference}/json", response_model=DataDownloadResponse)
async def download_data_json(
    reference: str = Path(..., description="Swarm reference hash of the data to download", pattern=r"^[a-fA-F0-9]{64,128}$")
):
    """
    Download data from the Swarm network as JSON with metadata (for API clients).

    **Always free** — no payment or headers required, even when x402 is enabled.

    **Use case**: Web apps, mobile apps, API integrations needing metadata

    **Response format**:
    ```json
    {
        "data": "base64-encoded-content",
        "content_type": "application/json",
        "size": 2048,
        "reference": "abc123..."
    }
    ```

    **Benefits**:
    - Get file metadata without triggering browser download
    - Programmatic access to file info and content
    - Base64 encoding for JSON-safe binary data transport
    """
    try:
        # Download from Swarm
        data_bytes = await download_data_from_swarm(reference)

        # Detect content type and generate filename for metadata
        content_type, filename = _detect_content_type_and_filename(data_bytes, reference)

        # Encode as base64
        data_b64 = base64.b64encode(data_bytes).decode('utf-8')

        downloads_total.labels(status="success").inc()

        return DataDownloadResponse(
            data=data_b64,
            content_type=content_type,
            size=len(data_bytes),
            reference=reference
        )

    except FileNotFoundError as e:
        downloads_total.labels(status="error").inc()
        logger.warning(f"Data not found for reference {reference}: {exc_text(e)}")
        raise HTTPException(status_code=404, detail=_not_found_detail(reference))
    except DownloadTooLargeError as e:
        raise _too_large(reference, e)
    except asyncio.TimeoutError:
        raise _download_timeout(reference)
    except httpx2.HTTPError as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Swarm API error during download: {exc_text(e)}")
        raise HTTPException(status_code=502, detail="Failed to download data from Swarm. The Bee node may be unavailable.")
    except Exception as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Unexpected error during download: {exc_text(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during download")


@router.get("/{reference}/{path:path}")
async def download_collection_path(
    reference: str = Path(..., description="Swarm reference of the collection (manifest)", pattern=r"^[a-fA-F0-9]{64,128}$"),
    path: str = Path(..., description="Path of a file inside the collection, as it was in the uploaded TAR archive"),
):
    """
    Download one file from a collection uploaded with `POST /api/v1/data/manifest`.

    **Always free** — no payment or headers required, even when x402 is enabled.

    The gateway fetches `bzz/{reference}/{path}` from its Bee node, with the same
    size limit (`MAX_DOWNLOAD_SIZE_MB`, 413 `DOWNLOAD_TOO_LARGE`) and timeout (504)
    as `GET /api/v1/data/{reference}`.

    **Path**: the file's path in the archive, for example `assets/logo.png`
    (`./assets/logo.png` in the archive is stored as `assets/logo.png`). Each
    segment is percent-encoded towards Bee, so send a name with spaces or `#`
    percent-encoded in the URL. Refused with 400 `PATH_INVALID`: `.` and `..`
    segments (also percent-encoded once more), empty segments (`a//b`, a leading
    slash), backslashes, control characters, an encoded slash, and paths over
    `MANIFEST_MAX_PATH_BYTES`. A trailing slash asks for that directory's index
    document (`docs/` serves `docs/index.html` when the collection has the index
    document `index.html`). `/api/v1/data/{reference}/` is the same as
    `/api/v1/data/{reference}`.

    **`json` at the collection root**: `/api/v1/data/{reference}/json` is the
    JSON-metadata download, so a file named exactly `json` at the root of a
    collection cannot be fetched through this route. Files called `json` in a
    directory (`a/json`) are not affected.

    **Response**: the file's bytes, always as a download:
    - `Content-Type`: the type Bee reports, or `application/octet-stream` when Bee
      reports none
    - `Content-Disposition: attachment` with the file's name
    - `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`, so
      an HTML or SVG file is never run as a page of the gateway
    - `X-Swarm-Reference`

    **Errors**:
    - 404 `COLLECTION_PATH_NOT_FOUND`: the collection exists and has no file at
      this path. A collection uploaded with an error document serves that
      document instead, with 200.
    - 404 `DATA_NOT_FOUND`: Bee has nothing for this reference and path. The
      reference may not exist, or may not be a collection.
    - 422: the reference is not 64 or 128 hex characters.
    """
    if path == "":
        # /data/{reference}/ used to redirect to /data/{reference}; keep its answer.
        return await download_data(reference)
    directory = path.endswith("/")
    try:
        segments = _path_segments(path[:-1] if directory else path)
    except ValueError as e:
        raise _path_invalid(str(e))

    try:
        result = await download_collection_file(reference, segments, directory=directory)
    except CollectionPathNotFound as e:
        downloads_total.labels(status="error").inc()
        logger.warning(f"No path in collection {reference}: {exc_text(e)}")
        raise HTTPException(status_code=404, detail={
            "code": "COLLECTION_PATH_NOT_FOUND",
            "message": "The collection has no file at this path.",
            "reference": reference,
            "path": path,
        })
    except FileNotFoundError as e:
        downloads_total.labels(status="error").inc()
        logger.warning(f"Nothing at {reference}/<path>: {exc_text(e)}")
        raise HTTPException(status_code=404, detail={
            "code": "DATA_NOT_FOUND",
            "message": ("Nothing was found for this reference and path. The reference may "
                        "not exist, or may not be a collection."),
            "reference": reference,
            "path": path,
        })
    except DownloadTooLargeError as e:
        raise _too_large(reference, e)
    except asyncio.TimeoutError:
        raise _download_timeout(reference)
    except httpx2.HTTPError as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Swarm API error during collection download: {exc_text(e)}")
        raise HTTPException(status_code=502, detail="Failed to download data from Swarm. The Bee node may be unavailable.")
    except Exception as e:
        downloads_total.labels(status="error").inc()
        logger.error(f"Unexpected error during collection download: {exc_text(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during download")

    downloads_total.labels(status="success").inc()
    filename = _collection_filename(result.content_disposition, segments)
    return Response(
        content=result.content,
        headers={
            "Content-Type": _collection_content_type(result.content_type),
            "Content-Length": str(len(result.content)),
            "Content-Disposition": _attachment(filename),
            # Served from the gateway's own origin: never sniffed into an active
            # type, never run as a page (#700).
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "X-Swarm-Reference": reference,
        },
    )


@router.post("/manifest", response_model=ManifestUploadResponse, openapi_extra=paid_route_openapi([200]))
async def upload_manifest(
    request: Request,
    stamp_id: str = Query(..., pattern=r"^[a-fA-F0-9]{64}$"),
    validate_stamp: bool = False,
    deferred: bool = False,
    include_timing: bool = False,
    redundancy: Optional[int] = Query(default=None, ge=0, le=4),
    index_document: Optional[str] = Query(
        default=None,
        description="Name of a regular file at the archive's root that Bee serves for the collection "
                    "root (and that name in every directory). Default: index.html when the archive has one at its root.",
    ),
    error_document: Optional[str] = Query(
        default=None,
        description="Path of a regular file in the archive that Bee serves, with status 200, "
                    "for a path the collection does not have.",
    ),
    file: UploadFile = File(...),
    x_owner_timestamp: Optional[str] = Header(None, alias="X-Owner-Timestamp", description=_OWNER_TS_DESC),
    x_owner_signature: Optional[str] = Header(None, alias="X-Owner-Signature", description=_OWNER_SIG_DESC),
):
    """
    Upload a TAR archive as a collection/manifest to the Swarm network.

    This endpoint bundles multiple files in a single upload for improved performance.
    The TAR archive is uploaded with the `Swarm-Collection: true` header, creating
    a manifest that maps file paths to their individual Swarm references.

    **x402 Payment** (when gateway has x402 enabled):
    This endpoint requires payment OR free tier access. Check `GET /health` for availability.
    - **Free tier**: Add header `X-Payment-Mode: free` (rate limited)
    - **Paid**: Include x402 payment header (higher rate limit)
    - Without either header, returns **HTTP 402** with payment instructions and free tier info

    **Owned stamps** (x402 enabled): a stamp bought with a payment belongs to the
    paying wallet, and only that wallet may upload to it. Prove it is you either
    by paying for the upload from that wallet, or for free (`X-Payment-Mode: free`,
    free-tier rate limit applies) with an owner proof:
    - `X-Owner-Timestamp`: current unix time in seconds (valid for 5 minutes)
    - `X-Owner-Signature`: EIP-191 `personal_sign` by the owner wallet of
      `swarm-connect-owner-upload:<stamp_id in lowercase>:<timestamp>`
    - Each proof buys one upload; sign a new one per upload. It is spent only
      when Bee stores the upload. If the request fails before that, you can
      retry with the same proof while it is fresh. It is a bearer credential
      for one upload (not bound to the file): do not log or share it.
      A rejected proof
      returns **401** `OWNER_PROOF_INVALID`; a valid proof from another wallet
      returns **403** `STAMP_OWNERSHIP_DENIED`.

    **Performance benefit**: Uploading 50 files as a TAR manifest takes ~500ms vs
    ~14 seconds for sequential individual uploads (15x improvement).

    **Requirements**:
    - Upload as **multipart/form-data** with a `file` field containing a TAR archive
    - Valid `stamp_id` query parameter required
    - TAR must be **uncompressed** (`tar -cf`). A gzip, bzip2, xz or zstd archive
      gets 400 `TAR_COMPRESSED`; Bee stores a plain tar only.
    - TAR must contain at least one file
    - At most `MANIFEST_MAX_MEMBERS` entries (default 10,000, directories
      included: 400 `TAR_TOO_MANY_ENTRIES`), each path at most
      `MANIFEST_MAX_PATH_BYTES` bytes (default 1024: `TAR_PATH_TOO_LONG`) and
      `MANIFEST_MAX_PATH_DEPTH` levels (default 64: `TAR_PATH_TOO_DEEP`)
    - Each PAX extended header and GNU long name at most
      `MANIFEST_MAX_EXTENDED_HEADER_BYTES` (default 65536), at most four in front
      of one entry, at most one global PAX header of at most 512 bytes, and no
      sparse entries (400 `TAR_EXTENDED_HEADER_REFUSED`)
    - An archive refused for any of these is refused before payment and not charged
    - Optional `validate_stamp` parameter (defaults to false)
    - Optional `deferred` parameter (defaults to false)
    - Optional `include_timing` parameter (defaults to false)
    - Optional `redundancy` parameter (0-4, defaults to 2=strong)
    - Optional `index_document` and `error_document` parameters (see below)

    **Index and error documents** (#700):
    - When the archive has a regular file `index.html` at its root (`index.html`
      or `./index.html`), the collection gets it as its index document, so
      `GET /api/v1/data/{reference}` returns it, and `GET
      /api/v1/data/{reference}/{dir}/` returns `{dir}/index.html`. This changes
      the reference: the same archive uploaded before this release got another
      one. Without a root `index.html` nothing changes.
    - `index_document=<name>` names another index document: a regular file at
      the archive's root (no `/`; Bee looks for that name in every directory).
      `error_document=<path>` names a regular file anywhere in the archive that
      Bee serves, with status 200, for any path the collection does not have.
      Both in plain ASCII without `.`/`..` or empty segments, or the upload gets
      400 `INDEX_DOCUMENT_INVALID` / `ERROR_DOCUMENT_INVALID` before payment
      (not charged).

    **Stamp Validation** (opt-in with `validate_stamp=true`):
    When enabled, validates the stamp before upload:
    - Returns 400 if stamp is at 100% utilization (full)
    - Returns 400 if stamp is not usable (expired, invalid)
    - Returns 404 if stamp is not found

    **Redundancy Levels** (erasure coding for data durability):
    - `0` = None: No erasure coding (0% chunk loss tolerance)
    - `1` = Medium: 1% chunk loss tolerance
    - `2` = Strong: 5% chunk loss tolerance (default)
    - `3` = Insane: 10% chunk loss tolerance
    - `4` = Paranoid: 50% chunk loss tolerance

    Higher redundancy levels increase storage cost but improve data durability.
    See: https://docs.ethswarm.org/docs/concepts/DISC/erasure-coding/

    **Deferred Mode** (opt-in with `deferred=true`):
    - `deferred=false` (default): Direct upload - chunks uploaded directly to network,
      ensuring immediate availability. Safer for gateway use cases.
    - `deferred=true`: Deferred upload - data goes to local node first, then syncs to
      network asynchronously. Faster upload response but data may not be immediately
      retrievable from the network.

    **Performance Timing** (opt-in with `include_timing=true`):
    When enabled, includes timing breakdown in response and Server-Timing header:
    - `stamp_validate_ms`: Time validating stamp (only if validate_stamp=true)
    - `file_read_ms`: Time reading uploaded file
    - `tar_validate_ms`: Time validating the TAR archive and counting its files
    - `tar_count_ms`: Always 0; files are counted in the validation pass
    - `bee_upload_ms`: Time uploading to Bee node
    - `total_ms`: Total request processing time
    - `file_count`: Number of files in the TAR archive
    - `ms_per_file`: Average milliseconds per file
    - `files_per_second`: Upload throughput

    **Usage Examples**:
    ```bash
    # Create TAR archive
    tar -cvf files.tar file1.json file2.json file3.json

    # Upload TAR as collection (direct mode, default)
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123" \\
         -F "file=@files.tar"

    # Upload with deferred mode (faster response)
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&deferred=true" \\
         -F "file=@files.tar"

    # Upload with pre-validation
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&validate_stamp=true" \\
         -F "file=@files.tar"

    # Upload with timing information
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&include_timing=true" \\
         -F "file=@files.tar"
    ```

    **Accessing individual files**:
    After upload, download each file through the gateway with
    `GET /api/v1/data/{reference}/{file_path}` (free). `GET /api/v1/data/{reference}`
    returns the index document, and answers 404 when the collection has none.

    **Response**:
    ```json
    {
        "reference": "a1b2c3...",
        "file_count": 50,
        "message": "Collection uploaded successfully"
    }
    ```
    """
    # The ownership registry is keyed in lowercase; the query accepts either
    # case (#473).
    stamp_id = stamp_id.lower()
    start_time = time.perf_counter()
    stamp_validate_ms = None
    file_read_ms = None
    tar_validate_ms = None
    tar_count_ms = None
    bee_upload_ms = None
    # The owner proof that granted access, if any. Released in the finally
    # block unless the Bee upload settled it, so a request refused or failed
    # before Bee does not spend the proof (#470).
    owner_proof = None

    try:
        # Reject an over-sized body before anything else.
        #
        # This ran after the stamp ownership check, so an over-sized upload with
        # an unrecognised stamp answered 403 STAMP_OWNERSHIP_DENIED and the
        # documented 413 was unreachable — the caller was told the wrong thing
        # about their request. Size does not depend on who is asking, and the
        # check is a header comparison against a registry lookup, so it belongs
        # first on both counts.
        #
        # Content-Length covers the whole multipart envelope — boundary, part
        # headers, trailer — not just the file, so it is compared against the
        # limit plus an allowance for that wrapper. Without it a file of exactly
        # MAX_UPLOAD_SIZE_MB was always rejected, putting the real ceiling a few
        # hundred bytes below the documented one and making it shrink as the
        # filename grew.
        #
        # This cannot reject early despite reading like it should: FastAPI parses
        # the multipart form during dependency resolution, so the body is already
        # in memory by the time this line runs. It is a coarse guard, not a fast
        # path. The exact limit is enforced on the file's own length further down.
        max_size = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_size + MULTIPART_ENVELOPE_ALLOWANCE:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": f"Upload exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB} MB.",
                    "max_size_mb": settings.MAX_UPLOAD_SIZE_MB
                }
            )

        # Check stamp ownership
        if settings.X402_ENABLED:
            owner_proof = _check_stamp_access(request, stamp_id, x_owner_timestamp, x_owner_signature)

        # Validate redundancy level if provided
        if redundancy is not None and redundancy not in REDUNDANCY_LEVELS:
            valid_levels = ", ".join(f"{k}={v}" for k, v in REDUNDANCY_LEVELS.items())
            raise HTTPException(
                status_code=400,
                detail=f"Invalid redundancy level {redundancy}. Must be 0-4 ({valid_levels})"
            )

        # Index and error document names (#700): their form here, their presence
        # in the archive in the inspect_tar pass below, both before payment.
        for value, code, label, single in (
                (index_document, "INDEX_DOCUMENT_INVALID", "index_document", True),
                (error_document, "ERROR_DOCUMENT_INVALID", "error_document", False)):
            if value is None:
                continue
            try:
                _document_path(value, single_segment=single)
            except ValueError as e:
                raise HTTPException(status_code=400, detail={
                    "code": code, "message": f"{label}: {e}",
                })

        # Optional pre-upload stamp validation. Always done for a paid upload:
        # the payment is settled just before the upload, so a stamp Bee would
        # refuse (missing, expired, full) must be caught while it is still free.
        if validate_stamp or getattr(request.state, "x402_mode", None) == "paid":
            stamp_start = time.perf_counter()
            try:
                await validate_stamp_for_upload(stamp_id, local_only=not validate_stamp)
            except StampValidationError as e:
                # Build structured error response
                detail = {
                    "code": e.code,
                    "message": e.message,
                    "suggestion": e.suggestion,
                    "stamp_id": stamp_id
                }
                if e.stamp_data:
                    detail["stamp_status"] = {
                        "exists": True,
                        "local": e.stamp_data.get("local", False),
                        "usable": e.stamp_data.get("usable"),
                        "utilizationPercent": e.stamp_data.get("utilizationPercent"),
                        "utilizationStatus": e.stamp_data.get("utilizationStatus"),
                        "batchTTL": e.stamp_data.get("batchTTL"),
                        "expectedExpiration": e.stamp_data.get("expectedExpiration")
                    }
                if e.status == "not_found":
                    raise HTTPException(status_code=404, detail=detail)
                else:
                    raise HTTPException(status_code=400, detail=detail)
            stamp_validate_ms = (time.perf_counter() - stamp_start) * 1000


        # Read TAR file content
        file_start = time.perf_counter()
        tar_bytes = await file.read()
        file_read_ms = (time.perf_counter() - file_start) * 1000

        if len(tar_bytes) > max_size:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": f"Upload exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB} MB.",
                    "max_size_mb": settings.MAX_UPLOAD_SIZE_MB
                }
            )

        # Validate the TAR archive and count its files in one streaming pass,
        # off the event loop (#694): parsing a large archive on the loop held
        # every other request, /health included, until it finished. Still
        # before settle_payment, so a refused archive is never charged.
        tar_validate_start = time.perf_counter()
        try:
            wanted = {name for name in (ROOT_INDEX_DOCUMENT, index_document, error_document) if name}
            summary: TarSummary = await asyncio.wrap_future(
                _tar_lane.submit(functools.partial(inspect_tar, tar_bytes, wanted=wanted)))
        except TarRefused as e:
            if e.code is None:
                raise HTTPException(status_code=e.status, detail=e.message)
            raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})
        file_count = summary.file_count
        if index_document is not None and index_document not in summary.found:
            raise HTTPException(status_code=400, detail={
                "code": "INDEX_DOCUMENT_INVALID",
                "message": f"index_document: the archive has no regular file at {index_document!r}.",
            })
        if error_document is not None and error_document not in summary.found:
            raise HTTPException(status_code=400, detail={
                "code": "ERROR_DOCUMENT_INVALID",
                "message": f"error_document: the archive has no regular file at {error_document!r}.",
            })
        if index_document is None and ROOT_INDEX_DOCUMENT in summary.found:
            index_document = ROOT_INDEX_DOCUMENT
        tar_validate_ms = (time.perf_counter() - tar_validate_start) * 1000
        # Counted in the same pass; the field stays for existing clients.
        tar_count_ms = 0.0

        # Upload to Swarm as collection
        bee_start = time.perf_counter()
        await settle_payment(request)
        reference = await _upload_to_bee(owner_proof, upload_collection_to_swarm(
            tar_bytes, stamp_id, deferred=deferred, redundancy_level=redundancy,
            index_document=index_document, error_document=error_document,
        ))
        bee_upload_ms = (time.perf_counter() - bee_start) * 1000

        total_ms = (time.perf_counter() - start_time) * 1000

        # Calculate derived metrics
        ms_per_file = total_ms / file_count if file_count > 0 else 0
        files_per_second = (file_count / (total_ms / 1000)) if total_ms > 0 else 0

        # Build response
        timing = None
        if include_timing:
            timing = ManifestUploadTiming(
                stamp_validate_ms=stamp_validate_ms,
                file_read_ms=file_read_ms,
                tar_validate_ms=tar_validate_ms,
                tar_count_ms=tar_count_ms,
                bee_upload_ms=bee_upload_ms,
                total_ms=total_ms,
                file_count=file_count,
                ms_per_file=ms_per_file,
                files_per_second=files_per_second
            )

        uploads_total.labels(status="success").inc()
        upload_bytes_total.inc(len(tar_bytes))

        response = ManifestUploadResponse(
            reference=reference,
            file_count=file_count,
            message=f"Collection uploaded successfully with {file_count} files",
            timing=timing,
            expires_at=await get_batch_expiry(stamp_id),
        )

        # Always add Server-Timing header (useful for browser devtools)
        timing_dict = {
            "stamp_validate_ms": stamp_validate_ms,
            "file_read_ms": file_read_ms,
            "tar_validate_ms": tar_validate_ms,
            "tar_count_ms": tar_count_ms,
            "bee_upload_ms": bee_upload_ms,
            "total_ms": total_ms,
            "ms_per_file": ms_per_file,
            "files_per_second": files_per_second
        }
        server_timing = _build_server_timing_header(timing_dict)

        return Response(
            content=response.model_dump_json(),
            media_type="application/json",
            headers={"Server-Timing": server_timing}
        )

    except HTTPException:
        uploads_total.labels(status="error").inc()
        raise
    except httpx2.HTTPError as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Swarm API error during manifest upload: {exc_text(e)}")
        failure_info = await check_upload_failure_reason(stamp_id, str(e))
        if failure_info:
            detail = {
                "code": failure_info.get("code"),
                "message": failure_info.get("message"),
                "suggestion": failure_info.get("suggestion"),
                "stamp_id": stamp_id,
                "stamp_status": failure_info.get("stamp_status")
            }
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=502, detail="Failed to upload collection to Swarm. The Bee node may be unavailable.")
    except ValueError as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Data processing error during manifest upload: {exc_text(e)}")
        raise HTTPException(status_code=400, detail="Manifest upload error. Please check your request and try again.")
    except Exception as e:
        uploads_total.labels(status="error").inc()
        logger.error(f"Unexpected error during manifest upload: {exc_text(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during manifest upload")
    finally:
        release_owner_proof(owner_proof)