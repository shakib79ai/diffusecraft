# Contributing to DiffuseCraft

Thanks for your interest in improving DiffuseCraft. This is a small,
community-friendly project — contributions of all sizes are welcome.

## Getting Started

1. Fork the repository and clone your fork.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Guidelines

- Keep `app.py` framework-thin — generation logic should stay easy to follow for someone new to diffusion models.
- Match existing code style (type hints where useful, small focused functions).
- Test changes locally with both a CUDA GPU (if available) and the CPU fallback path before submitting.
- Update `README.md` if you add a model, parameter, or feature that changes user-facing behavior.

## Submitting a Pull Request

1. Ensure the app runs without errors: `python app.py`.
2. Write a clear PR description: what changed and why.
3. Reference any related issue.
4. Keep PRs focused — one feature or fix per PR is easier to review.

## Reporting Issues

Please include:
- Your OS, Python version, and GPU/CUDA version (or "CPU only").
- Steps to reproduce.
- Full error traceback, if applicable.

## Code of Conduct

Be respectful and constructive. This project welcomes contributors of all
experience levels.
