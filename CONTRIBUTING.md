# Contributing to hpc-runner

Thank you for your interest in contributing to **hpc-runner**.
This project welcomes improvements, bug fixes, and new features.
The guidelines below keep contributions simple and consistent.

## How to Contribute

### 1. Fork the Repository
Create your own fork of the project:

- Go to: https://github.com/fractalclockwork/hpc-runner
- Click **Fork**

This gives you a personal copy where you can make changes safely.

### 2. Clone Your Fork

```bash
git clone https://github.com/<your-username>/hpc-runner.git
cd hpc-runner
```

### 3. Add the Upstream Remote

```bash
git remote add upstream https://github.com/fractalclockwork/hpc-runner.git
```

Use this to keep your fork up to date.

### 4. Create a Feature Branch

```bash
git checkout -b feature/my-change
```

Use a descriptive branch name.

### 5. Make Your Changes
Keep changes focused and incremental.
If adding new configs, solvers, or job types, follow existing patterns.

### 6. Commit Your Work

```bash
git add .
git commit -m "Brief description of the change"
```

If your organization requires DCO signoff:

```bash
git commit -s -m "Add feature X"
```

### 7. Push Your Branch

```bash
git push origin feature/my-change
```

### 8. Open a Pull Request
Open a PR from your fork:

```
<your-username>:feature/my-change → fractalclockwork:main
```

Include a short summary of what the change does and why.

## Code Style

- Follow the existing structure and naming conventions.
- Keep functions small and focused.
- Prefer clarity over cleverness.
- Add comments where behavior is non-obvious.

## Testing

If your change affects execution logic, please include tests or examples.
Ensure the project runs cleanly before submitting a PR.

## Keeping Your Fork Updated

Before starting new work:

```bash
git checkout main
git fetch upstream
git merge upstream/main
git push origin main
```

## Questions or Ideas?

Open an issue on GitHub to discuss proposals before implementing large changes.

Thanks again for contributing!
