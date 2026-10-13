-- REVIEW ONLY. Do not execute until the production DB change is approved.
-- All amounts are consolidated actuals in millions of JPY.
-- The source documents and before/after values are in finance_profit_review.md.
BEGIN;

CREATE TEMP TABLE finance_expected_ordinary (
    ticker text, period text, quarter text, amount numeric
) ON COMMIT DROP;
INSERT INTO finance_expected_ordinary VALUES
    ('8316','2026-03-31','1Q',483337),
    ('8316','2026-03-31','2Q',1278123),
    ('8316','2026-03-31','3Q',1899055),
    ('8316','2026-03-31','FY',2303350),
    ('8410','2026-03-31','1Q',6607),
    ('8410','2026-03-31','2Q',14814),
    ('8410','2026-03-31','3Q',21884),
    ('8410','2026-03-31','FY',30165),
    ('8750','2026-03-31','1Q',96056),
    ('8750','2026-03-31','2Q',343351),
    ('8750','2026-03-31','3Q',597712),
    ('8750','2026-03-31','FY',753688),
    ('7350','2026-03-31','1Q',3799),
    ('7350','2026-03-31','2Q',7688),
    ('7350','2026-03-31','3Q',12083),
    ('7350','2027-03-31','1Q',5312);

-- Abort if an existing J-Quants ordinary row differs from the audited value.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM finance_expected_ordinary e
        JOIN public.canonical_financials c
          ON c.source_row_key = 'cf|' || e.ticker || '|' || e.period || '|' || e.quarter || '|ordinary_profit|jquants|'
        WHERE c.metric <> 'ordinary_profit' OR c.value IS DISTINCT FROM e.amount
           OR c.unit <> 'millions_jpy' OR c.source <> 'jquants'
    ) THEN
        RAISE EXCEPTION 'Existing ordinary-profit row differs from the reviewed manifest';
    END IF;
END $$;

INSERT INTO public.canonical_financials
    (ticker, period, quarter, metric, value, unit, source, source_priority, source_row_key)
SELECT e.ticker, e.period, e.quarter, 'ordinary_profit', e.amount,
       'millions_jpy', 'jquants', 2,
       'cf|' || e.ticker || '|' || e.period || '|' || e.quarter || '|ordinary_profit|jquants|'
FROM finance_expected_ordinary e
ON CONFLICT (source_row_key) DO NOTHING;

DO $$
BEGIN
    IF (SELECT count(*) FROM finance_expected_ordinary e
        JOIN public.canonical_financials c
          ON c.source_row_key = 'cf|' || e.ticker || '|' || e.period || '|' || e.quarter || '|ordinary_profit|jquants|'
         AND c.metric = 'ordinary_profit' AND c.value = e.amount
         AND c.unit = 'millions_jpy' AND c.source = 'jquants') <> 16 THEN
        RAISE EXCEPTION 'Ordinary-profit insertion check failed';
    END IF;
END $$;

-- Remove only nine derived rows whose "operating_profit" is the disclosed
-- ordinary profit. Raw J-Quants records and filing history are retained.
CREATE TEMP TABLE finance_bad_canonical_op (
    id bigint, ticker text, period text, quarter text, amount numeric, source text
) ON COMMIT DROP;
INSERT INTO finance_bad_canonical_op VALUES
    (43358856,'7350','2026-03-31','FY',15799,'jquants'),
    (43427180,'7350','2026-03-31','FY',15799,'jquants_bank_proxy'),
    (42919115,'7350','2027-03-31','1Q',5312,'jquants_earnings_summary'),
    (43632166,'7350','2027-03-31','1Q',5312,'jquants'),
    (40069532,'8316','2027-03-31','1Q',693136,'jquants_earnings_summary'),
    (44726782,'8316','2027-03-31','1Q',693136,'jquants'),
    (41414530,'8410','2027-03-31','1Q',10802,'jquants_earnings_summary'),
    (43634162,'8410','2027-03-31','1Q',10802,'jquants'),
    (43634326,'8750','2027-03-31','1Q',251057,'jquants');

DO $$
DECLARE matched integer;
BEGIN
    SELECT count(*) INTO matched FROM finance_bad_canonical_op e
    JOIN public.canonical_financials c
      ON c.id = e.id AND c.ticker = e.ticker AND c.period = e.period
     AND c.quarter = e.quarter AND c.metric = 'operating_profit'
     AND c.value = e.amount AND c.source = e.source;
    IF matched <> 9 THEN
        RAISE EXCEPTION 'Canonical OP before-image changed: % of 9 rows match', matched;
    END IF;
    DELETE FROM public.canonical_financials c USING finance_bad_canonical_op e
    WHERE c.id = e.id AND c.ticker = e.ticker AND c.period = e.period
      AND c.quarter = e.quarter AND c.metric = 'operating_profit'
      AND c.value = e.amount AND c.source = e.source;
    GET DIAGNOSTICS matched = ROW_COUNT;
    IF matched <> 9 THEN
        RAISE EXCEPTION 'Canonical OP removal affected % rows, expected 9', matched;
    END IF;
END $$;

-- The Viewer reads canonical rows. Correct the seven known stale legacy
-- financials OP fields as well, without changing sales or other metrics.
CREATE TEMP TABLE finance_bad_legacy_op (
    ticker text, period text, quarter text, amount numeric
) ON COMMIT DROP;
INSERT INTO finance_bad_legacy_op VALUES
    ('7350','2027-03-31','1Q',5312),
    ('8316','2026-03-31','2Q',246614),
    ('8316','2026-03-31','FY',906817),
    ('8410','2027-03-31','1Q',10802),
    ('8750','2026-03-31','2Q',234560),
    ('8750','2026-03-31','FY',300264),
    ('8750','2027-03-31','1Q',251057);

DO $$
DECLARE matched integer;
BEGIN
    SELECT count(*) INTO matched FROM finance_bad_legacy_op e
    JOIN public.financials f
      ON f.ticker = e.ticker AND f.period = e.period AND f.quarter = e.quarter
     AND f.operating_profit = e.amount AND f.source = 'jquants';
    IF matched <> 7 THEN
        RAISE EXCEPTION 'Legacy OP before-image changed: % of 7 rows match', matched;
    END IF;
    UPDATE public.financials f SET operating_profit = NULL
    FROM finance_bad_legacy_op e
    WHERE f.ticker = e.ticker AND f.period = e.period AND f.quarter = e.quarter
      AND f.operating_profit = e.amount AND f.source = 'jquants';
    GET DIAGNOSTICS matched = ROW_COUNT;
    IF matched <> 7 THEN
        RAISE EXCEPTION 'Legacy OP correction affected % rows, expected 7', matched;
    END IF;
END $$;

COMMIT;
