# ChatGPT Userscripts

## 用途

`chatgpt_recent_scheduled_tasks_deduper.user.js`は、ChatGPTの「最近の項目」に増える次のScheduled Taskを、Task名ごとにDOM上の最新1件だけが見える状態へ畳みます。

- `Company News Monitor Task01`～`Company News Monitor Task08`
- `Sector Weekly Worker`

## 影響

ブラウザ上の表示だけを変更します。Scheduled Task本体、実行スケジュール、通常チャット、Productionデータには影響しません。

## 導入

1. TampermonkeyなどのUserscript managerで新しいUserscriptを作成します。
2. `chatgpt_recent_scheduled_tasks_deduper.user.js`の全文を貼り付けます。
3. Userscriptを保存して有効にします。
4. `https://chatgpt.com/`を再読み込みします。

## 期待結果

「最近の項目」内では対象Taskが最大9種類だけ表示され、同名Taskの古い行が非表示になります。それ以外の通常チャットやSidebar項目は変更されません。

## 元に戻す

Userscript managerでこのUserscriptを無効化または削除し、ChatGPTを再読み込みしてください。非表示だった行が再び表示されます。
