# Getting Started

## Requirements

- Python 3.9+
- OpenAI-compatible LLM backend with prompt-caching support
- A model with strong tool/function calling (recommended: Gemma 4)

## Installation

1. Clone the repo:
   ```
   git clone https://github.com/OrbFrontend/Orb.git
   ```
2. Verify Python 3 is installed: `python3 --version`
3. Enter `Orb` folder and start the app:
   - Linux/Mac: `./run_unix.sh`
   - Windows: `run_windows.bat`

## First Run

1. Open the **Endpoints** sidepanel and configure your Writer and Agent LLM endpoints.
   - The same model can serve both roles (suitable for local hosting).
   - Two separate models give better results at higher token cost.
   - Endpoints use a tree structure: each endpoint may have many models, each model has its own params and custom prompts.

2. Create or import a character in the **Characters** tab.

3. Click the character and send your first message.
