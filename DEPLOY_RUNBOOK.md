# Deployment Runbook (tdnet-excel-input)

> [!CAUTION]
> **【絶対禁止】このリポジトリからの Vercel Deploy は禁止されています**
> 
> 過去に `web-psi-six-68.vercel.app` などの無関係なプロジェクトへ上書きデプロイする事故が複数回発生しました。
> そのため、本リポジトリおよび配下のディレクトリからの Vercel デプロイ機能は封鎖されています。

## 禁止事項
1. **このリポジトリから Vercel へデプロイすること**
2. **`npx vercel --prod` などの Vercel CLI コマンドを直打ちすること**

## 正しいデプロイ先
- Viewer 機能を含む本番デプロイは、**必ず `company-memo-app` リポジトリからのみ**実施してください。

誤って `npm run deploy` などを実行した場合、安全装置が作動してエラー終了(exit 1)するようになっています。
