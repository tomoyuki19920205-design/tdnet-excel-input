# Deployment (Disaster Recovery Pack)

## Vercel Frontend Deployment

### Production Project
- **Project Name**: `company-memo-app`
- **Primary Domain**: `https://company-memo-app.vercel.app`

### Deployment Flow
1. **Local Development**: Code changes are made and tested on `localhost:3000`.
2. **Git Commit & Push**: Changes are pushed to the `main` branch of the `tdnet-excel-input` repository.
3. **Vercel CI/CD**: Vercel automatically detects the push to `main` and triggers a build for `company-memo-app`.
4. **Build Process**: `npm run build` runs Next.js build.
5. **Production Release**: Upon successful build, Vercel updates the production alias.

### WARNING: CLI Deployment
Do **NOT** use `vercel --prod` from the local `web` directory unless absolutely necessary and perfectly configured. Doing so bypasses the GitHub CI/CD pipeline, and can lead to:
- Missing environment variables.
- Incorrect tailwind CSS builds.
- Deployment to the wrong Vercel project (e.g., `web` instead of `company-memo-app`).
