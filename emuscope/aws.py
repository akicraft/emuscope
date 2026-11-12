"""エミュレーターへの接続。ここを通る限り、本物のAWSには絶対に届かない。

シェルの環境変数は一切見ない。接続先・鍵・証明書をすべて明示して渡す。
"""

import base64
import configparser
import datetime
import decimal
import threading

import boto3
from boto3.dynamodb.types import Binary
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import config

BOTO_CONFIG = Config(
    connect_timeout=2,
    read_timeout=15,
    retries={"max_attempts": 1},
    s3={"addressing_style": "path"},
)


class EmulatorDown(Exception):
    """エミュレーターが止まっている、または起動途中。"""


class NotEmulator(Exception):
    """接続先がエミュレーターではない。"""


_lock = threading.Lock()
_state = {"stamp": None, "session": None, "clients": {}}


def _session():
    """起動のたびに鍵と証明書が変わるので、認証情報ファイルの更新時刻で作り直す。"""
    try:
        stamp = config.CREDENTIALS.stat().st_mtime_ns
    except OSError:
        raise EmulatorDown() from None
    if _state["stamp"] != stamp:
        parser = configparser.ConfigParser()
        parser.read(config.CREDENTIALS)
        try:
            section = parser["default"]
            key, secret = section["aws_access_key_id"], section["aws_secret_access_key"]
        except KeyError:
            raise EmulatorDown() from None
        session = boto3.Session(aws_access_key_id=key, aws_secret_access_key=secret, region_name=config.REGION)
        # 何かを読む前に、相手がエミュレーターであることを確かめる
        sts = session.client("sts", endpoint_url=config.ENDPOINT, verify=str(config.CA), config=BOTO_CONFIG)
        account = sts.get_caller_identity()["Account"]
        if account != config.ACCOUNT:
            raise NotEmulator(account)
        _state.update(stamp=stamp, session=session, clients={})
    return _state["session"]


def client(name: str):
    with _lock:
        session = _session()
        if name not in _state["clients"]:
            _state["clients"][name] = session.client(
                name, endpoint_url=config.ENDPOINT, verify=str(config.CA), config=BOTO_CONFIG
            )
        return _state["clients"][name]


def credentials() -> tuple[str, str]:
    with _lock:
        c = _session().get_credentials()
        return c.access_key, c.secret_key


def collect(service: str, operation: str, key: str, limit: int = 500, **kwargs) -> list:
    """一覧取得を、ページをまたいで最大 limit 件まで集める。"""
    c = client(service)
    if c.can_paginate(operation):
        out = []
        for page in c.get_paginator(operation).paginate(**kwargs):
            out.extend(page.get(key, []))
            if len(out) >= limit:
                break
        return out[:limit]
    return getattr(c, operation)(**kwargs).get(key, [])[:limit]


def error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response["Error"].get("Code", "Error")
    return type(exc).__name__


def is_down(exc: Exception) -> bool:
    """接続拒否、タイムアウト、証明書の不一致など。起動途中にも起きる。"""
    return isinstance(exc, (EmulatorDown, BotoCoreError))


def jsonable(value):
    """boto3 の応答を、JSON にできる素直な値に直す。"""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items() if k != "ResponseMetadata"}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((jsonable(v) for v in value), key=str)
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, Binary):
        value = value.value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(bytes(value)).decode()
    if hasattr(value, "read"):  # StreamingBody
        return jsonable(value.read())
    return value


# --- DynamoDB の「型付きJSON」（{"S": "..."} の形）を、ファイルや画面に出せる形と相互変換する ---


def typed_to_json(value):
    """boto3 が返す型付きの値を JSON にする。バイナリだけ base64 にする。"""
    if isinstance(value, dict):
        return {k: typed_to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [typed_to_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode()
    return value


def typed_from_json(value, _type=None):
    """typed_to_json の逆。B と BS の中身を base64 からバイナリに戻す。"""
    if isinstance(value, dict):
        return {k: typed_from_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [typed_from_json(v, "B" if _type == "BS" else None) for v in value]
    if isinstance(value, str) and _type == "B":
        return base64.b64decode(value)
    return value
