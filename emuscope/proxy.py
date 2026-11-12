"""APIログ用の中継サーバー。

アプリ → http://127.0.0.1:9299（ここ）→ https://127.0.0.1:9297（エミュレーター）

リクエストは一切書き換えずに流す。署名は Host ヘッダーごと検証されるので、
Host もクライアントが送ってきたまま渡す（書き換えると署名が合わなくなる）。

通った呼び出しを「どのサービスの・どの操作が・どう返ったか」の形で覚えておき、
画面の「APIログ」に出す。未実装（501）の操作がひと目でわかる。
"""

import collections
import http.client
import itertools
import json
import re
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import botocore.session

from . import config

MAX_CALLS = 2000  # 覚えておく件数。超えたら古いものから捨てる
SNIPPET = 64 * 1024  # 本文は先頭 64KB まで覚える

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "expect"}


# ---- 操作名の逆引き ------------------------------------------------------------
# JSON 系のサービスは X-Amz-Target ヘッダー、Query 系は Action パラメーターに操作名が入っている。
# REST 系（S3、Lambda など）は入っていないので、botocore のAPI定義にある
# 「メソッド + URLの形」と突き合わせて求める。

# 署名に使われるサービス名 → botocore のサービス名（違うものだけ）
SIGNING_ALIASES = {
    "ses": ["sesv2", "ses"],
    "apigateway": ["apigateway", "apigatewayv2"],
    "states": ["stepfunctions"],
    "monitoring": ["cloudwatch"],
    "es": ["opensearch"],
    "elasticloadbalancing": ["elbv2"],
}

_loader = botocore.session.get_session().get_component("data_loader")
_rest_rules = {}
_rules_lock = threading.Lock()


def _rules_for(signing_name):
    """そのサービスの REST 操作を [(メソッド, パスの正規表現, 必須クエリ, 必須ヘッダー, 具体度, 操作名)] にする。"""
    with _rules_lock:
        if signing_name in _rest_rules:
            return _rest_rules[signing_name]
        rules = []
        for service in SIGNING_ALIASES.get(signing_name, [signing_name]):
            try:
                model = _loader.load_service_model(service, "service-2")
            except Exception:  # noqa: BLE001 - 定義がないサービスは逆引きしないだけ
                continue
            for name, op in model.get("operations", {}).items():
                http_def = op.get("http", {})
                uri = http_def.get("requestUri", "/")
                path, _, query = uri.partition("?")
                literal = len(re.sub(r"\{[^}]+\}", "", path))
                pattern = re.escape(path)
                pattern = re.sub(r"\\\{[^}]*?\\\+\\\}", "(.+)", pattern)  # {Key+} はスラッシュも含む
                pattern = re.sub(r"\\\{[^}]*?\\\}", "([^/]+)", pattern)
                required = {}
                for part in filter(None, query.split("&")):
                    k, _, v = part.partition("=")
                    if k != "x-id":  # SDK によっては付けないので、必須にしない
                        required[k] = v or None
                # URL の形が同じ操作（PutObject と CopyObject など）は、必須のクエリやヘッダーで見分ける
                need_headers = set()
                shape = model.get("shapes", {}).get(op.get("input", {}).get("shape"), {})
                for member in shape.get("required", []):
                    m = shape.get("members", {}).get(member, {})
                    if m.get("location") == "querystring" and m.get("locationName"):
                        required.setdefault(m["locationName"], None)
                    elif m.get("location") == "header" and m.get("locationName"):
                        need_headers.add(m["locationName"].lower())
                score = (len(required) + len(need_headers), literal)
                rules.append((http_def.get("method", "POST"), re.compile(f"^{pattern}/?$"), required, need_headers, score, name))
        rules.sort(key=lambda r: r[4], reverse=True)  # 条件の多いものから試す
        _rest_rules[signing_name] = rules
        return rules


def resolve_operation(signing_name, method, path, query, headers, body):
    target = headers.get("x-amz-target")
    if target:
        return target.rsplit(".", 1)[-1]
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    if "Action" in params:
        return params["Action"][0]
    if method == "POST" and "x-www-form-urlencoded" in headers.get("content-type", ""):
        form = urllib.parse.parse_qs(body[:4096].decode("latin-1"), keep_blank_values=True)
        if "Action" in form:
            return form["Action"][0]
    m = re.match(r"^/service/[^/]+/operation/([^/?]+)", path)  # Smithy RPC v2
    if m:
        return m.group(1)
    if "x-id" in params:  # SDK が操作名を付けてくれている場合
        return params["x-id"][0]
    for rule_method, pattern, required, need_headers, _, name in _rules_for(signing_name):
        if rule_method != method or not pattern.match(path):
            continue
        if all(k in params and (v is None or params[k][0] == v) for k, v in required.items()) and all(
                h in headers for h in need_headers):
            return name
    return f"{method} {path}"


def signing_name(headers, query):
    """Authorization ヘッダー（または署名付きURL）から、どのサービス宛てかを読む。"""
    m = re.search(r"Credential=[^/]+/\d+/[^/]+/([^/]+)/aws4_request", headers.get("authorization", ""))
    if not m:
        cred = urllib.parse.parse_qs(query).get("X-Amz-Credential", [""])[0]
        m = re.search(r"^[^/]+/\d+/[^/]+/([^/]+)/aws4_request", cred)
    return m.group(1) if m else ""


def error_code(status, headers, body):
    if status < 400:
        return ""
    code = headers.get("x-amzn-errortype", "").split(":")[0]
    if code:
        return code
    text = body[:2048].decode("utf-8", errors="replace")
    m = re.search(r'"(?:__type|code|Code)"\s*:\s*"([^"]+)"', text) or re.search(r"<Code>([^<]+)</Code>", text)
    return m.group(1).rsplit("#", 1)[-1] if m else ""


def client_name(user_agent):
    """User-Agent から、どのSDKやツールが呼んだかを短く表す。"""
    m = re.match(r"(aws-cli|Boto3|aws-sdk-[a-z0-9-]+|APN|Terraform|HashiCorp Terraform)[/ ]?([\w.]+)?", user_agent or "")
    return " ".join(filter(None, m.groups())) if m else (user_agent or "").split(" ")[0][:40]


# ---- 記録 --------------------------------------------------------------------

_calls = collections.deque(maxlen=MAX_CALLS)
_calls_lock = threading.Lock()
_ids = itertools.count(1)


def _record(entry):
    with _calls_lock:
        entry["id"] = next(_ids)
        _calls.append(entry)


def calls_after(after_id=0):
    """一覧用。本文は含めない。"""
    with _calls_lock:
        rows = [
            {k: c[k] for k in ("id", "time", "service", "operation", "method", "path", "status", "error", "ms",
                               "reqBytes", "resBytes", "client")}
            for c in _calls if c["id"] > after_id
        ]
        last = _calls[-1]["id"] if _calls else 0
    return {"calls": rows, "lastId": last}


def call_detail(call_id):
    with _calls_lock:
        call = next((c for c in _calls if c["id"] == call_id), None)
    if call is None:
        raise KeyError(call_id)
    out = dict(call)
    out["reqBody"] = _show_body(call["reqBody"], call["reqHeaders"].get("content-type", ""), call["reqBytes"])
    out["resBody"] = _show_body(call["resBody"], call["resHeaders"].get("content-type", ""), call["resBytes"])
    return out


def clear_calls():
    with _calls_lock:
        _calls.clear()


def _show_body(data, content_type, total):
    """覚えておいた本文を、読める形にする。"""
    if not data:
        return {"kind": "empty"}
    truncated = total > len(data)
    if b"\x00" in data[:1024]:
        return {"kind": "binary", "bytes": total}
    text = data.decode("utf-8", errors="replace")
    if "x-www-form-urlencoded" in content_type:
        text = "\n".join(f"{k} = {v}" for k, v in urllib.parse.parse_qsl(text, keep_blank_values=True))
    elif not truncated and text.lstrip()[:1] in ("{", "["):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    return {"kind": "text", "text": text, "truncated": truncated, "bytes": total}


# ---- 中継 --------------------------------------------------------------------

_ctx = {"stamp": None, "value": None}
_ctx_lock = threading.Lock()


def _ssl_context():
    """エミュレーターの証明書は起動のたびに変わるので、ファイルが変わったら読み直す。"""
    stamp = config.CA.stat().st_mtime_ns
    with _ctx_lock:
        if _ctx["stamp"] != stamp:
            _ctx.update(stamp=stamp, value=ssl.create_default_context(cafile=str(config.CA)))
        return _ctx["value"]


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    allowed_hosts = frozenset()

    def _handle(self):
        started = time.time()
        # 宛先が 127.0.0.1 と明記されたリクエストだけ受ける（DNSリバインディング対策）
        if self.headers.get("Host") not in self.allowed_hosts:
            return self._reply(403, {"Content-Type": "application/json"},
                               b'{"__type":"Forbidden","message":"emuscope proxy: unexpected Host header"}')

        body = self._read_body()
        path, _, query = self.path.partition("?")
        headers = {k.lower(): v for k, v in self.headers.items()}
        service = signing_name(headers, query)

        try:
            status, res_headers, res_body = self._forward(body)
        except (OSError, http.client.HTTPException):
            status, res_headers = 502, [("Content-Type", "application/json")]
            res_body = json.dumps({"__type": "EmulatorDown",
                                   "message": "emuscope proxy: エミュレーターに接続できません。./emu up で起動してください。"}).encode()

        self._reply(status, res_headers, res_body)
        res_lower = {k.lower(): v for k, v in (res_headers.items() if isinstance(res_headers, dict) else res_headers)}
        if "authorization" in headers:  # 署名の値そのものは覚えない
            headers["authorization"] = re.sub(r"Signature=\w+", "Signature=…", headers["authorization"])
        _record({
            "time": started, "service": service, "method": self.command, "path": path[:300], "query": query[:1000],
            "operation": resolve_operation(service, self.command, path, query, headers, body),
            "status": status, "error": error_code(status, res_lower, res_body),
            "ms": round((time.time() - started) * 1000, 1),
            "reqBytes": len(body), "resBytes": len(res_body),
            "client": client_name(headers.get("user-agent", "")),
            "reqHeaders": headers, "resHeaders": res_lower,
            "reqBody": body[:SNIPPET], "resBody": res_body[:SNIPPET],
        })

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _handle

    def _read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.read(2)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _forward(self, body):
        conn = http.client.HTTPSConnection("127.0.0.1", config.PORT, context=_ssl_context(), timeout=120)
        try:
            # Host を含め、ヘッダーは受け取ったまま送る
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in self.headers.items():
                if name.lower() not in HOP_BY_HOP and name.lower() != "content-length":
                    conn.putheader(name, value)
            # Content-Length は署名に含まれることがある（本文が空の DELETE でも）。受け取っていたら必ず渡す
            if body or self.headers.get("Content-Length") is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            res = conn.getresponse()
            return res.status, res.getheaders(), res.read()
        finally:
            conn.close()

    def _reply(self, status, headers, body):
        items = headers.items() if isinstance(headers, dict) else headers
        no_body = self.command == "HEAD" or status in (204, 304) or status < 200
        self.send_response(status)
        for name, value in items:
            lower = name.lower()
            if lower in HOP_BY_HOP or (lower == "content-length" and not no_body):
                continue
            self.send_header(name, value)
        if not no_body:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not no_body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def make_server(port=None):
    port = port or config.PROXY_PORT
    handler = type("EmuProxyHandler", (ProxyHandler,), {
        "allowed_hosts": frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}),
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)  # 自分のMacからだけ届く
