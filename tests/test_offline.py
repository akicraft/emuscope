"""エミュレーターなしで走るテスト。

実行:  python -m unittest tests.test_offline -v

本物のエミュレーターの代わりに、平文HTTPの「偽の上流」を立てて、中継を通す。
"""

import collections
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from emuscope import __main__ as cli
from emuscope import proxy, report


# ---- 偽の上流 ------------------------------------------------------------------

class FakeUpstream(BaseHTTPRequestHandler):
    """操作名（X-Amz-Target の末尾）で応答を変える。

    Fail* → 400、Missing* → 501、それ以外 → 200。
    """

    protocol_version = "HTTP/1.1"

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        op = self.headers.get("X-Amz-Target", "").rsplit(".", 1)[-1]
        if op.startswith("Missing"):
            status, body = 501, {"__type": "NotImplemented", "message": f"operation {op} is scaffolded but not implemented"}
        elif op.startswith("Fail"):
            status, body = 400, {"__type": "ValidationException", "message": "bad"}
        else:
            status, body = 200, {"ok": True}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


def call(endpoint, service, operation):
    """SDK の代わりに、署名ヘッダーの形だけ真似た要求を送る。"""
    req = urllib.request.Request(endpoint + "/", data=b"{}", method="POST", headers={
        "Authorization": f"AWS4-HMAC-SHA256 Credential=AKIDTEST/20261117/us-east-1/{service}/aws4_request, "
                         "SignedHeaders=host, Signature=0123456789abcdef0123456789abcdef",
        "X-Amz-Target": f"Svc.{operation}",
        "Content-Type": "application/x-amz-json-1.1",
        "User-Agent": "Boto3/1.44.1 test",
    })
    try:
        with urllib.request.urlopen(req) as res:
            status = res.status
    except urllib.error.HTTPError as e:
        with e:
            status = e.code
    proxy.wait_idle()  # 中継は応答を返してから記録する
    return status


CHILD = """
import os, urllib.request, urllib.error
ops = {ops!r}
for service, op in ops:
    req = urllib.request.Request(os.environ["AWS_ENDPOINT_URL"] + "/", data=b"{{}}", method="POST", headers={{
        "Authorization": "AWS4-HMAC-SHA256 Credential=AKIDTEST/20261117/us-east-1/" + service + "/aws4_request, SignedHeaders=host, Signature=00",
        "X-Amz-Target": "Svc." + op}})
    try:
        urllib.request.urlopen(req).close()
    except urllib.error.HTTPError as e:
        e.close()
raise SystemExit({exit})
"""


class UpstreamTestCase(unittest.TestCase):
    def setUp(self):
        proxy.clear_calls()
        self.upstream = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream))
        self.target = ("127.0.0.1", self.upstream.server_address[1], None)

    def tearDown(self):
        stop(self.upstream)
        proxy.clear_calls()

    def make_proxy(self):
        px = start(proxy.make_server(port=0, upstream=self.target))
        self.addCleanup(stop, px)
        return proxy.url_of(px)


# ---- 中継の集計 ----------------------------------------------------------------

class ProxySummaryTest(UpstreamTestCase):
    def test_summary_groups_calls_by_operation(self):
        url = self.make_proxy()
        self.assertEqual(call(url, "dynamodb", "ListTables"), 200)
        self.assertEqual(call(url, "dynamodb", "ListTables"), 200)
        self.assertEqual(call(url, "athena", "MissingThing"), 501)
        self.assertEqual(call(url, "sqs", "FailSend"), 400)
        stats = {(s["service"], s["operation"]): s for s in proxy.summary()}
        self.assertEqual(stats[("dynamodb", "ListTables")]["calls"], 2)
        self.assertEqual(stats[("dynamodb", "ListTables")]["statuses"], {200: 2})
        self.assertEqual(stats[("athena", "MissingThing")]["errors"], {"NotImplemented": 1})
        self.assertEqual(stats[("sqs", "FailSend")]["clients"], ["Boto3 1.44.1"])

    def test_summary_survives_when_old_calls_are_dropped(self):
        url = self.make_proxy()
        with mock.patch.object(proxy, "_calls", collections.deque(maxlen=3)):
            for _ in range(10):
                call(url, "s3", "ListBuckets")
            call(url, "athena", "MissingThing")
            self.assertEqual(len(proxy.calls_after()["calls"]), 3)
        stats = {(s["service"], s["operation"]): s["calls"] for s in proxy.summary()}
        self.assertEqual(stats, {("s3", "ListBuckets"): 10, ("athena", "MissingThing"): 1})

    def test_clear_also_clears_summary(self):
        url = self.make_proxy()
        call(url, "s3", "ListBuckets")
        proxy.clear_calls()
        self.assertEqual(proxy.summary(), [])

    def test_port_zero_accepts_its_own_host_only(self):
        url = self.make_proxy()
        port = url.rsplit(":", 1)[1]
        req = urllib.request.Request(url + "/", headers={"Host": "evil.example:" + port})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req)
        raised.exception.close()
        self.assertEqual(raised.exception.code, 403)


# ---- レポート ------------------------------------------------------------------

def stat(service, operation, statuses, errors=None):
    return {"service": service, "operation": operation, "calls": sum(statuses.values()), "statuses": statuses,
            "errors": errors or {}, "clients": ["Boto3 1.44.1"], "ms": 1.0, "firstId": 1}


class ReportTest(unittest.TestCase):
    def test_result_rules(self):
        rep = report.build([
            stat("s3", "PutObject", {200: 3, 412: 1}),           # 4xx が混じっても、成功があれば動いた
            stat("athena", "ListWorkGroups", {200: 1, 501: 1}),  # 501 が1回でもあれば未実装
            stat("sqs", "SendMessage", {400: 2}),                # 成功が1回もなければ失敗
        ], "1.0.385")
        results = [(o["service"], o["result"]) for o in rep["operations"]]
        self.assertEqual(results, [("athena", "unimplemented"), ("sqs", "failed"), ("s3", "ok")])
        self.assertEqual(rep["totals"], {"calls": 8, "operations": 3, "unimplemented": 1, "failed": 1})
        self.assertEqual(rep["emulator"], "1.0.385")
        self.assertEqual([o["operation"] for o in report.unimplemented(rep)], ["ListWorkGroups"])

    def test_markdown_lists_missing_operations(self):
        rep = report.build([stat("quicksight", "ListUsers", {501: 1}, {"NotImplemented": 1}),
                            stat("s3", "ListBuckets", {200: 1})], "1.0.385")
        md = report.to_markdown(rep)
        self.assertIn("## 未実装（1）", md)
        self.assertIn("| quicksight | ListUsers | 1 | 501×1 NotImplemented |", md)
        self.assertIn("1.0.385", md)
        ok_only = report.to_markdown(report.build([stat("s3", "ListBuckets", {200: 1})], None))
        self.assertIn("未実装の操作はありませんでした。", ok_only)

    def test_compare(self):
        old = report.build([stat("athena", "ListWorkGroups", {501: 1}), stat("s3", "PutObject", {200: 1}),
                            stat("sns", "Publish", {200: 1})], "1.0.322")
        new = report.build([stat("athena", "ListWorkGroups", {200: 1}), stat("s3", "PutObject", {501: 1}),
                            stat("kms", "Encrypt", {200: 1})], "1.0.385")
        diff = report.compare(old, new)
        self.assertEqual(diff, {"implemented": [("athena", "ListWorkGroups")], "broken": [("s3", "PutObject")],
                                "added": [("kms", "Encrypt")], "removed": [("sns", "Publish")]})
        md = report.to_markdown(new, old)
        self.assertIn("実装された: athena:ListWorkGroups", md)
        self.assertIn("使われなくなった: sns:Publish", md)

    def test_json_round_trip_and_schema_check(self):
        rep = report.build([stat("s3", "ListBuckets", {200: 1})], "1.0.385")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            report.dump(rep, path)
            self.assertEqual(report.load(path), rep)
            path.write_text(json.dumps({"schema": 999}))
            with self.assertRaises(ValueError):
                report.load(path)
            md = Path(d) / "r.md"
            report.dump(rep, md)
            self.assertTrue(md.read_text().startswith("# エミュレーター対応状況"))


# ---- ./emu record -------------------------------------------------------------

class RecordCommandTest(UpstreamTestCase):
    def run_record(self, ops, exit_code=0, *flags):
        real = proxy.make_server
        child = [sys.executable, "-c", CHILD.format(ops=ops, exit=exit_code)]
        err = io.StringIO()
        with mock.patch("emuscope.docker.up", return_value=False), \
                mock.patch("emuscope.docker.version", return_value="1.0.385"), \
                mock.patch.object(proxy, "make_server", lambda port=None: real(port=port, upstream=self.target)), \
                redirect_stderr(err):
            code = cli.main(["record", *flags, "--", *child])
        return code, err.getvalue()

    def test_prints_report_and_passes_exit_code_through(self):
        code, out = self.run_record([("s3", "ListBuckets"), ("athena", "MissingThing")])
        self.assertEqual(code, 0)
        self.assertIn("| athena | MissingThing |", out)
        self.assertIn("1.0.385", out)

    def test_child_failure_wins(self):
        code, _ = self.run_record([("athena", "MissingThing")], 5, "--strict")
        self.assertEqual(code, 5)

    def test_strict_fails_on_missing_operations(self):
        code, out = self.run_record([("athena", "MissingThing")], 0, "--strict")
        self.assertEqual(code, cli.STRICT_EXIT)
        self.assertIn("athena:MissingThing", out)
        code, _ = self.run_record([("s3", "ListBuckets")], 0, "--strict")
        self.assertEqual(code, 0)

    def test_writes_json_and_compares(self):
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / "first.json"
            self.run_record([("athena", "MissingThing")], 0, "--out", str(first))
            self.assertEqual(report.load(first)["totals"]["unimplemented"], 1)
            proxy.clear_calls()
            second = Path(d) / "second.md"
            self.run_record([("kms", "Encrypt")], 0, "--out", str(second), "--compare", str(first))
            text = second.read_text()
            self.assertIn("新しく使われた: kms:Encrypt", text)
            self.assertIn("使われなくなった: athena:MissingThing", text)

    def test_report_without_ui_explains_what_to_do(self):
        err = io.StringIO()
        with mock.patch("emuscope.config.UI_URL", "http://127.0.0.1:9"), redirect_stderr(err):
            code = cli.main(["report"])
        self.assertEqual(code, 1)
        self.assertIn("./emu record", err.getvalue())


# ---- pytest プラグイン ----------------------------------------------------------

try:
    import pytest  # noqa: F401
    HAVE_PYTEST = True
except ImportError:
    HAVE_PYTEST = False

ROOT = Path(__file__).resolve().parent.parent

# 一時フォルダに置く conftest.py。Docker とエミュレーターを使う部分を差し替え、偽の上流を立てる
PLUGIN_CONFTEST = '''
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import emuscope.docker, emuscope.testing

class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        op = self.headers.get("X-Amz-Target", "").rsplit(".", 1)[-1]
        status = 501 if op.startswith("Missing") else 200
        data = json.dumps({"__type": "NotImplemented"} if status == 501 else {}).encode()
        self.send_response(status); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass

_up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=_up.serve_forever, daemon=True).start()
emuscope.testing.RECORD_UPSTREAM = ("127.0.0.1", _up.server_address[1], None)
LOADED = []
for target, value in [("emuscope.docker.up", False), ("emuscope.docker.version", "1.0.385"),
                      ("emuscope.testing._guarded_client", None)]:
    mock.patch(target, return_value=value).start()
mock.patch("emuscope.testing.load_snapshot", lambda name, **kw: LOADED.append(name)).start()
'''

PLUGIN_TESTS = '''
import os, urllib.request, urllib.error
import pytest
import conftest

def hit(op):
    req = urllib.request.Request(os.environ["AWS_ENDPOINT_URL"] + "/", data=b"{}", method="POST", headers={
        "Authorization": "AWS4-HMAC-SHA256 Credential=AK/20261117/us-east-1/athena/aws4_request, SignedHeaders=host, Signature=00",
        "X-Amz-Target": "Svc." + op})
    try:
        urllib.request.urlopen(req).close()
    except urllib.error.HTTPError as e:
        e.close()

def test_calls(emulator):
    if os.environ["EMU_TEST_OP"]:  # 記録しないときの接続先は本物のエミュレーターなので、呼ばない
        hit(os.environ["EMU_TEST_OP"])

@pytest.mark.emu_snapshot("base")
def test_snapshot_marker(emu_client):
    assert conftest.LOADED == ["base"]
    assert callable(emu_client)
'''


@unittest.skipUnless(HAVE_PYTEST, "pytest が入っていない")
class PytestPluginTest(unittest.TestCase):
    def run_pytest(self, *args, op="ListWorkGroups"):
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "conftest.py").write_text(PLUGIN_CONFTEST)
            (Path(d) / "test_sample.py").write_text(PLUGIN_TESTS)
            env = {**os.environ, "PYTHONPATH": str(ROOT), "EMU_TEST_OP": op}
            r = subprocess.run([sys.executable, "-m", "pytest", "-p", "emuscope.pytest_plugin", "-W", "error::pytest.PytestUnknownMarkWarning",
                                "-q", "-p", "no:cacheprovider", *args], cwd=d, env=env, capture_output=True, text=True, timeout=120)
            files = {p.name: p.read_text() for p in Path(d).glob("report.*")}
        return r.returncode, r.stdout + r.stderr, files

    def test_plain_run_passes(self):
        code, out, _ = self.run_pytest(op="")
        self.assertEqual(code, 0, out)

    def test_strict_fails_on_missing_operations(self):
        code, out, _ = self.run_pytest("--emu-strict", op="MissingThing")
        self.assertEqual(code, 3, out)
        self.assertIn("athena:MissingThing", out)
        code, out, _ = self.run_pytest("--emu-strict")
        self.assertEqual(code, 0, out)

    def test_report_is_written(self):
        code, out, files = self.run_pytest("--emu-report", "report.json", op="MissingThing")
        self.assertEqual(code, 0, out)  # --emu-strict がなければ落とさない
        data = json.loads(files["report.json"])
        self.assertEqual(data["totals"]["unimplemented"], 1)
        self.assertIn("athena:MissingThing", out)


if __name__ == "__main__":
    unittest.main()
