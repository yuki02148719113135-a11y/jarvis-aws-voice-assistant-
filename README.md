# jarvis — AWS上に構築する音声AIアシスタント

Amazon Nova 2 Sonic を使った音声対話アシスタントを、Terraform で AWS 上に構築するプロジェクト。
個人開発だが、**「作って壊す」を前提としたインフラ設計とコスト最適化の判断**を主題に置いている。

現在 Sprint 0（インフラ基盤）完了。音声機能の実装は Sprint 1 以降。

---

## 構成

| 役割 | 使用サービス |
|---|---|
| 音声対話 | Amazon Bedrock / Nova 2 Sonic |
| 実行基盤 | ECS Fargate（0.5 vCPU / 1 GiB、Spot） |
| 接続 | ALB（WebSocket、スティッキーセッション有効） |
| 記憶 | DynamoDB（オンデマンド、TTL 付き） |
| イメージ | ECR |
| IaC | Terraform 1.14 / AWS Provider 6.x |
| リージョン | ap-northeast-1 |

![VPCリソースマップ](docs/vpc-resource-map.png)

*構成A。ALB の要件を満たすためパブリックサブネットを 2AZ に配置し、Fargate タスクとゲートウェイエンドポイント（S3 / DynamoDB）を同一 VPC 内に置いている。*

### Nova 2 Sonic

音声を直接受け取り音声を返す speech-to-speech モデル。
STT → LLM → TTS と繋ぐ従来構成に比べて応答遅延が小さい。
クロスリージョン推論には非対応のため、東京リージョンへ直接リクエストを投げる必要がある。

![Bedrockモデルアクセス](docs/bedrock-model-access.png)

*モデル ID は `amazon.nova-2-sonic-v1:0`。東京リージョンで `AUTHORIZED` / `AVAILABLE` を確認済み。*

---

## 設計判断

### 1. state を2層に分離した

Terraform を `persistent/` と `ephemeral/` に分け、state ファイルも別管理にしている。

```
jarvis/
├── persistent/   # 消さない：S3(tfstate), DynamoDB, ECR, IAM
├── ephemeral/    # 毎回消す：VPC, ALB, ECS
└── app/          # Dockerfile, FastAPI
```

分離しないと、`destroy` のたびに会話履歴とコンテナイメージが消えて、毎回ゼロから作り直すことになる。
ephemeral 側は `terraform_remote_state` で persistent の output を参照し、ARN をハードコードしない。

![S3のstate2層](docs/s3-state-layers.png)

*同一バケット内で `persistent/` と `ephemeral/` にキーを分けている。ephemeral を destroy しても persistent の state は残る。*

ECR を persistent 側に置いたのは、イメージのビルドと push をやり直すと
apply の所要時間が跳ね上がるため。

![ECRイメージ一覧](docs/ecr-images.png)

*イメージサイズ 61.5 MB。ライフサイクルポリシーで直近5世代のみ保持。タグなしのマニフェストも残る点に注意。*

![DynamoDBテーブル](docs/dynamodb-table.png)

*パーティションキー `session_id`、ソートキー `created_at`、オンデマンド課金。`expires_at` で TTL を設定済み。*

### 2. 実行基盤に AgentCore Runtime ではなく Fargate を選んだ

音声は長時間の双方向ストリーミングなので、Lambda では成立しない。
AgentCore Runtime のほうが構築は速いが、以下の理由で Fargate を選択した。

- WebSocket の保持、スケーリング、監視を自分で設計する必要があり、学習効果が高い
- ネットワーク構成を含めて IaC で完結できる
- SAP（Solutions Architect Professional）の出題領域と重なる

![ECSサービス](docs/ecs-service.png)

*タスク1件が実行中、デプロイステータス成功、ターゲット正常性1件。*

![ECSタスク詳細](docs/ecs-task.png)

*0.5 vCPU / 1 GiB、起動タイプ Fargate。プロビジョニングからイメージ pull、実行までのライフサイクル。*

### 3. VPC エンドポイントをやめてパブリックサブネットにした

当初はプライベートサブネット＋VPC エンドポイントで設計したが、**コストを計算して方針を変えた**。

プライベートサブネットで Fargate を動かすには、Bedrock だけでなく
ECR（api / dkr）と CloudWatch Logs のエンドポイントも必要になる。

| 構成 | 内訳 | 時間単価 |
|---|---|---|
| A. パブリックサブネット + パブリックIP | ALB + Fargate + EIP | **約 6 円** |
| B. プライベート + インターフェース型 4本 | A + エンドポイント（1AZ） | 約 15 円 |
| C. プライベート + NAT ゲートウェイ | A + NAT | 約 16 円 |

インターフェース型は 1AZ あたり 1本 月7.3ドル前後。4本を 2AZ に置くと月58ドルとなり、
NAT ゲートウェイ（月33ドル前後）より高くなる。**「エンドポイントのほうが安い」はこの規模では成立しない。**

一方、ゲートウェイ型（S3 / DynamoDB）は無料なので、構成 A でも残している。

構成 A ではセキュリティグループでインバウンドを ALB からのみに制限し、
パブリックサブネットに置くことによるリスクを抑えている。

なお構成 A は、Sprint 2 で Google カレンダーや Notion の API を叩く際の
外部通信経路の問題も同時に解決する（VPC エンドポイントは AWS サービスにしか効かない）。

### 4. ALB の idle_timeout を 300 秒に延長した

既定の 60 秒だと、会話の無音が1分続いた時点で WebSocket が切断される。
音声アプリでは致命的なので、あらかじめ延長している。

### 5. スティッキーセッションを有効にした

Nova Sonic のセッションはタスクのメモリ上にあるため、
再接続時に別タスクへ振られると会話が失われる。
Sprint 3 で状態を DynamoDB へ外出しするまでの暫定措置。

![curlのレスポンスヘッダー](docs/curl-health-headers.png)

*`AWSALB` Cookie が返っており、スティッキーセッションが実際に機能していることが確認できる。*

### 6. 3構成を変数で切り替えられるようにした

`use_vpc_endpoints` と `use_nat` の bool 変数だけで A / B / C を切り替えられる。
ブランチを分けずに済むので、コスト比較を `terraform apply` 一発で再現できる。

```bash
terraform apply                                # 構成A（常用）
terraform apply -var="use_vpc_endpoints=true"  # 構成B
terraform apply -var="use_nat=true"            # 構成C
```

---

## 実測値

| 項目 | 値 |
|---|---|
| ephemeral リソース数 | 19 |
| apply 所要時間 | **3分6秒**（うち ALB 作成が 2分16秒） |
| persistent リソース数 | 11 |
| コンテナイメージサイズ | 61.5 MB |
| 稼働コスト（構成A） | 約 6 円/時 |
| Nova 2 Sonic | 会話 1分あたり約 2.3 円 |

![apply完了](docs/apply-complete.png)

*19リソースを3分6秒で構築。`active_config` の output で、どの構成で立っているかを判別できるようにしている。*

![ターゲットグループ正常](docs/target-group-healthy.png)

*ALB のヘルスチェック通過。ECS で最も詰まりやすい部分で、タスクのバインドアドレスを `0.0.0.0` にすることが条件。*

apply 完了から `/health` が通るまで、ヘルスチェック通過を含めて約1分。

---

## 運用

コストの大半は ALB とエンドポイントの**時間課金**なので、
タスクを止める（`desired_count = 0`）だけでは不十分。
使わないときは ephemeral ごと destroy する。

```bash
# 作業開始
cd ephemeral && terraform apply

# 作業終了
terraform destroy
```

persistent 層は残るため、会話履歴もコンテナイメージも保持される。
月あたりの保持コストは数円。

---

## 詰まった点

### terraform plan は IAM 権限不足を検出しない

plan が参照するのは `sts:GetCallerIdentity` のみで、
実際の作成権限は apply まで検証されない。

結果として、S3 の4リソースだけ作成された状態で
DynamoDB / ECR / IAM が `AccessDenied` で停止した。

ただし**state には成功分が記録されているため、権限を付与して apply を再実行すれば続きから作られる**。
作り直しは不要だった。

### backend 移行は2段階

tfstate を置く S3 バケット自体を Terraform で作るため、初回はローカル state で apply し、
バケット作成後に `backend` ブロックを有効化して `terraform init -migrate-state` で移行する。

`backend` ブロックがコメントアウトされたままだと `-migrate-state` は何も聞いてこず、
移行されないまま成功したように見えるので注意。

### プロバイダキャッシュの破損

`terraform apply` の実行中に中断すると、
`.terraform/providers` のチェックサム検証が落ちて以降のコマンドが全滅する。

```bash
rm -rf .terraform && terraform init
```

state は S3 にあるため、この操作でリソースが失われることはない。

### ECR のイメージ削除

`terraform destroy` はイメージが残っているとリポジトリを削除できない。
タグなしのマニフェストも残るため、`--force` が必要。

```bash
aws ecr delete-repository --repository-name jarvis --force
```

---

## 今後

- **Sprint 1** — Nova 2 Sonic との双方向ストリーミング実装、TLS 対応（ブラウザのマイク利用には HTTPS が必須）
- **Sprint 2** — 外部ツール連携（カレンダー参照）
- **Sprint 3** — 会話状態の DynamoDB 永続化、スティッキーセッションの解消
- **Sprint 4** — Cognito 認証、GitHub Actions による CI/CD
