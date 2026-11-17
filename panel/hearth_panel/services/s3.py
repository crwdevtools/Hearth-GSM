"""S3-compatible storage (Amazon S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO) for off-site backups.

The panel holds the credentials and hands out presigned URLs (AWS Signature Version 4, query-string
form) so a node can upload or download one object, or one part of one, without ever seeing the
keys. Only what Hearth needs is here: PUT/GET/DELETE an object and multipart uploads.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

PART_SIZE = 128 * 1024 * 1024  # multipart chunk; S3 allows 5 MB–5 GB per part, 10,000 parts
SINGLE_PUT_LIMIT = 1024 * 1024 * 1024  # above this, upload in parts


class S3Error(Exception):
    pass


@dataclass(frozen=True)
class Target:
    endpoint: str  # https://s3.us-west-002.backblazeb2.com
    region: str  # us-east-1; "auto" for Cloudflare R2
    bucket: str
    access_key: str
    secret_key: str
    prefix: str = "hearth/"
    path_style: bool = True  # https://endpoint/bucket/key (works everywhere); False: https://bucket.endpoint/key

    def key(self, *parts: str) -> str:
        return self.prefix + "/".join(p.strip("/") for p in parts)

    def object_url(self, key: str) -> tuple[str, str]:
        """(host, path) for an object."""
        parts = urlsplit(self.endpoint.rstrip("/"))
        encoded = quote(key, safe="/~")
        if self.path_style:
            return parts.netloc, f"{parts.path}/{self.bucket}/{encoded}"
        return f"{self.bucket}.{parts.netloc}", f"{parts.path}/{encoded}"

    def scheme(self) -> str:
        return urlsplit(self.endpoint).scheme or "https"


def validate(t: Target) -> None:
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+(/[^\s?#]*)?", t.endpoint or ""):
        raise S3Error("The endpoint must be a URL like https://s3.us-west-002.backblazeb2.com")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", t.bucket or ""):
        raise S3Error("That isn't a valid bucket name.")
    if not t.access_key or not t.secret_key:
        raise S3Error("Enter the access key and secret.")
    if not re.fullmatch(r"[a-z0-9\-]{1,32}", t.region or ""):
        raise S3Error("Enter the region (for example us-east-1, or auto for Cloudflare R2).")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign(t: Target, method: str, key: str, *, expires: int = 3600, query: dict[str, str] | None = None,
            now: datetime | None = None) -> str:
    """A URL that performs `method` on `key` for `expires` seconds, signed with SigV4."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{t.region}/s3/aws4_request"
    host, path = t.object_url(key)
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{t.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
        **(query or {}),
    }
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    canonical = "\n".join([method, path, canonical_query, f"host:{host}", "", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{t.secret_key}".encode(), date), t.region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{t.scheme()}://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"


# ----- calls the panel makes itself -------------------------------------------------------------


async def _send(client, method: str, url: str, **kwargs):
    import httpx

    try:
        resp = await client.request(method, url, timeout=60, follow_redirects=False, **kwargs)
    except httpx.HTTPError as exc:
        raise S3Error(f"couldn't reach the storage ({exc.__class__.__name__})") from exc
    if resp.status_code >= 300 and not (method == "DELETE" and resp.status_code == 404):
        code = re.search(r"<Code>([^<]+)</Code>", resp.text)
        message = re.search(r"<Message>([^<]+)</Message>", resp.text)
        raise S3Error(f"{resp.status_code} {code.group(1) if code else ''} {message.group(1) if message else ''}".strip())
    return resp


async def put_bytes(client, t: Target, key: str, data: bytes) -> None:
    await _send(client, "PUT", presign(t, "PUT", key), content=data)


async def get_bytes(client, t: Target, key: str) -> bytes:
    return (await _send(client, "GET", presign(t, "GET", key))).content


async def delete(client, t: Target, key: str) -> None:
    await _send(client, "DELETE", presign(t, "DELETE", key))


async def start_multipart(client, t: Target, key: str) -> str:
    resp = await _send(client, "POST", presign(t, "POST", key, query={"uploads": ""}))
    found = re.search(r"<UploadId>([^<]+)</UploadId>", resp.text)
    if not found:
        raise S3Error("the storage didn't start a multipart upload")
    return found.group(1)


def part_urls(t: Target, key: str, upload_id: str, parts: int, expires: int = 12 * 3600) -> list[str]:
    return [presign(t, "PUT", key, expires=expires, query={"partNumber": str(n), "uploadId": upload_id})
            for n in range(1, parts + 1)]


async def complete_multipart(client, t: Target, key: str, upload_id: str, etags: list[str]) -> None:
    body = "<CompleteMultipartUpload>" + "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>{etag}</ETag></Part>" for n, etag in enumerate(etags, 1)
    ) + "</CompleteMultipartUpload>"
    resp = await _send(client, "POST", presign(t, "POST", key, query={"uploadId": upload_id}), content=body.encode())
    if "<Error>" in resp.text:  # S3 can report a failed completion inside a 200
        raise S3Error(re.sub(r"\s+", " ", resp.text)[:300])


async def abort_multipart(client, t: Target, key: str, upload_id: str) -> None:
    try:
        await _send(client, "DELETE", presign(t, "DELETE", key, query={"uploadId": upload_id}))
    except S3Error:
        pass
