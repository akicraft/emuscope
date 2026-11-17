"""対応状況レポート。

中継が集計した「どのサービスの・どの操作を・何回呼んで・どう返ったか」から、
そのアプリが使っているAPIのうち、エミュレーターで未実装のものを表にする。

  ./emu record -- python -m unittest     実行して、終わったら表を出す
  ./emu report                           画面（./emu ui）のAPIログから作る
"""

import datetime
import json
from pathlib import Path

from . import __version__

SCHEMA = 1

UNIMPLEMENTED, FAILED, OK = "unimplemented", "failed", "ok"
ORDER = {UNIMPLEMENTED: 0, FAILED: 1, OK: 2}
LABEL = {UNIMPLEMENTED: "未実装", FAILED: "失敗だけ", OK: "動いた"}


def _result(statuses: dict) -> str:
    """501 が1回でもあれば未実装。成功が1回もなければ失敗。それ以外は動いた。

    4xx が混じっていても「動いた」にするのは、条件付き書き込みの失敗のように、
    テストがわざと起こすエラーが多いため。
    """
    codes = {int(k): v for k, v in statuses.items()}
    if codes.get(501):
        return UNIMPLEMENTED
    if not any(code < 400 for code in codes):
        return FAILED
    return OK


def build(stats: list[dict], emulator: str | None) -> dict:
    operations = []
    for s in stats:
        operations.append({
            "service": s["service"], "operation": s["operation"], "result": _result(s["statuses"]),
            "calls": s["calls"], "statuses": {str(k): v for k, v in sorted(s["statuses"].items(), key=lambda kv: int(kv[0]))},
            "errors": dict(sorted(s["errors"].items())), "clients": list(s["clients"]),
        })
    operations.sort(key=lambda o: (ORDER[o["result"]], o["service"], o["operation"]))
    return {
        "schema": SCHEMA,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "emulator": emulator,
        "emuscope": __version__,
        "totals": {
            "calls": sum(o["calls"] for o in operations),
            "operations": len(operations),
            "unimplemented": sum(o["result"] == UNIMPLEMENTED for o in operations),
            "failed": sum(o["result"] == FAILED for o in operations),
        },
        "operations": operations,
    }


def unimplemented(report: dict) -> list[dict]:
    return [o for o in report["operations"] if o["result"] == UNIMPLEMENTED]


def _key(o):
    return (o["service"], o["operation"])


def compare(old: dict, new: dict) -> dict:
    """前のレポートと比べる。エミュレーターを上げたときに、何が変わったかを見る。"""
    before = {_key(o): o["result"] for o in old.get("operations", [])}
    after = {_key(o): o["result"] for o in new.get("operations", [])}
    out = {"implemented": [], "broken": [], "added": [], "removed": []}
    for key, result in after.items():
        if key not in before:
            out["added"].append(key)
        elif before[key] == UNIMPLEMENTED and result == OK:
            out["implemented"].append(key)
        elif before[key] == OK and result in (UNIMPLEMENTED, FAILED):
            out["broken"].append(key)
    out["removed"] = [key for key in before if key not in after]
    for v in out.values():
        v.sort()
    return out


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(rows):
    lines = ["| サービス | 操作 | 回数 | 結果 | 呼び出し元 |", "| --- | --- | ---: | --- | --- |"]
    for o in rows:
        codes = ", ".join(f"{code}×{n}" for code, n in o["statuses"].items())
        errors = ", ".join(o["errors"])
        lines.append(f"| {_cell(o['service'])} | {_cell(o['operation'])} | {o['calls']} | "
                     f"{_cell(codes + (' ' + errors if errors else ''))} | {_cell(', '.join(o['clients']))} |")
    return lines


def to_markdown(report: dict, previous: dict | None = None) -> str:
    t = report["totals"]
    lines = [
        "# エミュレーター対応状況",
        "",
        f"- エミュレーター: {report.get('emulator') or '不明'}",
        f"- 作成: {report['created']}（emuscope {report['emuscope']}）",
        f"- 呼び出し {t['calls']} 回、操作 {t['operations']} 種類、未実装 {t['unimplemented']}、失敗だけ {t['failed']}",
        "",
    ]
    if previous is not None:
        diff = compare(previous, report)
        lines += [f"## 前回（{previous.get('emulator') or '不明'}）との違い", ""]
        names = {"implemented": "実装された", "broken": "動かなくなった", "added": "新しく使われた", "removed": "使われなくなった"}
        if not any(diff.values()):
            lines.append("違いはありませんでした。")
        for kind, label in names.items():
            if diff[kind]:
                lines.append(f"- {label}: " + ", ".join(f"{s}:{o}" for s, o in diff[kind]))
        lines.append("")
    for result in (UNIMPLEMENTED, FAILED, OK):
        rows = [o for o in report["operations"] if o["result"] == result]
        lines += [f"## {LABEL[result]}（{len(rows)}）", ""]
        if rows:
            lines += _table(rows)
        elif result == UNIMPLEMENTED:
            lines.append("未実装の操作はありませんでした。")
        else:
            lines.append("なし")
        lines.append("")
    return "\n".join(lines)


def dump(report: dict, path, previous: dict | None = None) -> None:
    """拡張子が .json なら JSON、それ以外は Markdown で書く。"""
    path = Path(path)
    if path.suffix == ".json":
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        path.write_text(to_markdown(report, previous))


def load(path) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path} はこの版の emuscope のレポートではありません（schema {data.get('schema')}）")
    return data
