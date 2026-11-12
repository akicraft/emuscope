"""場所とポートの定義。ここが唯一の出どころ。"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PORT = int(os.environ.get("EMU_PORT", "9297"))  # エミュレーター本体（HTTPS）
UI_PORT = int(os.environ.get("EMU_UI_PORT", "9298"))  # 画面
PROXY_PORT = int(os.environ.get("EMU_PROXY_PORT", "9299"))  # APIログ用の中継（HTTP）

IMAGE = "aws-emulator"
CONTAINER = f"aws-emulator-{PORT}"

# 本物の ~/.aws には触れない。プロジェクト内の .emu-home を「仮のHOME」としてコンテナに渡す。
EMU_HOME = ROOT / ".emu-home"
EMU_ROOT = EMU_HOME / ".aws" / "emulator"
EMU_DIR = EMU_ROOT / str(PORT)
CREDENTIALS = EMU_DIR / "emulator_credentials"
CONFIG = EMU_DIR / "config"
CA = EMU_DIR / "ca.pem"

ENDPOINT = f"https://127.0.0.1:{PORT}"
PROXY_ENDPOINT = f"http://127.0.0.1:{PROXY_PORT}"
UI_URL = f"http://127.0.0.1:{UI_PORT}"

SNAPSHOTS = ROOT / "snapshots"

ACCOUNT = "000000000000"  # エミュレーターのアカウントは常にこれ
REGION = "us-east-1"

# 本物のAWSに向く可能性のある変数。エミュレーター用の環境を作るときは必ず外す。
UNSET_VARS = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"]


def env_vars(proxy: bool = False) -> dict:
    """CLI や SDK をエミュレーターに向ける環境変数。

    proxy=True のときは中継サーバー経由にする。呼び出しが画面の「APIログ」に記録される。
    """
    return {
        "AWS_ENDPOINT_URL": PROXY_ENDPOINT if proxy else ENDPOINT,
        "AWS_SHARED_CREDENTIALS_FILE": str(CREDENTIALS),
        "AWS_CONFIG_FILE": str(CONFIG),
        "AWS_CA_BUNDLE": str(CA),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DISABLE_HOST_PREFIX_INJECTION": "true",
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
    }


def child_env(proxy: bool = False) -> dict:
    """今の環境から本物向けの変数を外し、エミュレーター用の変数を足したもの。"""
    env = {k: v for k, v in os.environ.items() if k not in UNSET_VARS}
    env.update(env_vars(proxy))
    return env
