"""サンプルのアプリ（orders.py）のテスト。エミュレーターを相手に、本物と同じコードを動かす。

実行:  ./emu run -- python -m unittest discover -s examples -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import orders  # noqa: E402
from emuscope.testing import EmulatorTestCase  # noqa: E402


class OrderTest(EmulatorTestCase):
    clean_start = True  # 前のテストの残りに左右されないよう、空の状態から始める

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        orders.setup()

    def test_order_is_saved_everywhere(self):
        order_id = orders.place_order("sato", [{"name": "ノート", "price": 300, "quantity": 2}])

        saved = orders.get_order(order_id)
        self.assertEqual(saved["total"], 600)
        self.assertEqual(saved["status"], "受付")

        receipt = self.client("s3").get_object(Bucket=orders.BUCKET, Key=f"sato/{order_id}.json")
        self.assertEqual(json.loads(receipt["Body"].read())["order_id"], order_id)

        sqs = self.client("sqs")
        url = sqs.get_queue_url(QueueName=orders.QUEUE)["QueueUrl"]
        bodies = [json.loads(m["Body"]) for m in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])]
        self.assertIn({"order_id": order_id, "customer": "sato"}, bodies)

    def test_empty_order_is_rejected(self):
        with self.assertRaises(ValueError):
            orders.place_order("sato", [])

    def test_setup_can_run_twice(self):
        orders.setup()  # すでにあっても失敗しない
        self.assertIn(orders.TABLE, self.client("dynamodb").list_tables()["TableNames"])
