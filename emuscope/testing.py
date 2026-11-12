"""自分のアプリのテストで、エミュレーターを使うための補助。

    from emuscope.testing import EmulatorTestCase

    class OrderTest(EmulatorTestCase):
        snapshot = "orders-base"      # 任意。クラスの最初に、この状態から始める

        def test_create(self):
            ddb = self.client("dynamodb")
            ...

実行は `./emu run -- python -m unittest`（emuscope を import できるようにしてくれる）。
"""

import os
import unittest

import boto3

from . import config, docker, snapshot
from .aws import client as _guarded_client


def use_emulator(proxy: bool = False) -> None:
    """このプロセスの AWS SDK を、エミュレーターに向ける。

    本物のAWSに向く可能性のある変数（AWS_PROFILE など）は外す。
    proxy=True にすると、呼び出しが画面の「APIログ」に記録される（./emu ui が動いている必要がある）。
    """
    docker.up(quiet=True)
    for name in config.UNSET_VARS:
        os.environ.pop(name, None)
    os.environ.update(config.env_vars(proxy))
    boto3.DEFAULT_SESSION = None  # 以前の設定で作られた既定のセッションを捨てる
    _guarded_client("sts")  # 相手がエミュレーターであることを確かめる（違えば例外）


def reset() -> None:
    """エミュレーターを再起動して、中身を空にする。1〜2秒かかる。"""
    docker.restart()
    boto3.DEFAULT_SESSION = None  # 鍵と証明書が変わるので、作り直させる
    _guarded_client("sts")


def load_snapshot(name: str, clean: bool = True) -> None:
    """保存しておいた状態に戻す。clean=True なら、空にしてから入れる。"""
    snapshot.load(name, reset=clean)
    boto3.DEFAULT_SESSION = None


class EmulatorTestCase(unittest.TestCase):
    """エミュレーターにつないだ状態で走るテストの土台。

    snapshot:   クラスの最初に読み込むスナップショット名（空にしてから入れる）
    clean_start: snapshot を使わないとき、クラスの最初に中身を空にするか
    api_log:    True にすると、呼び出しを画面の「APIログ」に記録する
    """

    snapshot: str | None = None
    clean_start: bool = False
    api_log: bool = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        use_emulator(proxy=cls.api_log)
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
