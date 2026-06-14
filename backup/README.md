# Backup Directory

本ディレクトリはシステム復旧用の実バックアップファイルを格納するためのディレクトリです。
「推定DDLは禁止」「実システムから取得した内容のみ保存」「DB変更禁止」という厳格なルールに基づいて管理されています。

## 現在の取得状況

### 現在取得済み
- `vercel_env_template.md`

### 未取得
- `supabase_schema.sql`
- `supabase_views.sql`
- `supabase_functions.sql`
- `supabase_indexes.sql`

## 未取得理由
現在の実行環境には、Supabaseデータベースに直接接続するためのDB接続情報（データベースパスワードを含む接続文字列 `SUPABASE_POSTGRES_URL` 等）や、Supabase CLIを操作するためのアクセストークン（`SUPABASE_ACCESS_TOKEN`）が存在しないためです。

## 取得に必要な権限
実データベースからDDL（スキーマ、ビュー、関数、インデックス）を正確に抽出するには、以下のいずれかの権限が必要です。

1. **データベースの接続パスワード**
2. または、**Supabase CLI のアクセストークン**

## 取得コマンド例
権限（情報）が用意できた場合、以下のコマンドを使用して実システムから各種DDLファイルを取得します。

### 1. データベース接続文字列（pg_dump）を使用する場合
```bash
# 接続文字列を用いたスキーマ抽出の例
pg_dump "postgresql://postgres:<password>@db.fvkvfekzoebcolssnteo.supabase.co:5432/postgres" --schema-only > supabase_schema.sql
```

### 2. Supabase CLI（アクセストークン）を使用する場合
```bash
# アクセストークンを設定
export SUPABASE_ACCESS_TOKEN="<your_personal_access_token>"

# プロジェクトをリンク
npx supabase link --project-ref fvkvfekzoebcolssnteo

# リモートデータベースのスキーマを抽出
npx supabase db pull
```
