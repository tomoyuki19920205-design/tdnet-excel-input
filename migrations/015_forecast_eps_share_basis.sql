-- Preserve disclosed FEPS while recording how to convert it to the share
-- basis effective on the disclosure date.  Effective market actions after
-- disclosure then unwind this factor as the raw price changes basis.
ALTER TABLE public.per_share_data
ADD COLUMN IF NOT EXISTS forecast_eps_basis_factor double precision NOT NULL DEFAULT 1.0;

COMMENT ON COLUMN public.per_share_data.forecast_eps_basis_factor IS
'Multiplier from raw disclosed forecast_eps basis to disclosure-date share basis';
