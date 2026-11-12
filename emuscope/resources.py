"""S3・DynamoDB・SQS の中身の読み書き。"""

import json
import os

from boto3.dynamodb.types import TypeDeserializer

from .aws import client, jsonable, typed_from_json, typed_to_json

MAX_OBJECTS = 1000  # S3 の一覧で一度に出す件数
PAGE_ITEMS = 100  # DynamoDB の表で一度に出す件数
MAX_TEXT_BYTES = 1024 * 1024  # テキストとして読む上限（1MB）
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 100  # SQS で一度にのぞく件数

# 画像として返してよい種類。ここにないものは、中身をブラウザに解釈させない。
# SVG は中にスクリプトを書けるので、意図的に入れていない（テキストとして表示する）。
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


# ---- S3 ----------------------------------------------------------------------


def s3_list(bucket, prefix="", flat=False, token=None):
    """ファイルの一覧。flat=False ならフォルダ（/ 区切り）ごとに見せる。"""
    kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": MAX_OBJECTS}
    if not flat:
        kwargs["Delimiter"] = "/"
    if token:
        kwargs["ContinuationToken"] = token
    res = client("s3").list_objects_v2(**kwargs)
    return {
        "prefix": prefix,
        "folders": [p["Prefix"] for p in res.get("CommonPrefixes", [])],
        "objects": [
            {"key": o["Key"], "size": o["Size"], "modified": o["LastModified"].isoformat()}
            for o in res.get("Contents", [])
            if o["Key"] != prefix  # フォルダ自身を表す空ファイルは出さない
        ],
        "next": res.get("NextContinuationToken"),
        "limit": MAX_OBJECTS,
    }


def image_type(key):
    return IMAGE_TYPES.get(os.path.splitext(key)[1].lower())


def s3_object(bucket, key):
    """ファイル1つの情報と、表示できる場合はその中身。"""
    s3 = client("s3")
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    info = {
        "key": key, "size": size, "uri": f"s3://{bucket}/{key}",
        "contentType": head.get("ContentType", ""),
        "modified": head["LastModified"].isoformat(),
        "etag": head.get("ETag", "").strip('"'),
        "metadata": head.get("Metadata", {}),
        "kind": "binary",
    }
    try:
        info["tags"] = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", [])}
    except Exception:  # noqa: BLE001 - タグは見られなくても困らない
        info["tags"] = {}
    if image_type(key):
        info["kind"] = "image" if size <= MAX_IMAGE_BYTES else "binary"
        return info
    if size == 0:
        info.update(kind="text", text="", truncated=False)
        return info

    truncated = size > MAX_TEXT_BYTES
    kwargs = {"Range": f"bytes=0-{MAX_TEXT_BYTES - 1}"} if truncated else {}
    text = as_text(s3.get_object(Bucket=bucket, Key=key, **kwargs)["Body"].read())
    if text is None:
        return info
    if truncated:
        text = text.rstrip("�")  # 切り口が文字の途中に当たると化けるので、その半端な1文字を捨てる
    else:
        text = pretty_json(text)
    info.update(kind="text", text=text, truncated=truncated)
    return info


def as_text(data):
    """テキストなら文字列を、そうでなければ None を返す。"""
    if b"\x00" in data:
        return None
    text = data.decode("utf-8", errors="replace")
    if text.count("�") > len(text) * 0.05:
        return None
    return text


def pretty_json(text):
    if text.lstrip()[:1] not in ("{", "["):
        return text
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        return text


def s3_image(bucket, key):
    content_type = image_type(key)
    if not content_type:
        raise KeyError(key)
    s3 = client("s3")
    if s3.head_object(Bucket=bucket, Key=key)["ContentLength"] > MAX_IMAGE_BYTES:
        raise KeyError(key)
    return content_type, s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_download(bucket, key):
    return client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_upload(bucket, key, body, content_type):
    client("s3").put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type or "application/octet-stream")
    return {"key": key, "size": len(body)}


def s3_delete(bucket, key):
    client("s3").delete_object(Bucket=bucket, Key=key)
    return {"deleted": key}


def s3_create_bucket(bucket):
    client("s3").create_bucket(Bucket=bucket)
    return {"bucket": bucket}


def s3_delete_bucket(bucket):
    """中身ごと消す。"""
    s3 = client("s3")
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=o["Key"])
            count += 1
    s3.delete_bucket(Bucket=bucket)
    return {"deleted": bucket, "objects": count}


# ---- DynamoDB ----------------------------------------------------------------

_deserializer = TypeDeserializer()


def ddb_schema(table):
    t = client("dynamodb").describe_table(TableName=table)["Table"]
    types = {a["AttributeName"]: a["AttributeType"] for a in t["AttributeDefinitions"]}
    keys = [k["AttributeName"] for k in sorted(t["KeySchema"], key=lambda k: k["KeyType"] != "HASH")]
    return t, keys, types


def ddb_items(table, pk=None, start=None):
    """項目の一覧。pk を渡すと、そのパーティションキーの項目だけに絞る（Query）。"""
    ddb = client("dynamodb")
    t, keys, types = ddb_schema(table)
    kwargs = {"TableName": table, "Limit": PAGE_ITEMS}
    if start:
        kwargs["ExclusiveStartKey"] = typed_from_json(start)
    if pk not in (None, ""):
        kwargs.update(
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": keys[0]},
            ExpressionAttributeValues={":pk": {types[keys[0]]: pk}},
        )
        res = ddb.query(**kwargs)
    else:
        res = ddb.scan(**kwargs)
    items = [
        {"plain": jsonable({k: _deserializer.deserialize(v) for k, v in raw.items()}), "typed": typed_to_json(raw)}
        for raw in res.get("Items", [])
    ]
    others = sorted({name for item in items for name in item["plain"]} - set(keys))
    return {
        "keys": keys, "keyTypes": {k: types[k] for k in keys}, "columns": keys + others,
        "items": items,
        "next": typed_to_json(res["LastEvaluatedKey"]) if "LastEvaluatedKey" in res else None,
        "limit": PAGE_ITEMS,
        "status": t.get("TableStatus"),
        "indexes": [i["IndexName"] for i in t.get("GlobalSecondaryIndexes", []) + t.get("LocalSecondaryIndexes", [])],
    }


def ddb_put(table, typed_item):
    client("dynamodb").put_item(TableName=table, Item=typed_from_json(typed_item))
    return {"ok": True}


def ddb_delete(table, typed_key):
    client("dynamodb").delete_item(TableName=table, Key=typed_from_json(typed_key))
    return {"ok": True}


def ddb_delete_table(table):
    client("dynamodb").delete_table(TableName=table)
    return {"deleted": table}


# ---- SQS ---------------------------------------------------------------------


def _drain(url, limit):
    """キューの中身を、消さずに読み出す。

    SQS には「のぞくだけ」のAPIがない。いったん受信して、すぐに「未処理」に戻す。
    副作用として、各メッセージの受信回数（ApproximateReceiveCount）が1増える。
    """
    sqs = client("sqs")
    seen, out = set(), []
    while len(out) < limit:
        batch = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=20, WaitTimeSeconds=0,
            AttributeNames=["All"], MessageAttributeNames=["All"],
        ).get("Messages", [])
        fresh = [m for m in batch if m["MessageId"] not in seen]
        if not fresh:
            break
        for m in fresh:
            seen.add(m["MessageId"])
            out.append(m)
    for i in range(0, len(out), 10):  # 他の受信者が取れるよう、すぐ戻す
        sqs.change_message_visibility_batch(
            QueueUrl=url,
            Entries=[
                {"Id": str(n), "ReceiptHandle": m["ReceiptHandle"], "VisibilityTimeout": 0}
                for n, m in enumerate(out[i:i + 10])
            ],
        )
    return out


def sqs_detail(url):
    sqs = client("sqs")
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"]).get("Attributes", {})
    for key in ("Policy", "RedrivePolicy"):
        if key in attributes:
            try:
                attributes[key] = json.loads(attributes[key])
            except ValueError:
                pass
    messages = [
        {
            "id": m["MessageId"], "body": m.get("Body", ""),
            "attributes": m.get("Attributes", {}),
            "messageAttributes": jsonable(m.get("MessageAttributes", {})),
        }
        for m in _drain(url, MAX_MESSAGES)
    ]
    return {"url": url, "attributes": attributes, "messages": messages, "limit": MAX_MESSAGES,
            "fifo": url.endswith(".fifo")}


def sqs_send(url, body, group_id=None):
    kwargs = {"QueueUrl": url, "MessageBody": body}
    if url.endswith(".fifo"):
        import uuid

        kwargs["MessageGroupId"] = group_id or "default"
        kwargs["MessageDeduplicationId"] = uuid.uuid4().hex
    return {"id": client("sqs").send_message(**kwargs)["MessageId"]}


def sqs_purge(url):
    client("sqs").purge_queue(QueueUrl=url)
    return {"ok": True}


def sqs_delete_queue(url):
    client("sqs").delete_queue(QueueUrl=url)
    return {"deleted": url}
