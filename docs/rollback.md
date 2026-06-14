# Rollback Procedures (Disaster Recovery Pack)

## Scenario: Frontend UI is broken after a deployment

### 1. Instant Vercel Rollback (Recommended)
If the live site is broken, instantly revert the production traffic to the last known good deployment without changing code.

1. List recent deployments for the correct project:
   ```bash
   vercel ls company-memo-app
   ```
2. Identify the URL of the last stable deployment (e.g., `https://company-memo-edmsqqleb-...vercel.app`).
3. Reassign the production alias:
   ```bash
   vercel alias set https://company-memo-edmsqqleb-...vercel.app company-memo-app.vercel.app
   ```
4. Verify the live site (`https://company-memo-app.vercel.app/tdnet-alerts`) is fixed.

### 2. Git Revert (Fixing the Codebase)
After mitigating the immediate issue via Vercel alias, fix the GitHub repository.

1. Find the bad commits:
   ```bash
   git log
   ```
2. Revert the commits:
   ```bash
   git revert <bad-commit-id>
   ```
3. Push the fix:
   ```bash
   git push origin main
   ```
4. Vercel will auto-build the reverted code.
