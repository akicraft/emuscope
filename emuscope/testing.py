"""自分のアプリのテストで、エミュレーターを使うための補助。

    from emuscope.testing import EmulatorTestCase

    class OrderTest(EmulatorTestCase):
        snapshot = "orders-base"      # 任意。クラスの最初に、この状態から始める

        def test_create(self):
            ddb = self.client("dynamodb")
            ...

実行は `./emu run -- python -m unittest`（emuscope を import できるようにしてくれる）。

pytest なら emuscope.pytest_plugin のフィクスチャ（emu_client など）が使える。
"""

import os
import threading
import unittest

import boto3

from . import config, docker, proxy as _proxy, report, snapshot
from .aws import client as _guarded_client

# 記録用の中継の接続先。None ならエミュレーター（テストで偽の上流に差し替えるためのもの）
RECORD_UPSTREAM = None

_recorder = {"server": None}
_recorder_lock = threading.Lock()


def start_recording() -> str:
    """このプロセスの中で、呼び出しを集計する中継を立てて、その URL を返す。何度呼んでも1つだけ立てる。

    画面（./emu ui）は要らない。集計は unimplemented_calls() や emuscope.report で読める。
    """
    with _recorder_lock:
        if _recorder["server"] is None:
            server = _proxy.make_server(port=0, upstream=RECORD_UPSTREAM)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _recorder["server"] = server
        return _proxy.url_of(_recorder["server"])


def recording() -> bool:
    return _recorder["server"] is not None


def recorded_report() -> dict:
    """ここまでの呼び出しの対応状況レポート（emuscope.report の形）。"""
    _proxy.wait_idle()
    return report.build(_proxy.summary(), docker.version())


def unimplemented_calls() -> list[dict]:
    """ここまでに呼んだ操作のうち、エミュレーターが未実装（501）と答えたもの。"""
    return report.unimplemented(recorded_report())


def use_emulator(proxy: bool | str = False) -> None:
    """このプロセスの AWS SDK を、エミュレーターに向ける。

    本物のAWSに向く可能性のある変数（AWS_PROFILE など）は外す。
    proxy=True にすると、呼び出しが画面の「APIログ」に記録される（./emu ui が動いている必要がある）。
    proxy="record" にすると、このプロセスの中に中継を立てて集計する（画面は要らない）。
    """
    docker.up(quiet=True)
    for name in config.UNSET_VARS:
        os.environ.pop(name, None)
    if proxy == "record" and os.environ.get(config.RECORD_URL_VAR):
        proxy = False  # ./emu record の中では、外側の中継が集計する（中継を2段にはできない）
    os.environ.update(config.env_vars(start_recording() if proxy == "record" else bool(proxy)))
    boto3.DEFAULT_SESSION = None  # 以前の設定で作られた既定のセッションを捨てる
    _guarded_client("sts")  # 相手がエミュレーターであることを確かめる（違えば例外）


def reset() -> None:
    """エミュレーターを再起動して、中身を空にする。1〜2秒かかる。"""
    docker.restart()
    boto3.DEFAULT_SESSION = None  # 鍵と証明書が変わるので、作り直させる
    _guarded_client("sts")


def load_snapshot(name: str, clean: bool = True, directory=None) -> None:
    """保存しておいた状態に戻す。clean=True なら、空にしてから入れる。

    directory: スナップショットの置き場所。省略時は config.SNAPSHOTS（環境変数 EMU_SNAPSHOTS で変えられる）。
    """
    snapshot.load(name, reset=clean, directory=directory)
    boto3.DEFAULT_SESSION = None


class EmulatorTestCase(unittest.TestCase):
    """エミュレーターにつないだ状態で走るテストの土台。

    snapshot:   クラスの最初に読み込むスナップショット名（空にしてから入れる）
    clean_start: snapshot を使わないとき、クラスの最初に中身を空にするか
    api_log:    True にすると、呼び出しを画面の「APIログ」に記録する
    record:     True にすると、このプロセスの中で呼び出しを集計する（unimplemented_calls() で読める）
    """

    snapshot: str | None = None
    clean_start: bool = False
    api_log: bool = False
    record: bool = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        use_emulator(proxy="record" if cls.record else cls.api_log)
        if cls.snapshot:
            load_snapshot(cls.snapshot)
        elif cls.clean_start:
            reset()

    @staticmethod
    def client(service: str, **kwargs):
        """その時点の鍵で、新しいクライアントを作る。reset() のあとは作り直すこと。"""
        return boto3.client(service, **kwargs)

    @staticmethod
    def resource(service: str, **kwargs):
        return boto3.resource(service, **kwargs)
