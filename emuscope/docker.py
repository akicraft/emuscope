"""エミュレーターのコンテナを起動・停止する。docker コマンドを呼ぶだけ。"""

import json
import ssl
import subprocess
import time
import urllib.request

from . import config


class DockerError(Exception):
    pass


def _docker(*args, timeout=120):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DockerError("docker コマンドが見つかりません。OrbStack か Docker を入れてください。") from None
    except subprocess.TimeoutExpired:
        raise DockerError("docker の応答がありません。OrbStack / Docker は起動していますか。") from None


def daemon_ok() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", timeout=15).returncode == 0
    except DockerError:
        return False


def image_exists() -> bool:
    return _docker("image", "inspect", config.IMAGE).returncode == 0


def build() -> None:
    """Dockerfile からイメージを作る。進み具合はそのまま画面に流す。"""
    if subprocess.run(["docker", "build", "-t", config.IMAGE, str(config.ROOT)]).returncode != 0:
        raise DockerError("イメージのビルドに失敗しました。")


def is_running() -> bool:
    r = _docker("inspect", "-f", "{{.State.Running}}", config.CONTAINER)
    return r.returncode == 0 and r.stdout.strip() == "true"


def healthy() -> bool:
    """/__admin/health が ok を返すか。"""
    try:
        ctx = ssl.create_default_context(cafile=str(config.CA))
        with urllib.request.urlopen(f"{config.ENDPOINT}/__admin/health", context=ctx, timeout=2) as res:
            return res.read().strip() == b"ok"
    except Exception:
        return False


def up(quiet: bool = False) -> bool:
    """起動する。すでに動いていれば何もしない。新しく起動したら True。"""
    say = (lambda *a: None) if quiet else print
    if is_running():
        _wait_healthy()
        return False
    if not image_exists():
        say("イメージがないのでビルドします（初回は2分ほど）")
        build()
    config.EMU_ROOT.mkdir(parents=True, exist_ok=True)
    # 異常終了で残った古い鍵や証明書を消す。残っていると、起動を待つ間に古い証明書を掴んでしまう。
    if config.EMU_DIR.exists():
        for f in config.EMU_DIR.iterdir():
            f.unlink()
    r = _docker(
        "run", "-d", "--rm", "--name", config.CONTAINER,
        "--memory=2g", "--memory-swap=2g",
        # 127.0.0.1 を付けないと、同じLANの他の機器からも届いてしまう
        "-p", f"127.0.0.1:{config.PORT}:{config.PORT}",
        "-e", "EMULATOR_TELEMETRY=off",
        # config に書かれる ca.pem の絶対パスが、ホストとコンテナで一致するようにする
        "-e", f"HOME={config.EMU_HOME}",
        "-v", f"{config.EMU_ROOT}:{config.EMU_ROOT}",
        config.IMAGE, "-p", str(config.PORT), "--telemetry", "off",
    )
    if r.returncode != 0:
        raise DockerError("起動に失敗しました: " + (r.stderr.strip().splitlines() or ["不明なエラー"])[-1])
    _wait_healthy()
    return True


def _wait_healthy(seconds: int = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if healthy():
            return
        time.sleep(0.2)
    raise DockerError(f"{seconds}秒待っても起動しません。./emu logs を確認してください。")


def down() -> bool:
    """止める。作ったリソースはすべて消える。止めたら True。"""
    if not is_running():
        return False
    _docker("stop", config.CONTAINER)
    return True


def restart() -> None:
    down()
    up(quiet=True)


_version_cache = {"stamp": None, "value": None}


def version() -> str | None:
    """エミュレーターのバージョン。起動ごとに一度だけ調べる。"""
    try:
        stamp = config.CREDENTIALS.stat().st_mtime_ns
    except OSError:
        return None
    if _version_cache["stamp"] != stamp:
        r = _docker("exec", config.CONTAINER, "aws-emulator", "--version", timeout=15)
        value = None
        for line in r.stdout.splitlines():
            if line.lower().startswith("version:"):
                value = line.split(":", 1)[1].strip()
        _version_cache.update(stamp=stamp, value=value)
    return _version_cache["value"]


def memory() -> str | None:
    r = _docker("stats", "--no-stream", "--format", "{{.MemUsage}}", config.CONTAINER, timeout=20)
    return r.stdout.strip() or None if r.returncode == 0 else None


def started_at() -> float | None:
    try:
        return config.CREDENTIALS.stat().st_mtime
    except OSError:
        return None


# クライアントがIPアドレスで接続するたびに出る警告。害はなく、量が多いので既定では隠す。
NOISE = ("Illegal SNI extension",)


def logs(tail: int = 300) -> list[dict]:
    """エミュレーター自身のログ。JSON の行は読みやすい形に直す。"""
    r = _docker("logs", "--tail", str(tail), config.CONTAINER, timeout=20)
    if r.returncode != 0:
        return []
    entries = []
    for line in (r.stdout + r.stderr).splitlines():
        line = line.strip()
        if not line or "config.json" in line:
            continue
        entry = {"time": "", "level": "INFO", "message": line, "fields": {}, "target": ""}
        if line.startswith("{"):
            try:
                rec = json.loads(line)
                fields = dict(rec.get("fields", {}))
                entry.update(
                    time=rec.get("timestamp", ""),
                    level=rec.get("level", "INFO"),
                    message=fields.pop("message", ""),
                    fields={k: v for k, v in fields.items() if not k.startswith("log.")},
                    target=rec.get("target", ""),
                )
            except ValueError:
                pass
        entry["noise"] = any(n in entry["message"] for n in NOISE)
        entries.append(entry)
    entries.sort(key=lambda e: e["time"])  # stdout と stderr を混ぜたので時刻順に戻す（時刻なしの行は先頭）
    return entries
