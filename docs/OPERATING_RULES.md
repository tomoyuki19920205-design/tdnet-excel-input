# Operating Rules

本ドキュメントは、本システムにおけるリポジトリ管理、デプロイ、障害対応等の厳格な運用ルールを集約したものです。

## 1. Repository Rules

### `company-memo-app`
- **用途**: 本番フロントエンド、Company Viewer、TDNET Alerts、`company-memo-app.vercel.app`
- **原則**: UI変更は必ずこのリポジトリで行うこと。

### `tdnet-excel-input`
- **用途**: TDNET抽出パイプライン、Pythonバッチ、Supabaseマイグレーション、`docs/`、`backup/`
- **原則**: 原則としてフロントエンド修正は禁止。

### Legacy Frontend Warning
- `tdnet-excel-input/web` は旧フロントエンド資産である。
- このディレクトリを修正する前に必ず以下を確認し、確認が取れない限り修正禁止とする。
  1. `company-memo-app` に同名機能が存在しないか
  2. 本番URLが参照しているリポジトリはどこか
  3. Vercelの接続先はどこか

## 2. Deployment Rules

### 禁止事項
- `vercel --prod` のローカルからの直接実行
- Vercel CLIからの本番Alias変更
- リポジトリ未確認状態での修正
- `company-memo-app` 以外へのUI修正

### 必須事項
- GitHub Push経由のみでデプロイを行うこと
- 修正前に必ず対象リポジトリを明示すること
- 本番URLとの対応表を確認すること

## 3. Vercel Rules

- **正しいVercelプロジェクト**: `company-memo-app`
- **正しいProduction Alias**: `company-memo-app.vercel.app`
- **本番反映前後の確認手順**: GitHubへPush後、Vercelダッシュボードでビルド完了を待ち、必ず本番URL（`https://company-memo-app.vercel.app/tdnet-alerts` 等）をブラウザで開いて目視確認する。

## 4. Rollback Rules (障害発生時の原則)

本番障害（画面崩れなど）が発生した場合、以下の原則を徹底すること。

- 原因調査より先に復旧
- 追加修正禁止（障害発生中にコード修正で直そうとしない）
- 正常な過去のDeploymentへのRollback（Vercel管理画面からの即時ロールバック等）を最優先
- 復旧後に原因分析を行う

## 5. Scratch Script Rules

- 使い捨ての調査用・テスト用スクリプトは `scratch/` ディレクトリ等にまとめ、プロジェクトのルートディレクトリを散らかさないこと。

## 6. Completion Report Rules

### UI変更時の事前提出（必須）
実装開始前に以下を提出すること。これらが承認されるまで実装禁止とする。
1. 修正対象リポジトリ
2. 修正対象ファイル
3. 対応するVercelプロジェクト
4. 対応する本番URL
5. 修正内容の要約（3行以内）
6. 影響を受ける画面一覧
7. ロールバック方法

### 作業完了報告時の提出項目
ローカル修正のみで完了報告することは禁止。本番URL確認なしで「反映済み」と言わないこと。デプロイ完了を報告する際は必ず以下を提出する。
1. コミットID
2. Push先リポジトリ
3. Vercel Deployment URL
4. Production Alias URL
5. BEFORE/AFTER確認結果
6. 実際に確認できたこと（本番URLへのアクセス目視結果等）

## 7. Backup Rules

- **推定DDLは禁止**: データベース接続情報が存在しない状態で、API等から推測してCREATE文などのDDLを作成することは禁止。
- **実システムから取得した内容のみ保存**: 実システム環境から直接抽出・取得できたファイルや内容のみをGitの `backup/` 等に保存すること。DBの直接変更等も禁止。
