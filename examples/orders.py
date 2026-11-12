"""サンプルのアプリ: 注文を受け付ける。

DynamoDB に注文を保存し、S3 に控えを置き、SQS に発送の依頼を送る。
エンドポイントや鍵はどこにも書いていない。環境変数しだいで、本物のAWSにもエミュレーターにも向く。
"""

import json
import uuid

import boto3

TABLE = "orders"
BUCKET = "order-receipts"
QUEUE = "shipping-requests"


def setup():
    """必要なリソースを作る。すでにあれば何もしない。"""
    ddb = boto3.client("dynamodb")
    if TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.get_waiter("table_exists").wait(TableName=TABLE, WaiterConfig={"Delay": 0.2})
    s3 = boto3.client("s3")
    if BUCKET not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=BUCKET)
    boto3.client("sqs").create_queue(QueueName=QUEUE)


def place_order(customer: str, items: list[dict]) -> str:
    """注文を1件受け付けて、注文IDを返す。"""
    if not items:
        raise ValueError("商品が1つもありません")
    order_id = uuid.uuid4().hex[:12]
    total = sum(i["price"] * i["quantity"] for i in items)
    order = {"order_id": order_id, "customer": customer, "items": items, "total": total, "status": "受付"}

    boto3.resource("dynamodb").Table(TABLE).put_item(
        Item=order,
        ConditionExpression="attribute_not_exists(order_id)",  # 同じIDを二重に書かない
    )
    boto3.client("s3").put_object(
        Bucket=BUCKET, Key=f"{customer}/{order_id}.json",
        Body=json.dumps(order, ensure_ascii=False, indent=2).encode(), ContentType="application/json",
    )
    sqs = boto3.client("sqs")
    sqs.send_message(
        QueueUrl=sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"],
        MessageBody=json.dumps({"order_id": order_id, "customer": customer}),
    )
    return order_id


def get_order(order_id: str) -> dict | None:
    return boto3.resource("dynamodb").Table(TABLE).get_item(Key={"order_id": order_id}).get("Item")


if __name__ == "__main__":
    # 試し方: ./emu run --log -- python examples/orders.py   （画面の「APIログ」に呼び出しが出る）
    setup()
    oid = place_order("sato", [{"name": "ノート", "price": 300, "quantity": 2}, {"name": "ペン", "price": 150, "quantity": 1}])
    print("注文を受け付けました:", oid, get_order(oid)["total"], "円")
