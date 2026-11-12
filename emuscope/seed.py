"""画面で眺めるためのサンプルデータを入れる。  実行: ./emu seed

何度実行してもよい（同じ名前のものは上書きされる）。
"""

import json
import struct
import time
import zlib

from botocore.exceptions import ClientError

from .aws import client, error_code

BUCKET = "sample-bucket"
TABLE = "sample-users"
QUEUE = "sample-jobs"


def _ignore(codes, fn):
    try:
        return fn()
    except ClientError as e:
        if error_code(e) not in codes:
            raise


def tiny_png(width=96, height=64) -> bytes:
    """外部ライブラリなしで、グラデーションのPNGを作る。"""
    rows = b"".join(
        b"\x00" + bytes(c for x in range(width) for c in (x * 255 // width, y * 255 // height, 160))
        for y in range(height)
    )
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def seed_s3():
    s3 = client("s3")
    _ignore({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}, lambda: s3.create_bucket(Bucket=BUCKET))
    files = {
        "readme.txt": ("text/plain", "これはサンプルのテキストです。\n2行目です。\n".encode()),
        "config/app.json": ("application/json", b'{"name":"sample","debug":true,"ports":[8080,8443]}'),
        "logs/2026/11/access.csv": ("text/csv", b"time,path,status\n10:00:01,/,200\n10:00:02,/missing,404\n"),
        "images/gradient.png": ("image/png", tiny_png()),
        "archive/data.bin": ("application/octet-stream", bytes(range(256)) * 4),
        "empty.txt": ("text/plain", b""),
        # 中身や名前がプログラムとして実行されないことを確かめるためのファイル
        "danger/script.html": ("text/html", b"<h1>HTML</h1><script>document.title='XSS'</script>"),
        "danger/<img src=x onerror=alert(1)>.txt": ("text/plain", b"file name contains HTML"),
    }
    for key, (content_type, body) in files.items():
        s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)
    return f"S3: {BUCKET} に {len(files)} 個"


def seed_dynamodb():
    ddb = client("dynamodb")
    _ignore({"ResourceInUseException"}, lambda: ddb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "team", "AttributeType": "S"}, {"AttributeName": "id", "AttributeType": "N"}],
        KeySchema=[{"AttributeName": "team", "KeyType": "HASH"}, {"AttributeName": "id", "KeyType": "RANGE"}],
        BillingMode="PAY_PER_REQUEST",
    ))
    ddb.get_waiter("table_exists").wait(TableName=TABLE, WaiterConfig={"Delay": 0.2, "MaxAttempts": 100})
    items = [
        {"team": {"S": "red"}, "id": {"N": "1"}, "name": {"S": "佐藤"}, "active": {"BOOL": True}, "tags": {"SS": ["admin", "dev"]}},
        {"team": {"S": "red"}, "id": {"N": "2"}, "name": {"S": "鈴木"}, "active": {"BOOL": False}, "score": {"N": "12"}},
        {"team": {"S": "blue"}, "id": {"N": "1"}, "name": {"S": "田中"}, "active": {"BOOL": True},
         "profile": {"M": {"city": {"S": "東京"}, "langs": {"L": [{"S": "ja"}, {"S": "en"}]}}}},
        {"team": {"S": "blue"}, "id": {"N": "2"}, "name": {"S": "<b>太字</b>"}, "active": {"BOOL": True}, "note": {"NULL": True}},
    ]
    for item in items:
        ddb.put_item(TableName=TABLE, Item=item)
    return f"DynamoDB: {TABLE} に {len(items)} 件"


def seed_sqs():
    sqs = client("sqs")
    url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
    sqs.purge_queue(QueueUrl=url)
    bodies = [json.dumps({"job": "resize", "file": "images/gradient.png"}), json.dumps({"job": "mail", "to": "user-1"}), "plain text message"]
    for body in bodies:
        sqs.send_message(QueueUrl=url, MessageBody=body)
    return f"SQS: {QUEUE} に {len(bodies)} 通"


def seed_misc():
    """ほかのサービスにも1つずつ。画面の「詳細」を試せるようにする。"""
    client("ssm").put_parameter(Name="/sample/app/log-level", Value="debug", Type="String", Overwrite=True)
    client("ssm").put_parameter(Name="/sample/app/api-key", Value="dummy-key-123", Type="SecureString", Overwrite=True)
    sm = client("secretsmanager")
    secret = json.dumps({"username": "app", "password": "dummy-password"})
    if _ignore({"ResourceExistsException"}, lambda: sm.create_secret(Name="sample/db", SecretString=secret)) is None:
        sm.put_secret_value(SecretId="sample/db", SecretString=secret)
    client("sns").create_topic(Name="sample-events")
    logs = client("logs")
    _ignore({"ResourceAlreadyExistsException"}, lambda: logs.create_log_group(logGroupName="/sample/app"))
    _ignore({"ResourceAlreadyExistsException"}, lambda: logs.create_log_stream(logGroupName="/sample/app", logStreamName="web-1"))
    now = int(time.time() * 1000)
    logs.put_log_events(logGroupName="/sample/app", logStreamName="web-1", logEvents=[
        {"timestamp": now, "message": "server started on :8080"},
        {"timestamp": now + 1, "message": json.dumps({"level": "warn", "msg": "slow query", "ms": 812})},
    ])
    return "ほか: SSM パラメーター 2、シークレット 1、SNS トピック 1、ロググループ 1"


def run():
    return [seed_s3(), seed_dynamodb(), seed_sqs(), seed_misc()]
