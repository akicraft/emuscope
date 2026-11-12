"""サービスごとの「一覧」と「詳細」。

1種類のリソースを KIND 1つで表す。画面はこの定義だけを見て表を作るので、
対応サービスを足すときはここに数行足せばよい。
"""

import json
from concurrent.futures import ThreadPoolExecutor

from .aws import client, collect, error_code, is_down, jsonable

MAX_ROWS = 500


def _safe(fn):
    """詳細の一部が未実装でも、取れた分は見せる。"""
    try:
        return jsonable(fn())
    except Exception as e:  # noqa: BLE001 - 何が未実装かは呼んでみるまでわからない
        if is_down(e):
            raise
        return {"取得できませんでした": error_code(e)}


def _maybe_json(text):
    if isinstance(text, str) and text.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


# ---- 一覧 -------------------------------------------------------------------
# それぞれ [{"id": 一意な値, "name": 表示名, ...列の値}] を返す。


def list_s3():
    return [
        {"id": b["Name"], "name": b["Name"], "created": b.get("CreationDate")}
        for b in client("s3").list_buckets().get("Buckets", [])
    ]


def list_dynamodb():
    ddb = client("dynamodb")
    rows = []
    for name in collect("dynamodb", "list_tables", "TableNames"):
        t = ddb.describe_table(TableName=name)["Table"]
        keys = sorted(t["KeySchema"], key=lambda k: k["KeyType"] != "HASH")
        rows.append({
            "id": name, "name": name, "status": t.get("TableStatus"),
            "keys": " / ".join(k["AttributeName"] for k in keys),
            "indexes": len(t.get("GlobalSecondaryIndexes", [])) + len(t.get("LocalSecondaryIndexes", [])),
        })
    return rows


def list_sqs():
    sqs = client("sqs")
    rows = []
    for url in collect("sqs", "list_queues", "QueueUrls"):
        a = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        ).get("Attributes", {})
        rows.append({
            "id": url, "name": url.rsplit("/", 1)[-1],
            "visible": int(a.get("ApproximateNumberOfMessages", 0)),
            "inflight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
        })
    return rows


def list_sns():
    return [{"id": t["TopicArn"], "name": t["TopicArn"].rsplit(":", 1)[-1]} for t in collect("sns", "list_topics", "Topics")]


def list_lambda():
    return [
        {"id": f["FunctionName"], "name": f["FunctionName"], "runtime": f.get("Runtime"),
         "handler": f.get("Handler"), "modified": f.get("LastModified")}
        for f in collect("lambda", "list_functions", "Functions")
    ]


def list_secrets():
    return [
        {"id": s["Name"], "name": s["Name"], "changed": s.get("LastChangedDate")}
        for s in collect("secretsmanager", "list_secrets", "SecretList")
    ]


def list_ssm():
    return [
        {"id": p["Name"], "name": p["Name"], "type": p.get("Type"), "version": p.get("Version")}
        for p in collect("ssm", "describe_parameters", "Parameters")
    ]


def list_logs():
    return [
        {"id": g["logGroupName"], "name": g["logGroupName"], "bytes": g.get("storedBytes")}
        for g in collect("logs", "describe_log_groups", "logGroups")
    ]


def list_kinesis():
    return [{"id": n, "name": n} for n in collect("kinesis", "list_streams", "StreamNames")]


def list_events():
    return [
        {"id": r["Name"], "name": r["Name"], "state": r.get("State"), "schedule": r.get("ScheduleExpression")}
        for r in collect("events", "list_rules", "Rules")
    ]


def list_stepfunctions():
    return [
        {"id": m["stateMachineArn"], "name": m["name"], "type": m.get("type"), "created": m.get("creationDate")}
        for m in collect("stepfunctions", "list_state_machines", "stateMachines")
    ]


def list_cloudformation():
    return [
        {"id": s["StackName"], "name": s["StackName"], "status": s.get("StackStatus"), "created": s.get("CreationTime")}
        for s in collect("cloudformation", "describe_stacks", "Stacks")
    ]


def list_iam_roles():
    return [{"id": r["RoleName"], "name": r["RoleName"], "created": r.get("CreateDate")} for r in collect("iam", "list_roles", "Roles")]


def list_iam_users():
    return [{"id": u["UserName"], "name": u["UserName"], "created": u.get("CreateDate")} for u in collect("iam", "list_users", "Users")]


def list_apigateway():
    return [{"id": a["id"], "name": a.get("name") or a["id"], "api_id": a["id"]} for a in collect("apigateway", "get_rest_apis", "items")]


def list_apigatewayv2():
    return [
        {"id": a["ApiId"], "name": a.get("Name") or a["ApiId"], "protocol": a.get("ProtocolType"), "api_id": a["ApiId"]}
        for a in collect("apigatewayv2", "get_apis", "Items")
    ]


def list_kms():
    return [{"id": k["KeyId"], "name": k["KeyId"]} for k in collect("kms", "list_keys", "Keys")]


def list_ecr():
    return [
        {"id": r["repositoryName"], "name": r["repositoryName"], "uri": r.get("repositoryUri")}
        for r in collect("ecr", "describe_repositories", "repositories")
    ]


def list_cognito():
    pools = client("cognito-idp").list_user_pools(MaxResults=60).get("UserPools", [])
    return [{"id": p["Id"], "name": p.get("Name") or p["Id"], "pool_id": p["Id"]} for p in pools]


def list_ses():
    return [
        {"id": i["IdentityName"], "name": i["IdentityName"], "type": i.get("IdentityType")}
        for i in collect("sesv2", "list_email_identities", "EmailIdentities")
    ]


def list_scheduler():
    return [
        {"id": s["Name"], "name": s["Name"], "group": s.get("GroupName"), "state": s.get("State")}
        for s in collect("scheduler", "list_schedules", "Schedules")
    ]


# ---- 詳細 -------------------------------------------------------------------
# それぞれ {"data": JSONにできる値, "text": 任意の読み物} を返す。


def detail_sns(arn):
    sns = client("sns")
    return {"data": {
        "属性": _safe(lambda: sns.get_topic_attributes(TopicArn=arn)["Attributes"]),
        "サブスクリプション": _safe(lambda: sns.list_subscriptions_by_topic(TopicArn=arn)["Subscriptions"]),
    }}


def detail_lambda(name):
    return {"data": _safe(lambda: client("lambda").get_function(FunctionName=name)["Configuration"])}


def detail_secret(name):
    sm = client("secretsmanager")

    def value():
        v = sm.get_secret_value(SecretId=name)
        return _maybe_json(v["SecretString"]) if "SecretString" in v else v.get("SecretBinary")

    return {"data": {"値": _safe(value), "情報": _safe(lambda: sm.describe_secret(SecretId=name))}}


def detail_ssm(name):
    return {"data": _safe(lambda: client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"])}


def detail_logs(group):
    logs = client("logs")
    streams = _safe(lambda: [s["logStreamName"] for s in logs.describe_log_streams(logGroupName=group)["logStreams"]])
    try:
        events = logs.filter_log_events(logGroupName=group, limit=500).get("events", [])
    except Exception as e:  # noqa: BLE001
        if is_down(e):
            raise
        return {"data": {"ストリーム": streams, "イベント": {"取得できませんでした": error_code(e)}}}
    import datetime

    lines = [
        f"{datetime.datetime.fromtimestamp(e['timestamp'] / 1000).strftime('%m-%d %H:%M:%S')}  "
        f"{e.get('logStreamName', '')}  {e.get('message', '').rstrip()}"
        for e in events
    ]
    return {"data": {"ストリーム": streams, "イベント数": len(events)}, "text": "\n".join(lines)}


def detail_kinesis(name):
    kin = client("kinesis")

    def records():
        out = []
        for shard in kin.list_shards(StreamName=name)["Shards"][:4]:
            it = kin.get_shard_iterator(StreamName=name, ShardId=shard["ShardId"], ShardIteratorType="TRIM_HORIZON")
            for r in kin.get_records(ShardIterator=it["ShardIterator"], Limit=25)["Records"]:
                out.append({"shard": shard["ShardId"], "partitionKey": r["PartitionKey"],
                            "sequence": r["SequenceNumber"], "data": _maybe_json(jsonable(r["Data"]))})
        return out

    return {"data": {
        "概要": _safe(lambda: kin.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]),
        "レコード（各シャードの先頭25件）": _safe(records),
    }}


def detail_events(name):
    ev = client("events")
    rule = _safe(lambda: ev.describe_rule(Name=name))
    if isinstance(rule, dict) and "EventPattern" in rule:
        rule["EventPattern"] = _maybe_json(rule["EventPattern"])
    return {"data": {"ルール": rule, "ターゲット": _safe(lambda: ev.list_targets_by_rule(Rule=name)["Targets"])}}


def detail_stepfunctions(arn):
    sfn = client("stepfunctions")
    machine = _safe(lambda: sfn.describe_state_machine(stateMachineArn=arn))
    if isinstance(machine, dict) and "definition" in machine:
        machine["definition"] = _maybe_json(machine["definition"])
    return {"data": {
        "ステートマシン": machine,
        "最近の実行": _safe(lambda: sfn.list_executions(stateMachineArn=arn, maxResults=20)["executions"]),
    }}


def detail_cloudformation(name):
    cfn = client("cloudformation")
    return {"data": {
        "スタック": _safe(lambda: cfn.describe_stacks(StackName=name)["Stacks"][0]),
        "リソース": _safe(lambda: [
            {k: r.get(k) for k in ("LogicalResourceId", "ResourceType", "ResourceStatus", "PhysicalResourceId")}
            for r in cfn.describe_stack_resources(StackName=name)["StackResources"]
        ]),
        "イベント（新しい順に30件）": _safe(lambda: [
            {k: e.get(k) for k in ("Timestamp", "LogicalResourceId", "ResourceStatus", "ResourceStatusReason")}
            for e in cfn.describe_stack_events(StackName=name)["StackEvents"][:30]
        ]),
    }}


def detail_iam_role(name):
    iam = client("iam")
    return {"data": {
        "ロール": _safe(lambda: iam.get_role(RoleName=name)["Role"]),
        "アタッチ済みポリシー": _safe(lambda: iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]),
    }}


def detail_iam_user(name):
    return {"data": _safe(lambda: client("iam").get_user(UserName=name)["User"])}


def detail_apigateway(api_id):
    ag = client("apigateway")
    return {"data": {"API": _safe(lambda: ag.get_rest_api(restApiId=api_id)),
                     "リソース": _safe(lambda: ag.get_resources(restApiId=api_id)["items"])}}


def detail_apigatewayv2(api_id):
    ag = client("apigatewayv2")
    return {"data": {"API": _safe(lambda: ag.get_api(ApiId=api_id)),
                     "ルート": _safe(lambda: ag.get_routes(ApiId=api_id)["Items"])}}


def detail_kms(key_id):
    return {"data": _safe(lambda: client("kms").describe_key(KeyId=key_id)["KeyMetadata"])}


def detail_ecr(name):
    ecr = client("ecr")
    return {"data": {"リポジトリ": _safe(lambda: ecr.describe_repositories(repositoryNames=[name])["repositories"][0]),
                     "イメージ": _safe(lambda: ecr.list_images(repositoryName=name)["imageIds"])}}


def detail_cognito(pool_id):
    idp = client("cognito-idp")
    return {"data": {"ユーザー（先頭20件）": _safe(lambda: idp.list_users(UserPoolId=pool_id, Limit=20)["Users"]),
                     "プール": _safe(lambda: idp.describe_user_pool(UserPoolId=pool_id)["UserPool"])}}


def detail_ses(name):
    return {"data": _safe(lambda: client("sesv2").get_email_identity(EmailIdentity=name))}


def detail_scheduler(name):
    return {"data": _safe(lambda: client("scheduler").get_schedule(Name=name))}


# ---- 定義 -------------------------------------------------------------------
# view: 画面の種類。"s3" "dynamodb" "sqs" は専用の画面、"generic" は一覧と詳細だけ。
# columns: 一覧の表に出す列（キー, 見出し）。

KINDS = [
    {"id": "s3", "label": "S3 バケット", "view": "s3", "list": list_s3,
     "columns": [("name", "名前"), ("created", "作成日時")]},
    {"id": "dynamodb", "label": "DynamoDB テーブル", "view": "dynamodb", "list": list_dynamodb,
     "columns": [("name", "名前"), ("keys", "キー"), ("indexes", "インデックス数"), ("status", "状態")]},
    {"id": "sqs", "label": "SQS キュー", "view": "sqs", "list": list_sqs,
     "columns": [("name", "名前"), ("visible", "待機中"), ("inflight", "処理中")]},
    {"id": "sns", "label": "SNS トピック", "view": "generic", "list": list_sns, "detail": detail_sns,
     "columns": [("name", "名前"), ("id", "ARN")]},
    {"id": "lambda", "label": "Lambda 関数", "view": "generic", "list": list_lambda, "detail": detail_lambda,
     "columns": [("name", "名前"), ("runtime", "ランタイム"), ("handler", "ハンドラー"), ("modified", "更新日時")]},
    {"id": "secretsmanager", "label": "Secrets Manager", "view": "generic", "list": list_secrets, "detail": detail_secret,
     "columns": [("name", "名前"), ("changed", "更新日時")]},
    {"id": "ssm", "label": "SSM パラメーター", "view": "generic", "list": list_ssm, "detail": detail_ssm,
     "columns": [("name", "名前"), ("type", "種類"), ("version", "版")]},
    {"id": "logs", "label": "CloudWatch Logs", "view": "generic", "list": list_logs, "detail": detail_logs,
     "columns": [("name", "ロググループ"), ("bytes", "バイト数")]},
    {"id": "kinesis", "label": "Kinesis ストリーム", "view": "generic", "list": list_kinesis, "detail": detail_kinesis,
     "columns": [("name", "名前")]},
    {"id": "events", "label": "EventBridge ルール", "view": "generic", "list": list_events, "detail": detail_events,
     "columns": [("name", "名前"), ("state", "状態"), ("schedule", "スケジュール")]},
    {"id": "stepfunctions", "label": "Step Functions", "view": "generic", "list": list_stepfunctions,
     "detail": detail_stepfunctions, "columns": [("name", "名前"), ("type", "種類"), ("created", "作成日時")]},
    {"id": "cloudformation", "label": "CloudFormation スタック", "view": "generic", "list": list_cloudformation,
     "detail": detail_cloudformation, "columns": [("name", "名前"), ("status", "状態"), ("created", "作成日時")]},
    {"id": "apigateway", "label": "API Gateway (REST)", "view": "generic", "list": list_apigateway,
     "detail": detail_apigateway, "columns": [("name", "名前"), ("api_id", "ID")]},
    {"id": "apigatewayv2", "label": "API Gateway (HTTP)", "view": "generic", "list": list_apigatewayv2,
     "detail": detail_apigatewayv2, "columns": [("name", "名前"), ("protocol", "種類"), ("api_id", "ID")]},
    {"id": "iam-roles", "label": "IAM ロール", "view": "generic", "list": list_iam_roles, "detail": detail_iam_role,
     "columns": [("name", "名前"), ("created", "作成日時")]},
    {"id": "iam-users", "label": "IAM ユーザー", "view": "generic", "list": list_iam_users, "detail": detail_iam_user,
     "columns": [("name", "名前"), ("created", "作成日時")]},
    {"id": "kms", "label": "KMS キー", "view": "generic", "list": list_kms, "detail": detail_kms,
     "columns": [("name", "キーID")]},
    {"id": "ecr", "label": "ECR リポジトリ", "view": "generic", "list": list_ecr, "detail": detail_ecr,
     "columns": [("name", "名前"), ("uri", "URI")]},
    {"id": "cognito", "label": "Cognito ユーザープール", "view": "generic", "list": list_cognito, "detail": detail_cognito,
     "columns": [("name", "名前"), ("pool_id", "ID")]},
    {"id": "ses", "label": "SES ID", "view": "generic", "list": list_ses, "detail": detail_ses,
     "columns": [("name", "名前"), ("type", "種類")]},
    {"id": "scheduler", "label": "EventBridge Scheduler", "view": "generic", "list": list_scheduler,
     "detail": detail_scheduler, "columns": [("name", "名前"), ("group", "グループ"), ("state", "状態")]},
]
BY_ID = {k["id"]: k for k in KINDS}

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="inventory")


def _list_kind(kind):
    try:
        rows = kind["list"]()[:MAX_ROWS]
        rows.sort(key=lambda r: str(r["name"]))
        return jsonable(rows), None
    except Exception as e:  # noqa: BLE001
        if is_down(e):
            raise
        return [], error_code(e)


def inventory() -> list[dict]:
    """全種類の一覧をまとめて取る。画面の左側に出す。"""
    client("sts")  # 止まっているなら、ここで EmulatorDown になる
    results = list(_pool.map(_list_kind, KINDS))
    return [
        {
            "id": kind["id"], "label": kind["label"], "view": kind["view"],
            "columns": kind["columns"], "note": kind.get("note", ""),
            "rows": rows, "error": error,
        }
        for kind, (rows, error) in zip(KINDS, results)
    ]


def detail(kind_id: str, resource_id: str) -> dict:
    kind = BY_ID[kind_id]
    if "detail" not in kind:
        raise KeyError(kind_id)
    return kind["detail"](resource_id)
