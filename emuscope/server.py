"""画面と、画面が呼ぶAPIを出すWebサーバー。

守っていること:
  - 127.0.0.1 だけで待ち受ける（同じLANの他の機器からは見えない）
  - Host ヘッダーが 127.0.0.1 / localhost のリクエストだけ受ける（DNSリバインディング対策）
  - 書き込み（POST）には、起動ごとに変わる合言葉（トークン）を要求する。
    合言葉はページのHTMLに埋めてあり、別のサイトからは読めない（CSRF対策）
  - S3 などから来たデータを、ブラウザにHTMLとして解釈させない
"""

import hmac
import json
import mimetypes
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from botocore.exceptions import ClientError

from . import __version__, catalog, config, docker, proxy, report, resources, snapshot
from .aws import EmulatorDown, NotEmulator, is_down

STATIC = Path(__file__).parent / "static"
MAX_JSON = 2 * 1024 * 1024
MAX_UPLOAD = 100 * 1024 * 1024

# ページが読み込めるものを自分自身だけに絞る。外部のスクリプトや通信は一切許さない。
PAGE_CSP = "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"
DATA_CSP = "default-src 'none'; sandbox"

_control_lock = threading.Lock()  # 起動・停止・復元は同時に走らせない


class ApiError(Exception):
    def __init__(self, status, kind, message):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def emulator_state():
    running = docker.healthy()
    return {
        "running": running, "endpoint": config.ENDPOINT,
        "version": docker.version() if running else None,
        "startedAt": docker.started_at() if running else None,
    }


def connect_info(proxy_enabled):
    """「つなぎ方」のページに出す内容。"""
    def exports(env):
        return "\n".join(f"export {k}={v}" for k, v in env.items()) + "\nunset " + " ".join(config.UNSET_VARS)

    emu = "./emu"
    sections = [
        {"title": "いちばん簡単: コマンドの前に付ける",
         "body": "環境変数を自分で設定しなくても、エミュレーターに向けてコマンドを実行できます。止まっていれば自動で起動します。",
         "code": f"{emu} run -- aws s3 ls\n{emu} run -- python my_script.py\n{emu} run -- npm test"},
    ]
    if proxy_enabled:
        sections.append(
            {"title": "呼び出しをAPIログに記録する",
             "body": "中継サーバー経由にすると、アプリが投げた呼び出しがこの画面の「APIログ」に出ます。"
                     "未実装（501）の操作を探すのに使えます。中継は HTTP なので、証明書の設定も要りません。",
             "code": f"{emu} run --log -- python my_script.py\n\n# 今のシェルごと切り替える場合\neval \"$({emu} env --log)\""})
    sections += [
        {"title": "今のシェルをエミュレーターに向ける",
         "body": "このシェルで打つ aws コマンドや、ここから起動したプログラムが、すべてエミュレーターに向きます。ターミナルを閉じれば元に戻ります。",
         "code": f"eval \"$({emu} env)\""},
        {"title": "設定される環境変数（直接つなぐ場合）",
         "body": "エンドポイントは HTTPS で、証明書は自己署名です。AWS CLI v2・Boto3・Go の SDK は AWS_CA_BUNDLE を読みます。"
                 "S3 は localhost ではなく 127.0.0.1 を使ってください。",
         "code": exports(config.env_vars())},
    ]
    if proxy_enabled:
        sections.append({"title": "設定される環境変数（中継を通す場合）",
                         "body": "違いは AWS_ENDPOINT_URL だけです。", "code": exports(config.env_vars(proxy=True))})
    sections += [
        {"title": "Boto3（Python）",
         "body": "上の環境変数があれば、コードは本番と同じでかまいません。エンドポイントや鍵をコードに書く必要はありません。",
         "code": 'import boto3\n\ns3 = boto3.client("s3")\nprint(s3.list_buckets()["Buckets"])'},
        {"title": "テストで使う（unittest）",
         "body": "emuscope.testing を使うと、テストの前にエミュレーターを起動し、接続先を切り替えます。reset() で中身を空にできます。",
         "code": "from emuscope.testing import EmulatorTestCase\n\n\nclass MyTest(EmulatorTestCase):\n    def test_bucket(self):\n"
                 '        s3 = self.client("s3")\n        s3.create_bucket(Bucket="demo")\n'
                 '        self.assertIn("demo", [b["Name"] for b in s3.list_buckets()["Buckets"]])\n\n'
                 f"# 実行: {emu} run -- python -m unittest"},
        {"title": "ほかのSDK（未検証）",
         "body": "AWSのドキュメントによると、証明書の渡し方がSDKごとに違います。ここでは動作を確かめていません。"
                 "中継を通す（HTTP）なら、証明書の設定は不要なはずです。",
         "code": f"# JavaScript v3\nexport NODE_EXTRA_CA_CERTS={config.CA}\n\n# Rust / .NET\nexport SSL_CERT_FILE={config.CA}"},
    ]
    return {"sections": sections, "root": str(config.ROOT)}


class Handler(BaseHTTPRequestHandler):
    # make_server がサーバーごとに設定する
    allowed_hosts = frozenset()
    allowed_origins = frozenset()
    token = ""
    read_only = False
    proxy_enabled = True

    # ---- 入口 ----------------------------------------------------------------

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def _dispatch(self, handler):
        if self.headers.get("Host") not in self.allowed_hosts:
            return self._json(403, {"error": "forbidden", "message": "このホスト名では使えません"})
        url = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        try:
            handler(url.path, query)
        except ApiError as e:
            self._json(e.status, {"error": e.kind, "message": e.message})
        except KeyError as e:
            self._json(400, {"error": "bad_request", "message": f"指定が足りないか、見つかりません（{e}）"})
        except ValueError as e:
            self._json(400, {"error": "bad_request", "message": str(e)})
        except NotEmulator as e:
            self._json(500, {"error": "not_emulator", "message": f"接続先がエミュレーターではありません（アカウント {e}）"})
        except docker.DockerError as e:
            self._json(500, {"error": "docker", "message": str(e)})
        except ClientError as e:
            err = e.response["Error"]
            code = err.get("Code", "Error")
            missing = code in ("NoSuchBucket", "NoSuchKey", "404", "ResourceNotFoundException",
                               "AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist")
            self._json(404 if missing else 502, {"error": "gone" if missing else "aws_error",
                                                 "message": f"{code}: {err.get('Message', '')}".rstrip(": ")})
        except Exception as e:  # noqa: BLE001
            if is_down(e) or isinstance(e, EmulatorDown):
                self._json(503, {"error": "down", "message": "エミュレーターに接続できません"})
            else:
                self._json(500, {"error": "internal", "message": f"{type(e).__name__}: {e}"})

    # ---- 読み取り ------------------------------------------------------------

    def _get(self, path, q):
        if path == "/":
            html = (STATIC / "index.html").read_text().replace("__EMU_TOKEN__", self.token)
            return self._send(200, "text/html; charset=utf-8", html.encode(), PAGE_CSP)
        if path in ("/static/app.js", "/static/app.css"):
            file = STATIC / path.rsplit("/", 1)[-1]
            return self._send(200, mimetypes.guess_type(file.name)[0] + "; charset=utf-8", file.read_bytes(), DATA_CSP)

        if path == "/api/state":
            return self._json(200, {
                "emulator": emulator_state(), "readOnly": self.read_only, "version": __version__,
                "proxy": {"enabled": self.proxy_enabled, "endpoint": config.PROXY_ENDPOINT},
            })
        if path == "/api/inventory":
            return self._json(200, {"kinds": catalog.inventory()})
        if path == "/api/kind/detail":
            return self._json(200, catalog.detail(q["kind"], q["id"]))
        if path == "/api/s3/list":
            return self._json(200, resources.s3_list(q["bucket"], q.get("prefix", ""), q.get("flat") == "1", q.get("token")))
        if path == "/api/s3/object":
            return self._json(200, resources.s3_object(q["bucket"], q["key"]))
        if path == "/api/s3/image":
            content_type, data = resources.s3_image(q["bucket"], q["key"])
            return self._send(200, content_type, data, DATA_CSP)
        if path == "/api/s3/download":
            name = urllib.parse.quote(q["key"].rsplit("/", 1)[-1] or "download")
            # 種類は必ず「ただのバイト列」にして保存させる。ブラウザに中身を開かせない。
            return self._send(200, "application/octet-stream", resources.s3_download(q["bucket"], q["key"]), DATA_CSP,
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
        if path == "/api/ddb/items":
            start = json.loads(q["start"]) if q.get("start") else None
            return self._json(200, resources.ddb_items(q["table"], q.get("pk"), start))
        if path == "/api/sqs/detail":
            return self._json(200, resources.sqs_detail(q["url"]))
        if path == "/api/calls":
            return self._json(200, proxy.calls_after(int(q.get("after", 0))))
        if path == "/api/calls/detail":
            return self._json(200, proxy.call_detail(int(q["id"])))
        if path == "/api/calls/summary":
            return self._json(200, report.build(proxy.summary(), docker.version()))
        if path == "/api/calls/report":
            rep = report.build(proxy.summary(), docker.version())
            if q.get("format") == "json":
                data, name = json.dumps(rep, ensure_ascii=False, indent=2).encode(), "emulator-report.json"
            else:
                data, name = report.to_markdown(rep).encode(), "emulator-report.md"
            # 画面の中では開かせず、ファイルとして保存させる
            return self._send(200, "application/octet-stream", data, DATA_CSP,
                              {"Content-Disposition": f"attachment; filename={name}"})
        if path == "/api/logs":
            return self._json(200, {"entries": docker.logs(min(int(q.get("tail", 400)), 2000))})
        if path == "/api/snapshots":
            return self._json(200, {"snapshots": snapshot.listing(), "folder": str(config.SNAPSHOTS)})
        if path == "/api/connect":
            return self._json(200, connect_info(self.proxy_enabled))
        raise ApiError(404, "not_found", "そのURLはありません")

    # ---- 書き込み ------------------------------------------------------------

    def _post(self, path, q):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.allowed_origins:
            raise ApiError(403, "forbidden", "このページ以外からの操作は受け付けません")
        if not hmac.compare_digest(self.headers.get("X-Emu-Token", ""), self.token):
            raise ApiError(403, "forbidden", "合言葉が違います。ページを読み込み直してください")

        if path == "/api/calls/clear":  # 画面の表示を消すだけなので、読み取り専用でも許す
            proxy.clear_calls()
            return self._json(200, {"ok": True})
        if self.read_only:
            raise ApiError(403, "read_only", "読み取り専用モードで起動しています")

        length = int(self.headers.get("Content-Length") or 0)
        if path == "/api/s3/upload":
            if length > MAX_UPLOAD:
                raise ApiError(413, "too_large", "画面からのアップロードは 100MB までです")
            body = self.rfile.read(length)
            return self._json(200, resources.s3_upload(q["bucket"], q["key"], body, self.headers.get("Content-Type")))

        if length > MAX_JSON:
            raise ApiError(413, "too_large", "リクエストが大きすぎます")
        b = json.loads(self.rfile.read(length) or b"{}")

        if path == "/api/s3/delete":
            return self._json(200, resources.s3_delete(b["bucket"], b["key"]))
        if path == "/api/s3/create-bucket":
            return self._json(200, resources.s3_create_bucket(b["bucket"]))
        if path == "/api/s3/delete-bucket":
            return self._json(200, resources.s3_delete_bucket(b["bucket"]))
        if path == "/api/ddb/put":
            return self._json(200, resources.ddb_put(b["table"], b["item"]))
        if path == "/api/ddb/delete":
            return self._json(200, resources.ddb_delete(b["table"], b["key"]))
        if path == "/api/ddb/delete-table":
            return self._json(200, resources.ddb_delete_table(b["table"]))
        if path == "/api/sqs/send":
            return self._json(200, resources.sqs_send(b["url"], b["body"], b.get("groupId")))
        if path == "/api/sqs/purge":
            return self._json(200, resources.sqs_purge(b["url"]))
        if path == "/api/sqs/delete-queue":
            return self._json(200, resources.sqs_delete_queue(b["url"]))

        if path in ("/api/emulator/start", "/api/emulator/stop", "/api/emulator/restart"):
            with _control_lock:
                {"start": lambda: docker.up(quiet=True), "stop": docker.down, "restart": docker.restart}[path.rsplit("/", 1)[-1]]()
            return self._json(200, {"emulator": emulator_state()})
        if path == "/api/snapshots/save":
            with _control_lock:
                return self._json(200, snapshot.save(b["name"]))
        if path == "/api/snapshots/load":
            with _control_lock:
                return self._json(200, snapshot.load(b["name"], bool(b.get("reset"))))
        if path == "/api/snapshots/delete":
            snapshot.delete(b["name"])
            return self._json(200, {"ok": True})
        raise ApiError(404, "not_found", "そのURLはありません")

    # ---- 応答 ----------------------------------------------------------------

    def _json(self, status, payload):
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode(), DATA_CSP)

    def _send(self, status, content_type, body, csp, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")  # 種類を勝手に推測させない
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 数秒ごとの更新でログが埋まるので出さない


def make_server(port=None, read_only=False, proxy_enabled=True):
    port = port or config.UI_PORT
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    handler = type("EmuHandler", (Handler,), {
        "allowed_hosts": frozenset(hosts),
        "allowed_origins": frozenset(f"http://{h}" for h in hosts),
        "token": secrets.token_urlsafe(24),
        "read_only": read_only,
        "proxy_enabled": proxy_enabled,
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)  # 自分のMacからだけ見える
