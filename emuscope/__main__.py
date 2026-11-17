"""./emu コマンドの中身。"""

import argparse
import json
import os
import shlex
import shutil
import sys
import threading
import time

from . import __version__, config, docker

HELP = """\
AWS Emulator を使ったローカル開発の道具（非公式）

  ./emu dev                 エミュレーターと画面をまとめて起動する（迷ったらこれ）
  ./emu up / down / restart エミュレーターの起動・停止・再起動（再起動で中身は空になる）
  ./emu up --load 名前       起動して、保存しておいた状態を入れる
  ./emu down --save 名前     今の状態を保存してから止める
  ./emu status              動いているか、バージョン、メモリ
  ./emu doctor              うまく動かないときの点検
  ./emu ui                  画面とAPIログ用の中継を起動する  http://127.0.0.1:9298
  ./emu run -- <コマンド>    エミュレーターに向けてコマンドを実行する
  ./emu run --log -- <...>  同じ。呼び出しを画面の「APIログ」に記録する
  ./emu record -- <コマンド> 実行して、使ったAPIの対応状況（未実装の操作）を表にする
  ./emu report              画面のAPIログから、同じ表を作る
  ./emu aws <...>           ./emu run -- aws <...> の短縮形
  ./emu env [--log]         環境変数を表示する   使い方: eval "$(./emu env)"
  ./emu seed                サンプルデータを入れる
  ./emu snapshot save|load|list|delete [名前]   中身の保存と復元
  ./emu logs                エミュレーター自身のログ
  ./emu build               イメージを作り直す
"""


def cmd_up(args):
    started = docker.up()
    print(f"{'起動しました' if started else 'すでに動いています'}: {config.ENDPOINT}")
    if getattr(args, "load", None):
        from . import snapshot

        result = snapshot.load(args.load, reset=False)
        print(f"スナップショット {args.load} を読み込みました: " + _counts(result["counts"]))


def cmd_down(args):
    if getattr(args, "save", None) and docker.healthy():
        from . import snapshot

        result = snapshot.save(args.save)
        print(f"スナップショット {args.save} に保存しました: " + _counts(result["counts"]))
    print("停止しました（作ったリソースはすべて消えました）" if docker.down() else "動いていません")


def cmd_restart(args):
    docker.restart()
    print(f"再起動しました（中身は空です）: {config.ENDPOINT}")
    if args.load:
        cmd_snapshot(argparse.Namespace(action="load", name=args.load, merge=True))


def cmd_status(args):
    if not docker.healthy():
        print("停止中")
        return 1
    print(f"稼働中    {config.ENDPOINT}")
    print(f"バージョン {docker.version()}")
    print(f"メモリ    {docker.memory()}")
    started = docker.started_at()
    if started:
        print(f"起動時刻  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")


def cmd_doctor(args):
    """うまく動かないときに、どこで止まっているかを上から順に確かめる。"""
    ok = True

    def check(label, passed, hint=""):
        nonlocal ok
        print(f"{'OK  ' if passed else 'NG  '}{label}" + (f"\n      → {hint}" if hint and not passed else ""))
        ok = ok and passed
        return passed

    check("docker コマンドがある", shutil.which("docker") is not None, "OrbStack か Docker を入れてください")
    if check("Docker が動いている", docker.daemon_ok(), "OrbStack / Docker を起動してください"):
        check("イメージがある", docker.image_exists(), "./emu build で作れます")
        if check("エミュレーターのコンテナが動いている", docker.is_running(), "./emu up で起動できます"):
            mounts = docker.mounted_dirs()
            check(f"鍵と証明書の場所が一致している（{config.EMU_ROOT}）", not mounts or str(config.EMU_ROOT) in mounts,
                  f"コンテナは {mounts[0] if mounts else ''} を使っています。止めてから起動し直してください")
            check("ヘルスチェックに応答する", docker.healthy(), "./emu logs を確認してください")
            entries = docker.logs(50)
            check("テレメトリーが無効", any("telemetry: disabled" in e["message"] for e in entries) or not entries,
                  "起動ログに disabled の表示がありません")
            try:
                from .aws import client

                ident = client("sts").get_caller_identity()
                check(f"エミュレーターのアカウントにつながる（{ident['Account']}）", ident["Account"] == config.ACCOUNT)
            except Exception as e:  # noqa: BLE001
                check("エミュレーターのアカウントにつながる", False, f"{type(e).__name__}: {e}")
    check("aws コマンドがある（任意）", shutil.which("aws") is not None, "brew install awscli で入れられます。なくても Boto3 は使えます")

    # 今のシェルが本物のAWSに向いていないか
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    risky = [v for v in config.UNSET_VARS if os.environ.get(v)]
    if endpoint.startswith(("https://127.0.0.1:", "http://127.0.0.1:")):
        print(f"情報 このシェルはエミュレーターに向いています（{endpoint}）")
    else:
        print("情報 このシェルの aws コマンドは、エミュレーターに向いていません。./emu run -- <コマンド> を使うと確実です")
    if risky:
        print(f"注意 このシェルには {', '.join(risky)} が設定されています。./emu run と ./emu env は、これらを外します")
    return 0 if ok else 1


def cmd_env(args):
    env = config.env_vars(proxy=args.log)
    if args.format == "json":
        print(json.dumps(env, ensure_ascii=False, indent=2))
    elif args.format == "dotenv":
        for k, v in env.items():
            print(f"{k}={v}")
    else:
        for k, v in env.items():
            print(f"export {k}={shlex.quote(v)}")
        print("unset " + " ".join(config.UNSET_VARS))


def cmd_run(args):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("実行するコマンドを指定してください。例: ./emu run -- aws s3 ls", file=sys.stderr)
        return 2
    if docker.up(quiet=True):
        print("（エミュレーターを起動しました）", file=sys.stderr)
    # ./emu record の中なら、記録はその中継が引き受ける（画面の中継は要らない）
    if args.log and not os.environ.get(config.RECORD_URL_VAR) and not _port_open(config.PROXY_PORT):
        print("APIログ用の中継が動いていません。別のターミナルで ./emu ui を起動してください。", file=sys.stderr)
        return 1
    try:
        os.execvpe(command[0], command, config.child_env(proxy=args.log))
    except FileNotFoundError:
        print(f"コマンドが見つかりません: {command[0]}", file=sys.stderr)
        return 127


STRICT_EXIT = 3  # --strict で未実装の操作があったときの終了コード


def _finish_report(rep, args):
    """レポートを書き出して、終了コードを決める。record と report で共通。"""
    from . import report

    previous = report.load(args.compare) if args.compare else None
    if args.out:
        report.dump(rep, args.out, previous)
        print(f"レポートを書きました: {args.out}", file=sys.stderr)
    else:
        print(report.to_markdown(rep, previous), file=sys.stderr)  # 子の標準出力を汚さない
    missing = report.unimplemented(rep)
    if args.strict and missing:
        print("未実装の操作を使っています: " + ", ".join(f"{o['service']}:{o['operation']}" for o in missing),
              file=sys.stderr)
        return STRICT_EXIT
    return 0


def cmd_record(args):
    """自分で中継を立てて、コマンドを実行し、使った操作を集計する。画面は要らない。"""
    import subprocess

    from . import proxy, report

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("実行するコマンドを指定してください。例: ./emu record -- python -m unittest", file=sys.stderr)
        return 2
    if docker.up(quiet=True):
        print("（エミュレーターを起動しました）", file=sys.stderr)
    proxy.clear_calls()
    px = proxy.make_server(port=0)
    threading.Thread(target=px.serve_forever, daemon=True).start()
    try:
        try:
            url = proxy.url_of(px)
            env = config.child_env(proxy=url)
            env[config.RECORD_URL_VAR] = url  # 子が ./emu run やテストの補助を使っても、この中継を通るように
            code = subprocess.run(command, env=env).returncode
        except FileNotFoundError:
            print(f"コマンドが見つかりません: {command[0]}", file=sys.stderr)
            return 127
    finally:
        proxy.wait_idle()  # 最後の呼び出しが記録されるのを待つ
        px.shutdown()
        px.server_close()
    rep = report.build(proxy.summary(), docker.version())
    result = _finish_report(rep, args)
    if not rep["totals"]["calls"]:
        print("注意: 呼び出しが1回も記録されませんでした。コマンドが AWS_ENDPOINT_URL を自分で設定していないか確かめてください。",
              file=sys.stderr)
    return code if code != 0 else result


def cmd_report(args):
    """動いている画面（./emu ui）の中継が集めた記録から、レポートを作る。"""
    import urllib.error
    import urllib.request

    from . import report

    if args.source:
        rep = report.load(args.source)
    else:
        try:
            with urllib.request.urlopen(f"{config.UI_URL}/api/calls/summary", timeout=5) as res:
                rep = json.loads(res.read())
        except (urllib.error.URLError, OSError):
            print("./emu ui が動いていません。画面なしで使うなら ./emu record -- <コマンド> を使ってください。",
                  file=sys.stderr)
            return 1
    return _finish_report(rep, args)


def _port_open(port):
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_ui(args):
    from . import proxy, server

    try:
        ui = server.make_server(read_only=args.read_only, proxy_enabled=not args.no_proxy)
    except OSError as e:
        print(f"ポート {config.UI_PORT} を使えません（{e.strerror}）。すでに画面が動いていませんか。", file=sys.stderr)
        return 1
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    print(f"画面        {config.UI_URL}" + ("   （読み取り専用）" if args.read_only else ""))
    if not args.no_proxy:
        try:
            px = proxy.make_server()
            threading.Thread(target=px.serve_forever, daemon=True).start()
            print(f"APIログ中継  {config.PROXY_ENDPOINT}   （./emu run --log -- <コマンド> で記録）")
        except OSError as e:
            print(f"APIログ用の中継は起動できませんでした（ポート {config.PROXY_PORT}: {e.strerror}）", file=sys.stderr)
    print(f"エミュレーター {config.ENDPOINT}" + ("" if docker.healthy() else "   （停止中。画面から起動できます）"))
    print("止めるときは Ctrl+C", flush=True)
    if args.open:
        import webbrowser

        webbrowser.open(config.UI_URL)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print()


def cmd_dev(args):
    cmd_up(args)
    return cmd_ui(args)


def cmd_seed(args):
    from . import seed

    docker.up(quiet=True)
    for line in seed.run():
        print(line)


def _counts(counts):
    return "、".join(f"{k} {v}" for k, v in counts.items() if v) or "空"


def cmd_snapshot(args):
    from . import snapshot

    if args.action == "list":
        rows = snapshot.listing()
        if not rows:
            print(f"スナップショットはまだありません（保存先: {config.SNAPSHOTS}）")
        for s in rows:
            print(f"{s['name']:<24} {s['created']}  {s['bytes'] / 1024:8.1f} KB  {_counts(s['counts'])}")
        return
    if not args.name:
        print("名前を指定してください。例: ./emu snapshot save base", file=sys.stderr)
        return 2
    if args.action == "save":
        if not docker.healthy():
            print("エミュレーターが動いていません。", file=sys.stderr)
            return 1
        result = snapshot.save(args.name)
        print(f"保存しました: {config.SNAPSHOTS / args.name}\n  " + _counts(result["counts"]))
        for label, code in result["skipped"].items():
            print(f"  保存できなかったもの: {label}（{code}）")
    elif args.action == "load":
        result = snapshot.load(args.name, reset=not args.merge)
        how = "今の状態に重ねました" if args.merge else "空にしてから入れました"
        print(f"読み込みました（{how}）: " + _counts(result["counts"]))
    elif args.action == "delete":
        snapshot.delete(args.name)
        print(f"削除しました: {args.name}")


def cmd_logs(args):
    for e in docker.logs(args.tail):
        if e["noise"] and not args.all:
            continue
        fields = "  " + " ".join(f"{k}={v}" for k, v in e["fields"].items()) if e["fields"] else ""
        print(f"{e['time'][11:19]:<8} {e['level']:<5} {e['message']}{fields}")


def cmd_build(args):
    docker.build()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="emu", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"emuscope {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    def ui_options(p):
        p.add_argument("--read-only", action="store_true", help="画面からの書き込みと再起動を禁止する")
        p.add_argument("--no-proxy", action="store_true", help="APIログ用の中継を起動しない")
        p.add_argument("--open", action="store_true", help="起動したらブラウザで開く")

    p = sub.add_parser("up"); p.add_argument("--load", metavar="名前", help="起動後にスナップショットを読み込む"); p.set_defaults(fn=cmd_up)
    p = sub.add_parser("down"); p.add_argument("--save", metavar="名前", help="止める前にスナップショットを保存する"); p.set_defaults(fn=cmd_down)
    p = sub.add_parser("restart"); p.add_argument("--load", metavar="名前"); p.set_defaults(fn=cmd_restart)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("ui"); ui_options(p); p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("dev"); ui_options(p); p.add_argument("--load", metavar="名前"); p.set_defaults(fn=cmd_dev)
    p = sub.add_parser("env"); p.add_argument("--log", action="store_true", help="中継を通す（APIログに記録）")
    p.add_argument("--format", choices=["sh", "dotenv", "json"], default="sh"); p.set_defaults(fn=cmd_env)
    p = sub.add_parser("run"); p.add_argument("--log", action="store_true"); p.add_argument("command", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_run)
    def report_options(p):
        p.add_argument("--out", metavar="ファイル", help="レポートの書き出し先。.json なら JSON、それ以外は Markdown")
        p.add_argument("--compare", metavar="ファイル", help="前に書き出した JSON のレポートと比べる")
        p.add_argument("--strict", action="store_true", help=f"未実装の操作があれば、終了コード {STRICT_EXIT} で終わる")

    p = sub.add_parser("record"); report_options(p); p.add_argument("command", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_record)
    p = sub.add_parser("report"); report_options(p)
    p.add_argument("--from", dest="source", metavar="ファイル", help="画面の代わりに、書き出した JSON から作る"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("aws"); p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=lambda a: cmd_run(argparse.Namespace(log=False, command=["aws", *a.command])))
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    p = sub.add_parser("snapshot"); p.add_argument("action", choices=["save", "load", "list", "delete"]); p.add_argument("name", nargs="?")
    p.add_argument("--merge", action="store_true", help="load のとき、空にせず今の状態に重ねる"); p.set_defaults(fn=cmd_snapshot)
    p = sub.add_parser("logs"); p.add_argument("--tail", type=int, default=200); p.add_argument("--all", action="store_true", help="害のない警告も出す"); p.set_defaults(fn=cmd_logs)
    sub.add_parser("build").set_defaults(fn=cmd_build)

    args = parser.parse_args(argv)
    if not args.cmd:
        print(HELP)
        return 0
    try:
        return args.fn(args) or 0
    except docker.DockerError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except (KeyError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
