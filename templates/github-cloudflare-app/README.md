# __REPOSITORY__

Standard Astro + Cloudflare Workers application created by the approved repository bootstrap.

## Checks

```bash
npm ci
npm test
npm run typecheck
npm run build
npm run deploy:dry-run
```

Pull requests run one CI verification, CodeRabbit review, `Validate PR`, and an approval-gated Cloudflare preview. Pushes to `main` deploy production. CI never receives Cloudflare credentials.
