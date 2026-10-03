# Contributing to Dino Bot 🦖

First off, thank you for considering contributing to Dino Bot! Contributions from the community help make this project smarter, faster, and more robust.

## 🚀 How Can You Contribute?

### 1. Reporting Bugs
Before opening a new issue, please check existing issues to ensure it hasn't already been reported. When creating a bug report, include:
* Your operating system and Python version (`python --version`).
* The operating mode used (`--mode 1`, `--mode 2`, or `--mode 3`).
* Clear steps to reproduce the bug.
* Any relevant terminal logs or error tracebacks.

### 2. Suggesting Enhancements
Got an idea for improved jump-arc calculations or game feature automation?
* Open an issue describing the proposed feature or improvement.
* Explain the problem it solves and why it would be beneficial.

### 3. Submitting Pull Requests
1. Fork the repository and create your feature branch from `main`:
   ```bash
   git checkout -b feature/amazing-feature
   ```
2. Set up your environment and test changes locally:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
3. Ensure your Python code is clean and passes basic syntax checks.
4. Commit your changes with clear, descriptive commit messages.
5. Push to your fork and submit a Pull Request to the `main` branch.

## 📜 Code Style & Conventions
* Follow standard [PEP 8](https://peps.python.org/pep-0008/) Python guidelines.
* Keep embedded JavaScript (`BOT_JS`) well-commented, especially when adjusting physics logic or collision box bounds.

Thank you for helping improve Dino Bot!