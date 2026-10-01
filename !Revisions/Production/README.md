# Production Revisions

This folder is separate from the rest of `!Revisions/` on purpose.

`!Revisions/` (one level up) is every dev iteration -- one zip per version
bump, whether or not it ever left pcapps's machine. `!Revisions/Production/`
is the much smaller subset that actually got pushed to a live host (the lab
server, or wherever else this ends up running). Keeping them apart means a
backup of what's actually live never gets buried under the next day's dev
snapshot, and nobody has to guess which of 30 dev zips is the one currently
running in production.

## Convention

- Name each one `Curveball Tools Finalv<N>.zip` -- `Finalv1`, `Finalv2`, etc.
  N increments only when a NEW version actually gets deployed live, not on
  every dev version bump.
- Copy the exact zip that was deployed (same bytes as what `!Revisions/`
  already has for that version) -- don't rebuild it separately, so there's
  no chance of the "prod backup" silently drifting from what was actually
  shipped.
- Never delete or overwrite an existing Finalv<N> -- add the next number.
  The whole point is a durable backup, not a single "latest" pointer.
- Note the deploy date and where it was deployed (e.g. lab host, path) in
  this file when you add one, so there's a paper trail independent of git
  history or the live host itself.

## Log

| File | Version | Deployed | Where | Notes |
|------|---------|----------|-------|-------|
| Curveball Tools Finalv1.zip | v2.12 | 2026-09-15 | (not yet deployed -- current best local build) | First entry in this folder. Actual live lab deployment (`noe-util-01a.lab1.bwnet.us`) is still v1.96 per the main CHANGELOG -- this is a backup of the current dev state, not a claim that v2.12 is live yet. Update this row (or add a new one) once it's actually pushed. |
