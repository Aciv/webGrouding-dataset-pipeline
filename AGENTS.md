# Development conventions

- Group source by responsibility: domain, browser, application, config, infrastructure, review, tests.
- Keep domain types independent of browser and filesystem implementations.
- Copy existing proven View collection functions where applicable; record source and local changes in docs/MIGRATION.md.
- Do not modify the original collectors while developing this independent project.
- Raw DOM facts are candidates, not verified detection ground truth. Do not silently select labels or invent cross-viewport semantic identities.
- Every exported function needs a purpose/coordinate/lifecycle comment. Keep browser, persistence and presentation separate.
- Close pages, contexts, servers and browsers in finally blocks. A failed page must not cancel unrelated page jobs.
- Keep tests small, deterministic and local. Record real collection evidence separately from fixture tests.
- Preserve unknown visibility, clipping and occlusion as unknown; never equate aria-hidden with pixel invisibility.
