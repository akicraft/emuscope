# よく使う操作の短縮形。中身はすべて ./emu（emuscope）を呼んでいるだけ。
#
#   make dev      エミュレーターと画面をまとめて起動（迷ったらこれ）
#   make up       エミュレーターを起動（テレメトリーOFF）
#   make down     停止（リソースはすべて消える）
#   make viewer   画面とAPIログ用の中継を起動  http://127.0.0.1:9298
#   make seed     サンプルデータを入れる
#   make smoke    AWS CLI から S3 と DynamoDB を一通り操作
#   make boto3    同じ確認を Boto3 から
#   make test     emuscope 自体のテスト（エミュレーターの中身は消える）
#   make test-offline  エミュレーターなしで走るテストだけ
#   make example  サンプルアプリのテスト（同上）
#   make doctor   うまく動かないときの点検
#   make env      環境変数を表示   使い方: eval "$(make -s env)"
#   make clean    停止して、イメージと生成ファイルを削除

EMU := ./emu
AWS := $(EMU) aws

BUCKET := emu-trial-bucket
TABLE  := emu-trial-table

.PHONY: dev build up down viewer seed health whoami smoke boto3 test test-offline example doctor env logs clean

dev:
	@$(EMU) dev

build:
	@$(EMU) build

up:
	@$(EMU) up

down:
	@$(EMU) down

viewer:
	@$(EMU) ui

seed:
	@$(EMU) seed

health:
	@$(EMU) status

whoami:
	$(AWS) sts get-caller-identity

smoke: whoami
	@echo "--- S3: バケット作成 → アップロード → 一覧 → 取得"
	$(AWS) s3 mb s3://$(BUCKET)
	echo "hello from emulator $$(date +%H:%M:%S)" | $(AWS) s3 cp - s3://$(BUCKET)/hello.txt
	$(AWS) s3 ls s3://$(BUCKET)/
	$(AWS) s3 cp s3://$(BUCKET)/hello.txt -
	@echo "--- DynamoDB: テーブル作成 → put → get"
	$(AWS) dynamodb create-table --table-name $(TABLE) \
		--attribute-definitions AttributeName=id,AttributeType=S \
		--key-schema AttributeName=id,KeyType=HASH \
		--billing-mode PAY_PER_REQUEST \
		--query 'TableDescription.[TableName,TableStatus]' --output text
	$(AWS) dynamodb wait table-exists --table-name $(TABLE)
	$(AWS) dynamodb put-item --table-name $(TABLE) \
		--item '{"id":{"S":"user-1"},"name":{"S":"emulator"},"count":{"N":"42"}}'
	$(AWS) dynamodb get-item --table-name $(TABLE) --key '{"id":{"S":"user-1"}}'
	@echo "--- 後片付け（繰り返し実行できるように消す）"
	$(AWS) s3 rb s3://$(BUCKET) --force
	$(AWS) dynamodb delete-table --table-name $(TABLE) --query 'TableDescription.TableName' --output text

boto3:
	@$(EMU) run -- .venv/bin/python examples/boto3_smoke.py

test:
	@$(EMU) run -- .venv/bin/python -m unittest discover -s tests

test-offline:
	@$(EMU) --version > /dev/null  # 初回なら .venv を作る（エミュレーターは起動しない）
	@.venv/bin/python -m unittest tests.test_offline

example:
	@$(EMU) run -- .venv/bin/python -m unittest discover -s examples -v

doctor:
	@$(EMU) doctor

env:
	@$(EMU) env

logs:
	@$(EMU) logs

clean:
	-$(EMU) down
	-docker image rm aws-emulator
	rm -rf .emu-home
