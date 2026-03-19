-- ============================================================
-- fix_financials_rls.sql — financials テーブルの RLS 補完
-- ============================================================
-- 
-- 目的:
--   financials テーブルに不足している INSERT / UPDATE / DELETE
--   ポリシーを追加する。
--   push スクリプトが anon key で書き込みできるようにする。
--   既存の SELECT ポリシーはそのまま維持。
--
-- 使い方:
--   Supabase Dashboard → SQL Editor → この SQL を貼り付けて実行
--
-- 再実行: 安全（DROP IF EXISTS + CREATE で冪等）
-- ============================================================

-- financials: INSERT ポリシー追加
DROP POLICY IF EXISTS "Anon can insert financials" ON financials;
CREATE POLICY "Anon can insert financials"
    ON financials FOR INSERT
    TO anon
    WITH CHECK (true);

-- financials: UPDATE ポリシー追加
DROP POLICY IF EXISTS "Anon can update financials" ON financials;
CREATE POLICY "Anon can update financials"
    ON financials FOR UPDATE
    TO anon
    USING (true);

-- financials: DELETE ポリシー追加
DROP POLICY IF EXISTS "Anon can delete financials" ON financials;
CREATE POLICY "Anon can delete financials"
    ON financials FOR DELETE
    TO anon
    USING (true);

-- ============================================================
-- 確認: 現在のポリシー一覧
-- ============================================================
SELECT '=== financials RLS policies ===' AS info;
SELECT policyname, cmd, permissive, roles
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename = 'financials'
ORDER BY policyname;

-- ============================================================
-- 2301 誤データ修復: FY 行の削除
-- ============================================================
DELETE FROM financials
WHERE ticker = '2301'
  AND period = '2026-10-31'
  AND quarter = 'FY';

-- 確認
SELECT '=== 2301 + 2026-10-31 after fix ===' AS info;
SELECT ticker, period, quarter, sales, operating_profit
FROM financials
WHERE ticker = '2301'
  AND period = '2026-10-31'
ORDER BY quarter;
