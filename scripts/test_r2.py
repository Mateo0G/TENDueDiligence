"""One-off connectivity smoke test for the R2 bucket -- upload, read back,
verify content, delete. Prints only pass/fail, never the credentials
themselves (reads them from the environment; never logs os.environ).

Run via: railway run --service worker -- python scripts/test_r2.py
"""
import os
import uuid

import boto3

def main():
    bucket = os.environ["R2_BUCKET_NAME"]
    endpoint = os.environ["R2_ENDPOINT_URL"]

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    key = f"phase2-smoke-test/{uuid.uuid4()}.txt"
    body = b"ten due diligence - r2 connectivity check"

    client.put_object(Bucket=bucket, Key=key, Body=body)
    fetched = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert fetched == body, "round-tripped content did not match"
    client.delete_object(Bucket=bucket, Key=key)

    # Confirm the delete actually took.
    keys_after = client.list_objects_v2(Bucket=bucket, Prefix=key).get("Contents", [])
    assert not keys_after, "object still present after delete"

    print("PASS -- put/get/delete round-trip succeeded against R2 bucket", bucket)


if __name__ == "__main__":
    main()
