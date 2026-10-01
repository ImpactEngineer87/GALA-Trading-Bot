# GALA10 v3.3.11 Validation Notes

Regression coverage added in tests/test_v3_3_11_processing_training_visibility.py.

Covered behavior:
- HTML 403 Access Denied is classified as transient edge failure.
- A transient refresh failure does not set reauthorization_required.
- Per-ticker processing API and dashboard sources exist.
- Retraining supplemental-sample visibility is persisted.
- Version/branding is 3.3.11.

Existing v3.3.9/v3.3.10 visibility tests were updated to the current version while preserving the Todos-filter regression.

Production verification before REAL:
1. Schwab Test succeeds.
2. Read-only broker reconciliation succeeds.
3. Processing table lists all configured tickers.
4. Several complete 1-minute cycles update all rows.
5. No unexpected REAL positions/orders remain.
