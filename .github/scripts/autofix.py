#!/usr/bin/env python3
"""
Gemini-powered auto-fix agent for the Self-Healing Deploy pipeline.

The model proposes a complete corrected target file. The repository's checker
is the authority: a pull request is opened only if that checker passes.
"""

import os
import subprocess
import sys

from google import genai
from google.genai import types


# ---- Settings, overridable by GitHub Actions ----
MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_ID = os.environ.get("RUN_ID", "")
TARGET = os.environ.get("TARGET_FILE", "index.html")
CHECKER = os.environ.get("CHECKER", "check-html.py")
BASE_BRANCH = os.environ.get("BASE_BRANCH", "main")

MAX_LOG_CHARS = 8000
MAX_OUTPUT_TOKENS = 8192
MAX_ATTEMPTS = 1

BEGIN, END = "<<<BEGIN_FILE>>>", "<<<END_FILE>>>"


def sh(cmd):
    """Run a command and return its result without raising."""
    return subprocess.run(cmd, text=True, capture_output=True)


def read(path):
    with open(path, encoding="utf-8") as file:
        return file.read()


def write(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)


def failure_log():
    """Retrieve the failed GitHub Actions run log."""
    if not RUN_ID:
        return ""

    result = sh(["gh", "run", "view", RUN_ID, "--log-failed"])
    log = result.stdout or result.stderr
    return log[-MAX_LOG_CHARS:]


def looks_like_a_code_problem(log):
    """Only attempt an HTML repair when the repository checker rejected it."""
    return ("FAIL  " + TARGET) in log


SYSTEM = f"""You repair a single file for a CI teaching project.

You will be given a CI failure log, the checker script, and the current file.
Return the complete corrected file and nothing else.

Rules:
- Fix only what the checker is complaining about.
- Preserve unrelated wording, styling, layout, and behavior.
- Return the WHOLE file, from the first character to the last.
- Do not use Markdown fences or add an explanation.
- Wrap the full file in these exact markers:

{BEGIN}
...the entire corrected file...
{END}
"""


def ask_the_model(log, checker_src, current):
    """Make one Gemini API call and return its text response."""
    if not API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add it under GitHub "
            "Settings > Secrets and variables > Actions."
        )

    prompt_parts = [
        "The deploy pipeline rejected this file.",
        "",
        "=== CI failure log ===",
        log or "(no log available)",
        "",
        f"=== {CHECKER} (defines what valid means) ===",
        checker_src,
        "",
        f"=== current {TARGET} ===",
        current,
    ]

    client = genai.Client(api_key=API_KEY)
    response = client.models.generate_content(
        model=MODEL_ID,
        contents="\n".join(prompt_parts),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.1,
        ),
    )

    if not response.text:
        raise RuntimeError("Gemini returned an empty text response.")
    return response.text


def extract(reply):
    """Extract the complete file between the required markers."""
    if BEGIN not in reply or END not in reply:
        return None
    body = reply.split(BEGIN, 1)[1].split(END, 1)[0]
    return body.strip("\n") + "\n"


def run_checker():
    """Run the same checker used by the deployment pipeline."""
    result = sh([sys.executable, CHECKER, TARGET])
    return result.returncode, (result.stdout + result.stderr).strip()


def open_pull_request(check_output):
    branch = "autofix/run-" + (RUN_ID or "manual")

    sh(["git", "config", "user.name", "github-actions[bot]"])
    sh([
        "git", "config", "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    ])

    existing = sh([
        "git", "ls-remote", "--exit-code", "--heads", "origin", branch
    ])
    if existing.returncode == 0:
        print(f"Branch {branch} already exists on the remote.")
        print("Not opening a second PR for the same run.")
        return 0

    commands = [
        ["git", "checkout", "-b", branch],
        ["git", "add", TARGET],
        ["git", "commit", "-m", f"fix: repair {TARGET} so the deploy check passes"],
        ["git", "push", "-u", "origin", branch],
    ]

    for command in commands:
        result = sh(command)
        if result.returncode != 0:
            print("Git step failed: " + " ".join(command))
            print(result.stdout + result.stderr)
            return 1

    body = (
        f"The deploy failed and `{CHECKER}` rejected `{TARGET}`. "
        "This PR contains a proposed code fix.\n\n"
        "### Checker result\n\n"
        f"```\n{check_output}\n```\n\n"
        "The workflow ran the repository checker before opening this PR. "
        "Please review the diff before merging.\n\n"
        f"### How this fix was produced\n\n"
        f"Gemini model: `{MODEL_ID}` via the Google Gemini API.\n\n"
        f"Generated from workflow run `{RUN_ID or 'manual'}`.\n"
    )

    result = sh([
        "gh", "pr", "create",
        "--base", BASE_BRANCH,
        "--head", branch,
        "--title", f"fix: repair {TARGET} so the deploy check passes",
        "--body", body,
    ])
    print(result.stdout + result.stderr)
    return result.returncode


def main():
    original = read(TARGET)

    if RUN_ID:
        log = failure_log()
        if not looks_like_a_code_problem(log):
            print("The checker did not reject the target file.")
            print("No Gemini request will be made and no PR will be opened.")
            print("Tail of the available failure log:")
            print(log[-2000:] or "(no log available)")
            return 0
    else:
        # Manual dispatch has no prior run ID. Check the current file first.
        checker_code, checker_output = run_checker()
        if checker_code == 0:
            print("The checker passes already. No Gemini request is needed.")
            print(checker_output)
            return 0
        log = checker_output
        if not looks_like_a_code_problem(log):
            print("The checker output does not identify a repairable target-file failure.")
            print(log or "(no checker output)")
            return 1

    checker_src = read(CHECKER)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"--- attempt {attempt} of {MAX_ATTEMPTS}: asking Gemini model {MODEL_ID} ---")
        try:
            reply = ask_the_model(log, checker_src, original)
        except Exception as error:
            print(f"Gemini API request failed ({type(error).__name__}): {error}")
            print("The original target file will be restored.")
            write(TARGET, original)
            return 1

        fixed = extract(reply)
        if not fixed or len(fixed) < 20:
            print("Gemini did not return a complete file between the required markers.")
            write(TARGET, original)
            return 1

        write(TARGET, fixed)
        checker_code, checker_output = run_checker()
        print(checker_output)

        if checker_code == 0:
            return open_pull_request(checker_output)

        print("Gemini's proposed file did not pass the checker.")
        print("No PR will be opened. Restoring the original file.")
        write(TARGET, original)
        return 1

    write(TARGET, original)
    return 1


if __name__ == "__main__":
    sys.exit(main())
