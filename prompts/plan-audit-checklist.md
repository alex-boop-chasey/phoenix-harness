# Overlooked areas: the planner's self-audit checklist

One line per area, `- <id>: <what to check>`. The planner walks every id after drafting the plan and
writes the result into `PLAN.md` under `## Self-audit` as a table `| Area | Status | Where / why |`
(status `addressed` with where in the plan it lives, or `n/a` with the reason, at least 8 characters).
`python3 -m hands.planaudit check PLAN.md` enforces it; the runner runs it on every planner output.

- auth: Who can do what: sign-in, sessions or tokens, logout, protected routes and admin surfaces.
- errors: Error states and failure messages for every network call, form and background job; retries.
- empty-states: What every list, table, search and dashboard shows with no data yet.
- validation: Input validation on both client and server: lengths, formats, injection, upload types and sizes.
- accessibility: Keyboard navigation, focus order, labels and alt text, contrast, reduced motion, screen readers.
- responsive: Layouts and interactions at phone, tablet and desktop widths; touch targets; viewport meta.
- performance: Image sizes and formats, fonts, bundle size, caching, Core Web Vitals, slow-network behaviour.
- seo-meta: Titles, descriptions, canonical URLs, Open Graph and Twitter cards, sitemap, robots, structured data.
- security-headers: CSP, HSTS, frame and content-type options, referrer policy, CORS, cookie flags.
- secrets: Where every key lives, how it reaches the app, that none reaches the client bundle, logs or git.
- logging: What is logged where, with which levels, without secrets or personal data; how errors are reported.
- data-and-backups: Where data lives, its schema and migrations, backups, restore drills, retention and deletion.
- deploy: How the app is built and shipped, by which pipeline, to which host, with which environment variables.
- rollback: How a bad release is reverted, how fast, and whether data migrations are reversible.
- rate-limits: Abuse protection on forms, APIs, auth endpoints and third-party calls; quotas and spam defences.
- dependency-age: Every pinned dependency is recent enough to be supported and old enough to be stable.
- licences: Licences of dependencies, fonts, icons, images and templates are compatible with the project.
- copy-and-i18n: Final copy, tone, dates, numbers and currencies; which languages ship and how text is organised.
- legal-pages: Imprint, privacy policy, terms, cookie notice and contact details the project needs by law.
- analytics-consent: Which analytics run, what they collect, and the consent flow they need before loading.
- tests-review-focus: What the acceptance tests cover, what they miss on purpose, and what reviewers check by hand.
- docs-conventions: docs/conventions.md states the stack, versions, scripts, paths and style every builder follows.
- style-refs-applied: The frozen style references in refs/style-refs were read and their findings shaped the design.
- setup-record-used: The scaffold recorded in refs/setup.json (framework, libraries, versions) is what the plan builds on.
