# Splitting this into its own repository

This directory is a self-contained project — it has no dependency on the Python
code in this repository, and lives here only because the frontend repo could not
be created from the session that wrote it (the GitHub App token is not permitted
to create repositories in the `contextmachine` org: `403 Resource not accessible
by integration`).

To move it out, once an empty `contextmachine/a101-reinforcement-frontend`
exists:

```bash
# from the root of this repository, on the branch holding frontend/
git subtree split -P frontend -b frontend-only

git push git@github.com:contextmachine/a101-reinforcement-frontend.git frontend-only:main

# then drop it from this repository
git rm -r frontend && git commit -m "Move the frontend to its own repository"
git branch -D frontend-only
```

`git subtree split` rewrites the commits that touched `frontend/` into a history
rooted at this directory, so the new repository gets the files at its root with
their history intact.
