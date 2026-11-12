# AWS Emulator を Mac で試した記録

> これは、最初に手作業で試したときの記録。その後、道具は emuscope に作り替えた（[README](../README.md)）。
> 文中の `viewer.py`、`boto3_smoke.py`、`make viewer` などは当時のもので、今は `emuscope/` と `./emu` に置き換わっている。

AWS公式のローカルエミュレーター「AWS Emulator」を、Apple Silicon の Mac で動かして S3 と DynamoDB を触るところまでの記録。

- 試した日: 2026-11-12（GAの翌日）
- エミュレーター: 1.0.322（リリース日 2026-11-10）
- マシン: MacBook Air M4 / 16GB / macOS Tahoe 26.6
- Docker: OrbStack（Docker 29.8.2）
- AWS CLI: 2.39.3（Homebrew）

## 使い方

```bash
make build   # イメージをビルド（初回だけ）
make up      # 起動。テレメトリーはOFF
make smoke   # S3 と DynamoDB の動作確認（AWS CLI）
make boto3   # 同じ確認を Boto3 から（初回は .venv を作る）
make viewer  # 中身をブラウザで見る（http://127.0.0.1:9298）
make down    # 停止。作ったリソースは全部消える
```

自分のシェルから `aws` コマンドを打ちたいときは、起動後に環境変数を読み込む。

```bash
eval "$(make -s env)"
aws s3 ls
```

この設定はそのシェルの中だけで有効。ターミナルを閉じれば元に戻る。

ほかに `make health`（ヘルスチェック）、`make whoami`（接続先の確認）、`make logs`、`make clean`（イメージと生成ファイルの削除）がある。

## やったこと

### 1. Dockerfile を書く

macOS 向けのバイナリも、公式のDockerイメージも配られていない。Linux用のRPMを入れるだけの短い Dockerfile を自分で書いてビルドする。

Apple Silicon では、ドキュメントの `ADD` 行にある `aws-emulator-x86_64.rpm` を `aws-emulator-aarch64.rpm` に書き換える。変更点はそこだけ。

ビルドは2分弱。ほとんどがRPMのダウンロード（約84秒）だった。できあがったイメージは 1.62GB。

### 2. 起動する

ドキュメントの起動コマンドに、テレメトリーOFFの指定を足した。

```bash
docker run -d --rm --name aws-emulator-9297 \
  --memory=2g --memory-swap=2g \
  -p 127.0.0.1:9297:9297 \
  -e EMULATOR_TELEMETRY=off \
  -e HOME="$PWD/.emu-home" \
  -v "$PWD/.emu-home/.aws/emulator:$PWD/.emu-home/.aws/emulator" \
  aws-emulator -p 9297 --telemetry off
```

起動は1秒かからない。起動ログに次の1行が出るので、テレメトリーが切れていることを確かめられる。

```
This emulator collects usage and error telemetry: disabled (via EMULATOR_TELEMETRY=off)
```

起動すると、次の4つのファイルが書き出される。

| ファイル | 中身 |
| --- | --- |
| `emulator_credentials` | エミュレーター専用のアクセスキー |
| `config` | リージョン、エンドポイント、証明書の場所 |
| `ca.pem` | 自己署名証明書。起動のたびに作り直される |
| `truststore.p12` | 同じ証明書のJava向け形式 |

### 3. ヘルスチェック

```bash
curl --cacert .emu-home/.aws/emulator/9297/ca.pem https://127.0.0.1:9297/__admin/health
# => ok
```

### 4. S3 と DynamoDB

`make smoke` の中身。どれも本物のAWSと同じコマンドがそのまま通った。

| 操作 | 結果 |
| --- | --- |
| `sts get-caller-identity` | アカウント `000000000000`、`user/emulator` |
| `s3 mb` → `s3 cp` → `s3 ls` → `s3 cp`（取得） | アップロードした文字列がそのまま返る |
| `dynamodb create-table` → `wait table-exists` | `CREATING` で返り、待つと使えるようになる |
| `dynamodb put-item` → `get-item` | 入れた項目がそのまま返る |
| `s3 rb --force` / `dynamodb delete-table` | 削除できる |

動作中のメモリ使用量は 188MiB（上限2GiB）だった。

### 5. 未実装のAPIを呼んで 501 を見る

25個のAPI（ほぼ一覧取得系）を順に呼んで、どれが未実装かを探した。

| 結果 | API |
| --- | --- |
| 501 `NotImplemented` | `athena list-work-groups`、`quicksight list-users`、`ce get-cost-and-usage` |
| 成功 | SQS、Lambda、EC2、IAM、Kinesis、SNS、CloudFormation、Secrets Manager、SSM、EKS、Glacier、Braket、Ground Station、Bedrock、SageMaker、Route 53、DynamoDB、S3 の一覧取得など18個 |
| 本物と同じ種類のエラー | 存在しないテーブルへの PartiQL（`ResourceNotFoundException`）、存在しないバケット（`NoSuchBucket`）、`support describe-services`（`SubscriptionRequiredException`） |

未実装のときの見え方はこうなる。

```
$ aws athena list-work-groups
aws: [ERROR]: An error occurred (NotImplemented) when calling the ListWorkGroups operation:
operation ListWorkGroups is scaffolded but not implemented in this emulator
```

HTTPの応答は次のとおり。

```
HTTP/1.1 501
content-type: application/x-amz-json-1.1

{"__type":"NotImplemented","message":"operation ListWorkGroups is scaffolded but not implemented in this emulator"}
```

わかったこと。

- エラーの形式は本物のAWSと同じ（`__type` と `message`）。SDK は普通のサービスエラーとして受け取れる
- メッセージにAPI名が入っているので、何が足りないかはすぐわかる
- AWS CLI は 501 を再試行しなかった。リクエストは1回だけで、すぐ返る
- CLI の終了コードは 254
- エミュレーター側のログには、この呼び出しについて何も出なかった
- 一覧取得が成功しても、そのサービスが丸ごと使えるとは限らない。Braket や Bedrock は一覧に実在のデバイスやモデルが並ぶが、その先の操作は試していない

### 6. Boto3 から接続する

Boto3 1.44.1（Python 3.15.2）で、CLI と同じことを試した。スクリプトは `boto3_smoke.py`、実行は `make boto3`。

コードにはエンドポイントも認証情報も書いていない。`boto3.client("s3")` とだけ書き、接続先は環境変数に任せた。CLI のときと同じ環境変数で、そのままエミュレーターにつながった。

| 操作 | 結果 |
| --- | --- |
| S3: `create_bucket` → `put_object` → `list_objects_v2` → `get_object` | 入れた文字列がそのまま返る |
| S3: `resource` 経由でサイズ取得 | 16バイト（正しい） |
| S3: `generate_presigned_url` | `https://127.0.0.1:9297/バケット名/キー?...` の形で生成される |
| DynamoDB: `create_table` → `wait_until_exists` → `put_item` | 通る |
| DynamoDB: `update_item` で `ADD count 1` | 42 が 43 になる |
| DynamoDB: 条件付き `put_item`（既存キーに `attribute_not_exists`） | `ConditionalCheckFailedException` |
| Athena: `list_work_groups`（未実装） | `ClientError`、Code は `NotImplemented`、HTTP 501、再試行0回 |

わかったこと。

- 自己署名証明書は `AWS_CA_BUNDLE` だけで通る。コード側で `verify=False` などを書く必要はない
- 条件付き書き込みの失敗が、本物と同じ例外コードで返る。楽観ロックのテストに使えそう
- 未実装APIは普通の `ClientError` として捕まえられる。`e.response["Error"]["Code"] == "NotImplemented"` で見分けられる
- 署名付きURLは生成しただけで、そのURLへのアクセスは試していない

スクリプトの先頭にはガードを入れた。`AWS_ENDPOINT_URL` が `https://127.0.0.1:` で始まらない、またはアカウントが `000000000000` でないときは、何もせずに終了する。

### 7. 中身を見るビューアーを作る

AWS Emulator には画面がない。S3 に何が入っているかをブラウザで眺められるよう、小さなビューアーを自作した。

```bash
make seed     # サンプルデータを入れる（任意）
make viewer   # http://127.0.0.1:9298 を開く。Ctrl+C で終了
```

- 左にバケットとテーブルの一覧、右に中身の表。行をクリックすると下に中身が出る
- S3 は、テキストと JSON を表示（JSON は字下げ）、画像はその場で表示、それ以外は情報だけ
- DynamoDB は、項目を表で表示。クリックで1件を JSON で全部表示
- 3秒ごとに自動で更新。エミュレーターを止めると「接続できません」になり、起動し直すと自動でつながる
- 見るだけ。アップロードも削除もできない

中身は `viewer.py` の1ファイル。Boto3 と Python の標準機能だけで動き、追加のインストールはない。

決めたこと。

| 項目 | 内容 |
| --- | --- |
| 対象 | S3 と DynamoDB だけ |
| 件数の上限 | ファイルは先頭1000件、項目は先頭100件。超えたら画面にその旨を出す |
| 大きなテキスト | 先頭の 1MB だけ表示し、「途中まで」と出す |
| S3 のフォルダ | 分けない。`logs/2026/11/access.csv` のようなフルの名前で並べる |

安全のために入れたもの。

| 対策 | 理由 |
| --- | --- |
| 接続先が `127.0.0.1` でなければ起動しない。アカウントが `000000000000` でなければ何も読まない | 本物のAWSを誤って見にいかないため |
| ビューアー自体も `127.0.0.1` だけで待ち受ける | 同じLANの他の機器から見えないようにするため |
| 宛先（Host）が `127.0.0.1:9298` か `localhost:9298` のリクエストだけ受ける | 別のサイトが名前を偽って読みに来る攻撃（DNSリバインディング）を防ぐため |
| S3 の中身やファイル名は、必ず文字として画面に入れる | HTML やスクリプトが入っていても実行させないため |
| 画像として返すのは PNG・JPEG・GIF・WebP だけ。SVG はテキスト扱い | SVG は中にスクリプトを書けるため |
| 外部のスクリプトや通信を禁じるヘッダー（CSP）を付ける | 上の対策に漏れがあったときの保険 |

確かめたこと。

- `<img src=x onerror=alert(1)>.txt` という名前のファイルも、`<script>` 入りの HTML ファイルも、文字のまま表示された
- `Host: evil.example` を付けたリクエストは 403 で断られた
- 画面を開いたまま CLI でファイルを消すと、数秒で画面からも消えた
- エミュレーターを再起動すると、新しい鍵と証明書に自動で切り替わってつながり直した
- 1.5MB のテキストは先頭だけ表示され、120件の項目は100件で打ち切られ、どちらも打ち切りが通知された

このとき見つけたこと。

- **エミュレーターは、1バケットに1000個までしかファイルを置けない。** 1001個目で `ValidationException`（`already holds the maximum of 1000 objects this emulator keeps per bucket`）が返る。本物のS3にはない制限。このため、ビューアーの「1000件で打ち切り」は実際には起きない
- **DynamoDB のテーブル削除は、少し遅れて一覧から消える。** 直後の `list-tables` にはまだ名前が残っていた

### 8. 停止

`make down`（中身は `docker stop`）。正常に止めると、上の4ファイルは自動で消える。空の `9297/` ディレクトリだけが残る。

## ドキュメントから変えたところ

ドキュメントは `~/.aws/emulator` をコンテナにマウントする。ここではプロジェクト内の `.emu-home/` を「仮のHOME」として渡している。

理由は、本物の `~/.aws` と置き場所を完全に分けたかったから。エミュレーターの認証情報が、本物の認証情報と同じディレクトリの下に並ぶのを避けた。

注意点がひとつある。エミュレーターは `config` に `ca.pem` の絶対パスを書く。ホストとコンテナでそのパスが一致していないと、CLIが証明書を見つけられない。`-e HOME=...` と `-v` の両方で同じパスを指定しているのはそのため。

## つまずいた点

**エンドポイントは HTTP ではなく HTTPS。**
`curl http://127.0.0.1:9297/...` ではつながらない。`--cacert` で `ca.pem` を渡す必要がある。CLIには `AWS_CA_BUNDLE` で渡す。

**環境変数は `AWS_ENDPOINT_URL` だけでは足りない。**
ドキュメントは8個の設定と、4個の `unset` をセットで案内している。特に `AWS_ACCESS_KEY_ID` や `AWS_PROFILE` がシェルに残っていると、エミュレーターの認証情報より優先されてしまう。Makefile では `env -u` で必ず外してから `aws` を呼ぶようにした。

**S3 は `localhost` ではなく `127.0.0.1` を使う。**
`localhost` だと、SDKによってはバケット名をホスト名の先頭に付けてしまう。

**テレメトリーの環境変数は、コンテナに渡さないと効かない。**
手元のシェルで `export EMULATOR_TELEMETRY=off` しても、コンテナには届かない。`docker run -e` で渡す。

**ビルド時に署名の警告が出る。**
`rpm -i` で `Header V4 RSA/SHA512 Signature, key ID d940f624: NOKEY` と出る。コンテナ内に検証用の公開鍵が入っていないという警告で、インストールは完了する。裏を返すと、ドキュメントの手順ではRPMの署名は検証されていない。

**起動ログに `0.0.0.0` の警告が出る。**
コンテナの中で全アドレスに待ち受けているという警告。ホスト側は `-p 127.0.0.1:9297:9297` で自分のMacにしか公開していないので、外からは届かない。逆に `-p 9297:9297` と書くと同じLANから届いてしまう。

**（Claude Code で作業する場合）サンドボックスの外で実行する必要がある。**
Dockerソケットへの接続、`brew install`、`127.0.0.1` への接続は、いずれもサンドボックス内では失敗した。brew は「ディレクトリの所有者を直せ」と `sudo chown` を案内してくるが、原因はサンドボックスなので従わなくてよい。

## 制約（ドキュメントより）

- リソースはメモリ上だけ。止めると消える。状態をリセットするAPIはなく、再起動する
- IAMポリシーやリソースポリシーを本物どおりには評価しない。権限まわりのテストには使えない
- アカウントは `000000000000` のひとつ、リージョンもひとつ
- 未実装のAPIは HTTP 501 `NotImplemented` を返す
- 長時間の稼働や負荷試験は、公式にテストされていない
- （試して見つけたもの）S3 は1バケットに1000個まで

## 所感

- 手順は少ない。Dockerfile を1行直して、ビルドして、起動するだけで動いた
- 起動が速く、メモリも200MiB弱。テストのたびに立ち上げて捨てる使い方に向いていそう
- 止めればすべて消えるので、後片付けを考えなくてよい。その代わり、データを残したい用途には使えない
- 環境変数の多さが一番の引っかかりどころ。Makefile などにまとめておかないと、毎回どれかを忘れる
- 未実装のAPIは黙って空を返すのではなく、501 ではっきり断る。「動いたつもり」になりにくい
- 今回しっかり試したのは S3 と DynamoDB の基本操作だけ。サービスごとの再現度は GitHub のリリースページに載っているとのことで、まだ見ていない

## まだ試していないこと

- JavaScript v3 の SDK からの接続。 `AWS_CA_BUNDLE` ではなく `NODE_EXTRA_CA_CERTS` が必要
- Terraform / CDK からの利用
- `--idle-timeout`（一定時間APIが呼ばれなければ自動で止まる）

## 参考

- [Getting started with Docker](https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-docker.html)
- [Point your SDK to the emulator](https://docs.aws.amazon.com/emulator/latest/userguide/point-sdk-to-emulator.html)
- [CLI reference](https://docs.aws.amazon.com/emulator/latest/userguide/cli-reference.html)
- [Telemetry](https://docs.aws.amazon.com/emulator/latest/userguide/telemetry.html)
- [aws/aws-emulator](https://github.com/aws/aws-emulator)
