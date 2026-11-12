"""Boto3 から AWS Emulator に接続して S3 / DynamoDB / 未実装API を試す。

コード内にはエンドポイントも認証情報も書かない。
接続先は環境変数（make env の出力）だけで決まる。  実行: make boto3
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

BUCKET = "emu-boto3-bucket"
TABLE = "emu-boto3-table"


def guard() -> None:
    """本物のAWSに向いていたら、何もせずに止まる。"""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if not endpoint.startswith("https://127.0.0.1:"):
        sys.exit(f"中止: AWS_ENDPOINT_URL がエミュレーターではありません ({endpoint or '未設定'})")
    identity = boto3.client("sts").get_caller_identity()
    if identity["Account"] != "000000000000":
        sys.exit(f"中止: エミュレーターのアカウントではありません ({identity['Account']})")
    print(f"接続先: {endpoint}  /  {identity['Arn']}")


def try_s3() -> None:
    print("--- S3")
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="hello.txt", Body="hello from boto3".encode())
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    body = s3.get_object(Bucket=BUCKET, Key="hello.txt")["Body"].read().decode()
    print(f"一覧: {keys}")
    print(f"取得: {body!r}")
    # 高レベルAPI（resource）と、署名付きURLの生成
    size = boto3.resource("s3").Object(BUCKET, "hello.txt").content_length
    url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": "hello.txt"}, ExpiresIn=60)
    print(f"resource 経由のサイズ: {size} バイト")
    print(f"署名付きURL: {url.split('?')[0]}?...")
    s3.delete_object(Bucket=BUCKET, Key="hello.txt")
    s3.delete_bucket(Bucket=BUCKET)


def try_dynamodb() -> None:
    print("--- DynamoDB")
    ddb = boto3.resource("dynamodb")
    table = ddb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    table.put_item(Item={"id": "user-1", "name": "boto3", "count": 42})
    table.update_item(
        Key={"id": "user-1"},
        UpdateExpression="ADD #c :one",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":one": 1},
    )
    print(f"get_item: {table.get_item(Key={'id': 'user-1'})['Item']}")
    # 条件付き書き込みが、本物と同じ例外で失敗するか
    try:
        table.put_item(Item={"id": "user-1"}, ConditionExpression="attribute_not_exists(id)")
        print("条件付き put: 通ってしまった（本物と違う）")
    except ClientError as e:
        print(f"条件付き put: {e.response['Error']['Code']}")
    table.delete()


def try_not_implemented() -> None:
    print("--- 未実装API")
    try:
        boto3.client("athena").list_work_groups()
        print("athena.list_work_groups: 成功した")
    except ClientError as e:
        meta = e.response["ResponseMetadata"]
        print(f"例外の型: {type(e).__name__}")
        print(f"Code: {e.response['Error']['Code']}  /  HTTP: {meta['HTTPStatusCode']}  /  再試行: {meta['RetryAttempts']}回")
        print(f"Message: {e.response['Error']['Message']}")


if __name__ == "__main__":
    print(f"boto3 {boto3.__version__}")
    guard()
    try_s3()
    try_dynamodb()
    try_not_implemented()
