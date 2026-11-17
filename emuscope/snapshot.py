"""エミュレーターの中身を、フォルダに保存して後で戻す。

エミュレーターはメモリ上にしかデータを持たないので、止めると全部消える。
開発用のデータを毎回入れ直さずに済むよう、スナップショットとして残しておく。

保存するもの: S3、DynamoDB、SQS、SNS、SSM パラメーター、Secrets Manager。
それ以外（Lambda、CloudFormation など）は保存しない。

置き場所: snapshots/<名前>/
  manifest.json                 いつ・何を・何件
  s3/<n>/bucket.json            バケット名と、ファイルの一覧
  s3/<n>/<連番>.bin             ファイルの中身
  dynamodb/<n>.json             テーブルの定義と全項目
  sqs.json / sns.json / ssm.json / secrets.json
"""

import base64
import datetime
import json
import re
import shutil
import uuid
from pathlib import Path

from botocore.exceptions import ClientError

from . import config, docker, resources
from .aws import client, collect, error_code, typed_from_json, typed_to_json

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 作り直すときに渡せるキューの属性
SQS_ATTRS = ["VisibilityTimeout", "MessageRetentionPeriod", "DelaySeconds", "MaximumMessageSize",
             "ReceiveMessageWaitTimeSeconds", "RedrivePolicy", "Policy", "FifoQueue", "ContentBasedDeduplication"]


def _dir(name, directory=None):
    if not NAME_RE.match(name or ""):
        raise ValueError("スナップショット名は、英数字と . _ - だけで付けてください（64文字まで）。")
    return Path(directory or config.SNAPSHOTS) / name


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def _read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def listing() -> list[dict]:
    if not config.SNAPSHOTS.exists():
        return []
    out = []
    for d in sorted(config.SNAPSHOTS.iterdir()):
        manifest = d / "manifest.json"
        if manifest.exists():
            m = json.loads(manifest.read_text())
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            out.append({"name": d.name, "created": m.get("created"), "counts": m.get("counts", {}),
                        "emulator": m.get("emulator"), "bytes": size})
    return out


def delete(name) -> None:
    d = _dir(name)
    if not (d / "manifest.json").exists():
        raise KeyError(name)
    shutil.rmtree(d)


# ---- 保存 --------------------------------------------------------------------


def save(name) -> dict:
    target = _dir(name)
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    counts, skipped = {}, {}

    def step(label, fn):
        try:
            counts[label] = fn()
        except ClientError as e:  # 未実装のサービスがあっても、取れた分は保存する
            skipped[label] = error_code(e)

    step("S3 バケット", lambda: _save_s3(tmp))
    step("DynamoDB テーブル", lambda: _save_dynamodb(tmp))
    step("SQS キュー", lambda: _save_sqs(tmp))
    step("SNS トピック", lambda: _save_sns(tmp))
    step("SSM パラメーター", lambda: _save_ssm(tmp))
    step("シークレット", lambda: _save_secrets(tmp))

    manifest = {
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "emulator": docker.version(), "counts": counts, "skipped": skipped,
    }
    _write(tmp / "manifest.json", manifest)
    if target.exists():  # 同じ名前は上書き。書き終えてから入れ替えるので、途中で失敗しても前のものは残る
        shutil.rmtree(target)
    tmp.rename(target)
    return {"name": name, **manifest}


def _save_s3(root):
    s3 = client("s3")
    buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    for n, bucket in enumerate(buckets):
        folder = root / "s3" / str(n)
        folder.mkdir(parents=True)
        objects = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for o in page.get("Contents", []):
                got = s3.get_object(Bucket=bucket, Key=o["Key"])
                file = f"{len(objects):06d}.bin"  # キーには / や記号が入るので、連番のファイル名にする
                (folder / file).write_bytes(got["Body"].read())
                objects.append({"key": o["Key"], "file": file, "contentType": got.get("ContentType"),
                                "metadata": got.get("Metadata", {})})
        _write(folder / "bucket.json", {"bucket": bucket, "objects": objects})
    return len(buckets)


def _table_definition(t):
    """describe_table の結果から、create_table に渡せる引数を作る。"""
    d = {"TableName": t["TableName"], "AttributeDefinitions": t["AttributeDefinitions"], "KeySchema": t["KeySchema"]}
    on_demand = t.get("BillingModeSummary", {}).get("BillingMode") == "PAY_PER_REQUEST" or not t.get(
        "ProvisionedThroughput", {}).get("ReadCapacityUnits")

    def throughput(src):
        p = src.get("ProvisionedThroughput", {})
        return {"ReadCapacityUnits": p.get("ReadCapacityUnits") or 1, "WriteCapacityUnits": p.get("WriteCapacityUnits") or 1}

    if on_demand:
        d["BillingMode"] = "PAY_PER_REQUEST"
    else:
        d["ProvisionedThroughput"] = throughput(t)
    gsis = []
    for i in t.get("GlobalSecondaryIndexes", []):
        g = {"IndexName": i["IndexName"], "KeySchema": i["KeySchema"], "Projection": i["Projection"]}
        if not on_demand:
            g["ProvisionedThroughput"] = throughput(i)
        gsis.append(g)
    if gsis:
        d["GlobalSecondaryIndexes"] = gsis
    lsis = [{"IndexName": i["IndexName"], "KeySchema": i["KeySchema"], "Projection": i["Projection"]}
            for i in t.get("LocalSecondaryIndexes", [])]
    if lsis:
        d["LocalSecondaryIndexes"] = lsis
    if t.get("StreamSpecification"):
        d["StreamSpecification"] = t["StreamSpecification"]
    return d


def _save_dynamodb(root):
    ddb = client("dynamodb")
    tables = collect("dynamodb", "list_tables", "TableNames", limit=10000)
    for n, table in enumerate(tables):
        items = []
        for page in ddb.get_paginator("scan").paginate(TableName=table):
            items.extend(typed_to_json(i) for i in page.get("Items", []))
        definition = _table_definition(ddb.describe_table(TableName=table)["Table"])
        _write(root / "dynamodb" / f"{n}.json", {"definition": definition, "items": items})
    return len(tables)


def _save_sqs(root):
    sqs = client("sqs")
    queues = []
    for url in collect("sqs", "list_queues", "QueueUrls", limit=10000):
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"]).get("Attributes", {})
        messages = [
            {"body": m.get("Body", ""), "messageAttributes": typed_to_json(m.get("MessageAttributes", {})),
             "groupId": m.get("Attributes", {}).get("MessageGroupId")}
            for m in resources._drain(url, 1000)
        ]
        queues.append({"name": url.rsplit("/", 1)[-1], "attributes": {k: v for k, v in attrs.items() if k in SQS_ATTRS},
                       "messages": messages})
    _write(root / "sqs.json", queues)
    return len(queues)


def _save_sns(root):
    sns = client("sns")
    topics = []
    for t in collect("sns", "list_topics", "Topics", limit=10000):
        arn = t["TopicArn"]
        subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
        topics.append({"name": arn.rsplit(":", 1)[-1],
                       "subscriptions": [{"protocol": s["Protocol"], "endpoint": s["Endpoint"]} for s in subs]})
    _write(root / "sns.json", topics)
    return len(topics)


def _save_ssm(root):
    ssm = client("ssm")
    described = collect("ssm", "describe_parameters", "Parameters", limit=10000)
    params = []
    for i in range(0, len(described), 10):
        names = [p["Name"] for p in described[i:i + 10]]
        for p in ssm.get_parameters(Names=names, WithDecryption=True).get("Parameters", []):
            params.append({"name": p["Name"], "type": p["Type"], "value": p["Value"]})
    _write(root / "ssm.json", params)
    return len(params)


def _save_secrets(root):
    sm = client("secretsmanager")
    secrets = []
    for s in collect("secretsmanager", "list_secrets", "SecretList", limit=10000):
        v = sm.get_secret_value(SecretId=s["Name"])
        entry = {"name": s["Name"], "description": s.get("Description")}
        if "SecretString" in v:
            entry["string"] = v["SecretString"]
        else:
            entry["binary"] = typed_to_json(v["SecretBinary"])
        secrets.append(entry)
    _write(root / "secrets.json", secrets)
    return len(secrets)


# ---- 復元 --------------------------------------------------------------------


def _ignore(codes, fn):
    """「すでにある」は無視して先へ進む。"""
    try:
        return fn()
    except ClientError as e:
        if error_code(e) not in codes:
            raise


def load(name, reset=False, directory=None) -> dict:
    """スナップショットを戻す。

    reset=True なら、エミュレーターを再起動して空にしてから入れる（保存時と同じ状態になる）。
    reset=False なら、今の状態に重ねる（同じ名前のファイルや項目は上書き）。
    directory を渡すと、既定の置き場所（config.SNAPSHOTS）の代わりにそこから読む。
    """
    src = _dir(name, directory)
    if not (src / "manifest.json").exists():
        raise KeyError(name)
    if reset:
        docker.restart()
    else:
        docker.up(quiet=True)
    counts = {
        "S3 バケット": _load_s3(src),
        "DynamoDB テーブル": _load_dynamodb(src),
        "SQS キュー": _load_sqs(src),
        "SNS トピック": _load_sns(src),
        "SSM パラメーター": _load_ssm(src),
        "シークレット": _load_secrets(src),
    }
    return {"name": name, "reset": reset, "counts": counts}


def _load_s3(src):
    s3 = client("s3")
    folders = sorted((src / "s3").iterdir()) if (src / "s3").exists() else []
    for folder in folders:
        meta = _read(folder / "bucket.json", None)
        if not meta:
            continue
        _ignore({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}, lambda: s3.create_bucket(Bucket=meta["bucket"]))
        for o in meta["objects"]:
            s3.put_object(Bucket=meta["bucket"], Key=o["key"], Body=(folder / o["file"]).read_bytes(),
                          ContentType=o.get("contentType") or "application/octet-stream", Metadata=o.get("metadata") or {})
    return len(folders)


def _load_dynamodb(src):
    ddb = client("dynamodb")
    files = sorted((src / "dynamodb").glob("*.json")) if (src / "dynamodb").exists() else []
    for file in files:
        data = json.loads(file.read_text())
        table = data["definition"]["TableName"]
        _ignore({"ResourceInUseException"}, lambda: ddb.create_table(**data["definition"]))
        ddb.get_waiter("table_exists").wait(TableName=table, WaiterConfig={"Delay": 0.2, "MaxAttempts": 100})
        for i in range(0, len(data["items"]), 25):
            requests = [{"PutRequest": {"Item": typed_from_json(item)}} for item in data["items"][i:i + 25]]
            pending = {table: requests}
            while pending:  # 書き切れなかった分は、なくなるまで送り直す
                pending = ddb.batch_write_item(RequestItems=pending).get("UnprocessedItems", {})
    return len(files)


def _load_sqs(src):
    sqs = client("sqs")
    queues = _read(src / "sqs.json", [])
    for q in queues:
        attrs = {k: v for k, v in q["attributes"].items() if k in SQS_ATTRS}
        url = _ignore({"QueueAlreadyExists", "QueueNameExists"},
                      lambda: sqs.create_queue(QueueName=q["name"], Attributes=attrs)["QueueUrl"])
        url = url or sqs.get_queue_url(QueueName=q["name"])["QueueUrl"]
        for m in q["messages"]:
            kwargs = {"QueueUrl": url, "MessageBody": m["body"]}
            if m.get("messageAttributes"):
                kwargs["MessageAttributes"] = _message_attributes(m["messageAttributes"])
            if url.endswith(".fifo"):
                kwargs["MessageGroupId"] = m.get("groupId") or "default"
                kwargs["MessageDeduplicationId"] = uuid.uuid4().hex
            sqs.send_message(**kwargs)
    return len(queues)


def _message_attributes(attrs):
    """SQS のメッセージ属性。BinaryValue だけ base64 から戻す。"""
    out = {}
    for name, a in attrs.items():
        a = dict(a)
        if "BinaryValue" in a:
            a["BinaryValue"] = base64.b64decode(a["BinaryValue"])
        out[name] = {k: v for k, v in a.items() if k in ("DataType", "StringValue", "BinaryValue")}
    return out


def _load_sns(src):
    sns = client("sns")
    topics = _read(src / "sns.json", [])
    for t in topics:
        arn = sns.create_topic(Name=t["name"])["TopicArn"]  # 同じ名前なら既存のものが返る
        for s in t["subscriptions"]:
            sns.subscribe(TopicArn=arn, Protocol=s["protocol"], Endpoint=s["endpoint"])
    return len(topics)


def _load_ssm(src):
    ssm = client("ssm")
    params = _read(src / "ssm.json", [])
    for p in params:
        ssm.put_parameter(Name=p["name"], Type=p["type"], Value=p["value"], Overwrite=True)
    return len(params)


def _load_secrets(src):
    sm = client("secretsmanager")
    secrets = _read(src / "secrets.json", [])
    for s in secrets:
        value = {"SecretString": s["string"]} if "string" in s else {"SecretBinary": base64.b64decode(s["binary"])}
        created = _ignore({"ResourceExistsException"}, lambda: sm.create_secret(
            Name=s["name"], **({"Description": s["description"]} if s.get("description") else {}), **value))
        if created is None:
            sm.put_secret_value(SecretId=s["name"], **value)
    return len(secrets)
