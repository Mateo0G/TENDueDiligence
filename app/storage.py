"""Thin wrapper around the R2 (S3-compatible) bucket used for dataroom
uploads and generated docx output. Postgres stores object keys only, never
file bytes -- see docs/schema.md.

Client construction is lazy (first actual call, not import time) so that
importing this module -- which happens transitively through app.tasks for
any worker process, including one running Phase 1's DB-only tests -- never
requires R2 credentials unless something genuinely touches storage.
"""
import os

import boto3
from dotenv import load_dotenv

load_dotenv()

_client = None
_bucket = None


def _get_client():
    global _client, _bucket
    if _client is None:
        _bucket = os.environ["R2_BUCKET_NAME"]
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    return _client


def put_bytes(key: str, data: bytes, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else {}
    _get_client().put_object(Bucket=_bucket, Key=key, Body=data, **extra)


def get_bytes(key: str) -> bytes:
    return _get_client().get_object(Bucket=_bucket, Key=key)["Body"].read()


def delete(key: str) -> None:
    _get_client().delete_object(Bucket=_bucket, Key=key)


def list_prefix(prefix: str) -> list[str]:
    client = _get_client()
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys
