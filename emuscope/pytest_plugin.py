"""pytest からエミュレーターを使うためのプラグイン。

pip で入れた場合は自動で読み込まれる。クローンして使う場合は conftest.py に1行書く。

    pytest_plugins = ["emuscope.pytest_plugin"]

フィクスチャ:
    emulator     セッションの最初に一度だけ、SDK をエミュレーターに向ける
    emu_client   boto3.client を作る関数。emu_client("s3") のように使う
    emu_reset    エミュレーターを再起動して、空の状態から始める

マーカー:
    @pytest.mark.emu_snapshot("base")   そのテストの前に、保存した状態に戻す

オプション:
    --emu-report PATH   終わったら対応状況レポートを書く（.json なら JSON、それ以外は Markdown）
    --emu-strict        未実装（501）の操作を呼んでいたら、最後に失敗にする（終了コード 3）
"""

import os

import boto3
import pytest

from . import config as emu_config
from . import report as _report
from . import testing

STRICT_EXIT = 3
_KEY = pytest.StashKey[dict]()


def pytest_addoption(parser):
    group = parser.getgroup("emuscope", "AWS Emulator（emuscope）")
    group.addoption("--emu-report", metavar="PATH", help="対応状況レポートの書き出し先（.json なら JSON、それ以外は Markdown）")
    group.addoption("--emu-strict", action="store_true", help="未実装の操作を呼んでいたら失敗にする")


def pytest_configure(config):
    config.addinivalue_line("markers", "emu_snapshot(name): そのテストの前に、保存したスナップショットに戻す")
    config.stash[_KEY] = {"missing": []}


def _wants_record(config):
    return bool(config.getoption("--emu-report") or config.getoption("--emu-strict"))


@pytest.fixture(scope="session")
def emulator(pytestconfig):
    """SDK をエミュレーターに向ける。--emu-report か --emu-strict があれば、呼び出しを集計する。"""
    testing.use_emulator(proxy="record" if _wants_record(pytestconfig) else False)
    yield


@pytest.fixture
def emu_client(emulator, request):
    marker = request.node.get_closest_marker("emu_snapshot")
    if marker:
        testing.load_snapshot(*marker.args, **marker.kwargs)

    def make(service, **kwargs):
        # スナップショットの読み込みや reset で鍵が変わるので、毎回作る
        return boto3.client(service, **kwargs)

    return make


@pytest.fixture
def emu_reset(emulator):
    testing.reset()


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not testing.recording():
        return
    rep = testing.recorded_report()
    path = config.getoption("--emu-report")
    if path:
        _report.dump(rep, path)
    missing = _report.unimplemented(rep)
    config.stash[_KEY].update(missing=missing, path=path)
    if config.getoption("--emu-strict") and missing and session.exitstatus == 0:
        session.exitstatus = STRICT_EXIT


def pytest_terminal_summary(terminalreporter, config):
    if _wants_record(config) and os.environ.get(emu_config.RECORD_URL_VAR):
        terminalreporter.write_line("emuscope: emu record の中なので、--emu-report と --emu-strict は使わず、"
                                    "集計は emu record に任せました")
    state = config.stash.get(_KEY, {})
    if state.get("path"):
        terminalreporter.write_line(f"emuscope: 対応状況レポートを書きました: {state['path']}")
    missing = state.get("missing")
    if missing:
        terminalreporter.section("emuscope: エミュレーターで未実装の操作", sep="-", red=bool(config.getoption("--emu-strict")))
        for o in missing:
            terminalreporter.write_line(f"  {o['service']}:{o['operation']}（{o['calls']} 回）")
