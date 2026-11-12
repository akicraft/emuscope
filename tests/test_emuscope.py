"""emuscope 自体のテスト。実際のエミュレーターを相手に動かす。

実行:  ./emu run -- python -m unittest discover -s tests -v

注意: エミュレーターを再起動するので、中身はすべて消える。
"""

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from emuscope import aws, config, docker, proxy, resources, server, snapshot

UI_PORT, RO_PORT, PROXY_PORT = 19298, 19398, 19299  # 普段使うポートとぶつからないようにする
SNAP = "emuscope-selftest"


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Http:
    """テスト用の小さなHTTPクライアント。"""

    def __init__(self, port, token=""):
        self.base, self.token = f"http://127.0.0.1:{port}", token

    def call(self, method, path, params=None, body=None, headers=None, raw=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if method == "POST" and "X-Emu-Token" not in req.headers and self.token:
            req.add_header("X-Emu-Token", self.token)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read(), dict(res.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), dict(e.headers)

    def get(self, path, **params):
        status, body, _ = self.call("GET", path, params)
        return status, json.loads(body)

    def post(self, path, body=None, **kw):
        status, data, _ = self.call("POST", path, body=body or {}, **kw)
        return status, json.loads(data)


class OperationNameTest(unittest.TestCase):
    """呼び出しから操作名を求める部分。エミュレーターなしで確かめられる。"""

    def resolve(self, service, method, path, query="", headers=None, body=b""):
        return proxy.resolve_operation(service, method, path, query, headers or {}, body)

    def test_json_protocol_uses_target_header(self):
        self.assertEqual(self.resolve("dynamodb", "POST", "/", headers={"x-amz-target": "DynamoDB_20120810.PutItem"}), "PutItem")

    def test_query_protocol_uses_action(self):
        headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8"}
        self.assertEqual(self.resolve("sts", "POST", "/", headers=headers, body=b"Action=GetCallerIdentity&Version=2011-06-15"), "GetCallerIdentity")

    def test_s3_is_resolved_from_the_url_shape(self):
        cases = [
            ("GET", "/", "", {}, "ListBuckets"),
            ("PUT", "/b", "", {}, "CreateBucket"),
            ("GET", "/b", "list-type=2&prefix=x", {}, "ListObjectsV2"),
            ("PUT", "/b/dir/key.txt", "", {}, "PutObject"),
            ("PUT", "/b/key", "", {"x-amz-copy-source": "/a/k"}, "CopyObject"),
            ("GET", "/b/dir/key.txt", "", {}, "GetObject"),
            ("HEAD", "/b/key", "", {}, "HeadObject"),
            ("DELETE", "/b/key", "", {}, "DeleteObject"),
            ("DELETE", "/b/key", "uploadId=abc", {}, "AbortMultipartUpload"),
            ("GET", "/b/key", "tagging", {}, "GetObjectTagging"),
            ("POST", "/b/key", "uploads", {}, "CreateMultipartUpload"),
        ]
        for method, path, query, headers, expected in cases:
            with self.subTest(expected):
                self.assertEqual(self.resolve("s3", method, path, query, headers), expected)

    def test_rest_json_service(self):
        self.assertEqual(self.resolve("lambda", "GET", "/2015-03-31/functions/"), "ListFunctions")

    def test_unknown_falls_back_to_method_and_path(self):
        self.assertEqual(self.resolve("no-such-service", "GET", "/x/y"), "GET /x/y")

    def test_signing_name_and_error_code(self):
        auth = "AWS4-HMAC-SHA256 Credential=EMULATORABC/20261112/us-east-1/athena/aws4_request, SignedHeaders=host, Signature=abc"
        self.assertEqual(proxy.signing_name({"authorization": auth}, ""), "athena")
        self.assertEqual(proxy.error_code(501, {}, b'{"__type":"NotImplemented","message":"x"}'), "NotImplemented")
        self.assertEqual(proxy.error_code(404, {}, b"<Error><Code>NoSuchKey</Code></Error>"), "NoSuchKey")
        self.assertEqual(proxy.error_code(200, {}, b"{}"), "")


class TypedJsonTest(unittest.TestCase):
    def test_binary_survives_a_round_trip(self):
        item = {"id": {"S": "a"}, "blob": {"B": b"\x00\x01\xff"}, "set": {"BS": [b"x", b"\xfe"]},
                "nested": {"M": {"B": {"S": "属性名が B でも壊れない"}, "list": {"L": [{"B": b"\x02"}, {"N": "1"}]}}}}
        restored = aws.typed_from_json(json.loads(json.dumps(aws.typed_to_json(item))))
        self.assertEqual(restored, item)


class LiveTest(unittest.TestCase):
    """ここから下は、動いているエミュレーターが要る。"""

    @classmethod
    def setUpClass(cls):
        docker.restart()  # 空の状態から始める
        cls.ui = serve(server.make_server(UI_PORT))
        cls.ro = serve(server.make_server(RO_PORT, read_only=True))
        cls.px = serve(proxy.make_server(PROXY_PORT))
        cls.http = Http(UI_PORT, cls.ui.RequestHandlerClass.token)
        cls.ro_http = Http(RO_PORT, cls.ro.RequestHandlerClass.token)

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.ui, cls.ro, cls.px):
            srv.shutdown()
            srv.server_close()
        if (config.SNAPSHOTS / SNAP).exists():
            snapshot.delete(SNAP)

    # ---- 守り ----------------------------------------------------------------

    def test_wrong_host_header_is_rejected(self):
        status, _, _ = self.http.call("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_write_needs_the_token(self):
        status, body = Http(UI_PORT).post("/api/s3/create-bucket", {"bucket": "no-token"})
        self.assertEqual(status, 403)
        self.assertNotIn("no-token", [b["Name"] for b in aws.client("s3").list_buckets()["Buckets"]])

    def test_write_from_another_origin_is_rejected(self):
        status, _ = self.http.post("/api/s3/create-bucket", {"bucket": "cross-origin"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_read_only_mode_blocks_writes_but_not_reads(self):
        status, body = self.ro_http.post("/api/s3/create-bucket", {"bucket": "read-only"})
        self.assertEqual((status, body["error"]), (403, "read_only"))
        status, body = self.ro_http.post("/api/emulator/restart")
        self.assertEqual(status, 403)
        status, body = self.ro_http.get("/api/inventory")
        self.assertEqual(status, 200)

    def test_page_has_strict_headers_and_token(self):
        status, body, headers = self.http.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.http.token, body.decode())
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_html_in_s3_is_never_served_as_html(self):
        s3 = aws.client("s3")
        s3.create_bucket(Bucket="t-danger")
        s3.put_object(Bucket="t-danger", Key="x.html", Body=b"<script>alert(1)</script>", ContentType="text/html")
        s3.put_object(Bucket="t-danger", Key="x.svg", Body=b"<svg onload='alert(1)'/>", ContentType="image/svg+xml")
        status, _, headers = self.http.call("GET", "/api/s3/object", {"bucket": "t-danger", "key": "x.html"})
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        status, _, _ = self.http.call("GET", "/api/s3/image", {"bucket": "t-danger", "key": "x.html"})
        self.assertEqual(status, 400)
        status, _, _ = self.http.call("GET", "/api/s3/image", {"bucket": "t-danger", "key": "x.svg"})
        self.assertEqual(status, 400)  # SVG は画像として返さない
        status, _, headers = self.http.call("GET", "/api/s3/download", {"bucket": "t-danger", "key": "x.html"})
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", headers["Content-Disposition"])

    # ---- S3 ------------------------------------------------------------------

    def test_s3_upload_browse_download_delete(self):
        self.assertEqual(self.http.post("/api/s3/create-bucket", {"bucket": "t-files"})[0], 200)
        status, _, _ = self.http.call("POST", "/api/s3/upload", {"bucket": "t-files", "key": "docs/日本語 名.txt"},
                                      raw="こんにちは".encode(), headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.http.call("POST", "/api/s3/upload", {"bucket": "t-files", "key": "top.json"}, raw=b'{"a":1}', headers={"Content-Type": "application/json"})

        _, root = self.http.get("/api/s3/list", bucket="t-files")
        self.assertEqual(root["folders"], ["docs/"])
        self.assertEqual([o["key"] for o in root["objects"]], ["top.json"])
        _, folder = self.http.get("/api/s3/list", bucket="t-files", prefix="docs/")
        self.assertEqual([o["key"] for o in folder["objects"]], ["docs/日本語 名.txt"])
        _, flat = self.http.get("/api/s3/list", bucket="t-files", flat="1")
        self.assertEqual(len(flat["objects"]), 2)

        _, info = self.http.get("/api/s3/object", bucket="t-files", key="top.json")
        self.assertEqual((info["kind"], info["text"]), ("text", '{\n  "a": 1\n}'))
        _, data, _ = self.http.call("GET", "/api/s3/download", {"bucket": "t-files", "key": "docs/日本語 名.txt"})
        self.assertEqual(data.decode(), "こんにちは")

        self.assertEqual(self.http.post("/api/s3/delete", {"bucket": "t-files", "key": "top.json"})[0], 200)
        status, result = self.http.post("/api/s3/delete-bucket", {"bucket": "t-files"})
        self.assertEqual((status, result["objects"]), (200, 1))
        self.assertEqual(self.http.get("/api/s3/list", bucket="t-files")[0], 404)

    # ---- DynamoDB ------------------------------------------------------------

    def test_dynamodb_put_query_page_delete(self):
        ddb = aws.client("dynamodb")
        ddb.create_table(TableName="t-items", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "n", "AttributeType": "N"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "n", "KeyType": "RANGE"}])
        ddb.get_waiter("table_exists").wait(TableName="t-items", WaiterConfig={"Delay": 0.2})
        for i in range(resources.PAGE_ITEMS + 5):
            ddb.put_item(TableName="t-items", Item={"pk": {"S": "many"}, "n": {"N": str(i)}})

        item = {"pk": {"S": "one"}, "n": {"N": "1"}, "tags": {"SS": ["a", "b"]}, "blob": {"B": "AAH/"}, "price": {"N": "1.5"}}
        self.assertEqual(self.http.post("/api/ddb/put", {"table": "t-items", "item": item})[0], 200)
        stored = ddb.get_item(TableName="t-items", Key={"pk": {"S": "one"}, "n": {"N": "1"}})["Item"]
        self.assertEqual(stored["blob"], {"B": b"\x00\x01\xff"})  # base64 がバイナリに戻っている
        self.assertEqual(sorted(stored["tags"]["SS"]), ["a", "b"])  # セットがリストに化けていない

        _, one = self.http.get("/api/ddb/items", table="t-items", pk="one")
        self.assertEqual(len(one["items"]), 1)
        self.assertEqual(one["items"][0]["plain"]["price"], 1.5)
        self.assertEqual((one["keys"], one["keyTypes"]), (["pk", "n"], {"pk": "S", "n": "N"}))

        _, page1 = self.http.get("/api/ddb/items", table="t-items", pk="many")
        self.assertEqual(len(page1["items"]), resources.PAGE_ITEMS)
        _, page2 = self.http.get("/api/ddb/items", table="t-items", pk="many", start=json.dumps(page1["next"]))
        self.assertEqual((len(page2["items"]), page2["next"]), (5, None))

        self.assertEqual(self.http.post("/api/ddb/delete", {"table": "t-items", "key": {"pk": {"S": "one"}, "n": {"N": "1"}}})[0], 200)
        self.assertEqual(len(self.http.get("/api/ddb/items", table="t-items", pk="one")[1]["items"]), 0)
        self.assertEqual(self.http.post("/api/ddb/put", {"table": "t-items", "item": {"pk": {"S": "x"}}})[0], 502)  # キー不足は、AWSのエラーとして返る

    # ---- SQS -----------------------------------------------------------------

    def test_sqs_send_peek_purge(self):
        sqs = aws.client("sqs")
        url = sqs.create_queue(QueueName="t-queue")["QueueUrl"]
        for i in range(12):
            self.assertEqual(self.http.post("/api/sqs/send", {"url": url, "body": f"m{i}"})[0], 200)
        _, detail = self.http.get("/api/sqs/detail", url=url)
        self.assertEqual(sorted(m["body"] for m in detail["messages"]), sorted(f"m{i}" for i in range(12)))
        # のぞいたあとも、ほかの受信者がすぐに受け取れる
        self.assertTrue(sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, VisibilityTimeout=0).get("Messages"))
        self.assertEqual(self.http.post("/api/sqs/purge", {"url": url})[0], 200)
        self.assertEqual(self.http.get("/api/sqs/detail", url=url)[1]["messages"], [])

    # ---- 一覧と詳細 -----------------------------------------------------------

    def test_inventory_and_generic_detail(self):
        aws.client("ssm").put_parameter(Name="/t/param", Value="v", Type="SecureString", Overwrite=True)
        _, inv = self.http.get("/api/inventory")
        kinds = {k["id"]: k for k in inv["kinds"]}
        self.assertGreaterEqual(len(kinds), 20)
        self.assertEqual([k["id"] for k in inv["kinds"] if k["error"]], [])  # どの種類も一覧を取れる
        self.assertIn("/t/param", [r["id"] for r in kinds["ssm"]["rows"]])
        _, detail = self.http.get("/api/kind/detail", kind="ssm", id="/t/param")
        self.assertEqual(detail["data"]["Value"], "v")

    # ---- 中継とAPIログ --------------------------------------------------------

    def test_proxy_forwards_and_records_calls(self):
        key, secret = aws.credentials()
        session = boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret, region_name=config.REGION)
        cfg = Config(retries={"max_attempts": 1}, s3={"addressing_style": "path"})
        via = lambda name: session.client(name, endpoint_url=f"http://127.0.0.1:{PROXY_PORT}", config=cfg)

        proxy.clear_calls()
        s3 = via("s3")
        s3.create_bucket(Bucket="t-proxy")
        body = bytes(range(256)) * 300  # バイナリが壊れずに往復するか
        s3.put_object(Bucket="t-proxy", Key="a/b.bin", Body=body)
        self.assertEqual(s3.get_object(Bucket="t-proxy", Key="a/b.bin")["Body"].read(), body)
        self.assertEqual(s3.head_object(Bucket="t-proxy", Key="a/b.bin")["ContentLength"], len(body))
        s3.delete_object(Bucket="t-proxy", Key="a/b.bin")
        via("dynamodb").list_tables()
        via("sqs").list_queues()
        with self.assertRaises(ClientError) as raised:
            via("athena").list_work_groups()
        self.assertEqual(raised.exception.response["Error"]["Code"], "NotImplemented")

        _, log = self.http.get("/api/calls")
        seen = [(c["service"], c["operation"], c["status"]) for c in log["calls"]]
        for expected in [("s3", "CreateBucket", 200), ("s3", "PutObject", 200), ("s3", "GetObject", 200), ("s3", "HeadObject", 200),
                         ("s3", "DeleteObject", 204), ("dynamodb", "ListTables", 200), ("sqs", "ListQueues", 200),
                         ("athena", "ListWorkGroups", 501)]:
            self.assertIn(expected, seen)
        missing = next(c for c in log["calls"] if c["status"] == 501)
        self.assertEqual(missing["error"], "NotImplemented")
        _, detail = self.http.get("/api/calls/detail", id=missing["id"])
        self.assertIn("NotImplemented", detail["resBody"]["text"])
        self.assertNotRegex(detail["reqHeaders"]["authorization"], r"Signature=[0-9a-f]{16}")  # 署名の値は残さない

        self.assertEqual(self.http.post("/api/calls/clear")[0], 200)
        self.assertEqual(self.http.get("/api/calls")[1]["calls"], [])

    def test_proxy_rejects_wrong_host(self):
        status, _, _ = Http(PROXY_PORT).call("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    # ---- 状態と操作 -----------------------------------------------------------

    def test_state_logs_and_connect(self):
        _, state = self.http.get("/api/state")
        self.assertTrue(state["emulator"]["running"])
        self.assertRegex(state["emulator"]["version"], r"^\d+\.\d+")
        _, logs = self.http.get("/api/logs")
        self.assertTrue(any("telemetry: disabled" in e["message"] for e in logs["entries"]))
        _, connect = self.http.get("/api/connect")
        self.assertTrue(any("AWS_ENDPOINT_URL=https://127.0.0.1" in s["code"] for s in connect["sections"]))


class SnapshotTest(unittest.TestCase):
    """保存 → 空にする → 復元 で、中身が元どおりになるか。"""

    def test_round_trip(self):
        docker.restart()
        s3, ddb, sqs = aws.client("s3"), aws.client("dynamodb"), aws.client("sqs")
        s3.create_bucket(Bucket="snap-bucket")
        s3.put_object(Bucket="snap-bucket", Key="dir/日本語.txt", Body="本文".encode(), ContentType="text/plain", Metadata={"owner": "me"})
        s3.put_object(Bucket="snap-bucket", Key="bin", Body=bytes(range(256)))
        ddb.create_table(
            TableName="snap-table", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "g", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-g", "KeySchema": [{"AttributeName": "g", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}],
        )
        ddb.get_waiter("table_exists").wait(TableName="snap-table", WaiterConfig={"Delay": 0.2})
        items = [{"pk": {"S": f"k{i}"}, "g": {"S": "grp"}, "n": {"N": str(i)}, "b": {"B": bytes([i])}, "s": {"SS": ["x", "y"]}} for i in range(40)]
        for item in items:
            ddb.put_item(TableName="snap-table", Item=item)
        url = sqs.create_queue(QueueName="snap-queue", Attributes={"VisibilityTimeout": "45"})["QueueUrl"]
        sqs.send_message(QueueUrl=url, MessageBody="queued", MessageAttributes={"k": {"DataType": "String", "StringValue": "v"}})
        aws.client("ssm").put_parameter(Name="/snap/secure", Value="s3cret", Type="SecureString")
        aws.client("secretsmanager").create_secret(Name="snap/secret", SecretString='{"a":1}')
        topic = aws.client("sns").create_topic(Name="snap-topic")["TopicArn"]

        saved = snapshot.save(SNAP)
        self.assertEqual(saved["skipped"], {})
        self.assertEqual(saved["counts"], {"S3 バケット": 1, "DynamoDB テーブル": 1, "SQS キュー": 1, "SNS トピック": 1,
                                           "SSM パラメーター": 1, "シークレット": 1})
        self.assertIn(SNAP, [s["name"] for s in snapshot.listing()])

        snapshot.load(SNAP, reset=True)  # 再起動して空にしてから入れる
        s3, ddb, sqs = aws.client("s3"), aws.client("dynamodb"), aws.client("sqs")
        got = s3.get_object(Bucket="snap-bucket", Key="dir/日本語.txt")
        self.assertEqual((got["Body"].read().decode(), got["ContentType"], got["Metadata"]), ("本文", "text/plain", {"owner": "me"}))
        self.assertEqual(s3.get_object(Bucket="snap-bucket", Key="bin")["Body"].read(), bytes(range(256)))
        restored = ddb.scan(TableName="snap-table")["Items"]
        key = lambda i: i["pk"]["S"]
        normal = lambda i: {**i, "s": {"SS": sorted(i["s"]["SS"])}}
        self.assertEqual(sorted(map(normal, restored), key=key), sorted(map(normal, items), key=key))
        self.assertEqual(ddb.query(TableName="snap-table", IndexName="by-g", KeyConditionExpression="g = :g",
                                   ExpressionAttributeValues={":g": {"S": "grp"}})["Count"], 40)  # インデックスも戻っている
        url = sqs.get_queue_url(QueueName="snap-queue")["QueueUrl"]
        self.assertEqual(sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["VisibilityTimeout"])["Attributes"]["VisibilityTimeout"], "45")
        message = sqs.receive_message(QueueUrl=url, MessageAttributeNames=["All"])["Messages"][0]
        self.assertEqual((message["Body"], message["MessageAttributes"]["k"]["StringValue"]), ("queued", "v"))
        self.assertEqual(aws.client("ssm").get_parameter(Name="/snap/secure", WithDecryption=True)["Parameter"]["Value"], "s3cret")
        self.assertEqual(aws.client("secretsmanager").get_secret_value(SecretId="snap/secret")["SecretString"], '{"a":1}')
        self.assertIn(topic, [t["TopicArn"] for t in aws.client("sns").list_topics()["Topics"]])

        snapshot.load(SNAP, reset=False)  # 重ねても、失敗したり二重になったりしない
        self.assertEqual(aws.client("dynamodb").scan(TableName="snap-table", Select="COUNT")["Count"], 40)

        snapshot.delete(SNAP)
        self.assertNotIn(SNAP, [s["name"] for s in snapshot.listing()])

    def test_bad_names_are_rejected(self):
        for name in ["", "../escape", "a/b", ".hidden", "x" * 80]:
            with self.subTest(name), self.assertRaises(ValueError):
                snapshot.save(name)


if __name__ == "__main__":
    unittest.main()
