"""場所とポートの定義。ここが唯一の出どころ。"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(__file__).resolve().parent
IMAGE_DIR = PACKAGE / "image"  # Dockerfile の置き場所

# リポジトリをクローンして ./emu で使っているか、pip で入れて使っているか
CLONED = (ROOT / "emu").is_file()
EMU_CMD = "./emu" if CLONED else "emu"


def _path_from_env(name, default):
    """環境変数で場所を変えられるようにする。コンテナとホストでパスを一致させるため、絶対パスに直す。"""
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default

PORT = int(os.environ.get("EMU_PORT", "9297"))  # エミュレーター本体（HTTPS）
UI_PORT = int(os.environ.get("EMU_UI_PORT", "9298"))  # 画面
PROXY_PORT = int(os.environ.get("EMU_PROXY_PORT", "9299"))  # APIログ用の中継（HTTP）

IMAGE = "aws-emulator"
CONTAINER = f"aws-emulator-{PORT}"

# 本物の ~/.aws には触れない。専用の場所を「仮のHOME」としてコンテナに渡す。
# クローンして使うときは .emu-home、pip で入れたときは ~/.emuscope/home。
# プロジェクトごとにしないのは、コンテナが1ポートに1つだから（どのフォルダから呼んでも同じ鍵を見る必要がある）。
EMU_HOME = _path_from_env("EMU_HOME", ROOT / ".emu-home" if CLONED else Path.home() / ".emuscope" / "home")
EMU_ROOT = EMU_HOME / ".aws" / "emulator"
EMU_DIR = EMU_ROOT / str(PORT)
CREDENTIALS = EMU_DIR / "emulator_credentials"
CONFIG = EMU_DIR / "config"
CA = EMU_DIR / "ca.pem"

ENDPOINT = f"https://127.0.0.1:{PORT}"
PROXY_ENDPOINT = f"http://127.0.0.1:{PROXY_PORT}"
UI_URL = f"http://127.0.0.1:{UI_PORT}"

# スナップショットの置き場所。pip で入れたときは、今いるフォルダ（自分のプロジェクト）の snapshots/
SNAPSHOTS = _path_from_env("EMU_SNAPSHOTS", ROOT / "snapshots" if CLONED else Path.cwd() / "snapshots")

ACCOUNT = "000000000000"  # エミュレーターのアカウントは常にこれ
REGION = "us-east-1"

# 本物のAWSに向く可能性のある変数。エミュレーター用の環境を作るときは必ず外す。
# ./emu record が子に渡す、自分の中継の URL
RECORD_URL_VAR = "EMU_RECORD_URL"

UNSET_VARS = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"]


def env_vars(proxy: bool | str = False) -> dict:
    """CLI や SDK をエミュレーターに向ける環境変数。

    proxy=True のときは中継サーバー経由にする。呼び出しが画面の「APIログ」に記録される。
    proxy に URL の文字列を渡すと、その中継を通す（./emu record が自分で立てた中継など）。
    ./emu record の中（EMU_RECORD_URL がある）では、proxy が True でも False でも、その中継を通す。
    """
    if isinstance(proxy, str):
        endpoint = proxy
    elif os.environ.get(RECORD_URL_VAR):
        # ./emu record の中で動いているときは、その中継を通す。
        # 入れ子の ./emu run やテストの補助が直結に向け直すと、呼び出しが記録から漏れる
        endpoint = os.environ[RECORD_URL_VAR]
    else:
        endpoint = PROXY_ENDPOINT if proxy else ENDPOINT
    return {
        "AWS_ENDPOINT_URL": endpoint,
        "AWS_SHARED_CREDENTIALS_FILE": str(CREDENTIALS),
        "AWS_CONFIG_FILE": str(CONFIG),
        "AWS_CA_BUNDLE": str(CA),
        # JavaScript（Node.js）の SDK は AWS_CA_BUNDLE ではなくこちらを読む（未検証）
        "NODE_EXTRA_CA_CERTS": str(CA),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DISABLE_HOST_PREFIX_INJECTION": "true",
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
    }


def child_env(proxy: bool | str = False) -> dict:
    """今の環境から本物向けの変数を外し、エミュレーター用の変数を足したもの。"""
    env = {k: v for k, v in os.environ.items() if k not in UNSET_VARS}
    env.update(env_vars(proxy))
    return env
