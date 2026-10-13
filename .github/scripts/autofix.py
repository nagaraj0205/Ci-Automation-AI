#!/usr/bin/env python3
"""
Auto-fix agent for the Self-Healing Deploy pipeline.

When a deploy fails, this reads the real failure log, asks a model on Amazon
Bedrock for a corrected index.html, checks that answer with the very same
script the pipeline uses, and only then opens a pull request.

This is deliberately NOT an autonomous agent. The model gets exactly one turn
and returns exactly one thing: the full text of a fixed index.html. Every
decision that actually matters -- is this even a code problem, is the fix
valid, does a PR get opened -- is made down here in plain Python you can read
top to bottom.

That split is the point. The model is allowed to be wrong. check-html.py is
what decides whether its answer gets anywhere near production.

Any Bedrock model that supports the Converse API works here. Nothing in this
file is tied to a particular model vendor.
"""

import os
import subprocess
import sys

import boto3
import botocore
from botocore.exceptions import ClientError

# ---- settings, all77 overridable from the workflow ----
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "openai.gpt-oss-120b-1:0")
REGION = os.environ.get("BEDROCK_REGION", "us-west-2")
RUN_ID = os.environ.get("RUN_ID", "")
TARGET = os.environ.get("TARGET_FILE", "index.html")
CHECKER = os.environ.get("CHECKER", "check-html.py")
BASE_BRANCH = os.environ.get("BASE_BRANCH", "master")

MAX_LOG_CHARS = 20000   # tail of the failure log we send to the model
MAX_ATTEMPTS = 2        # one first try, one retry with the checker's complaint

# The model wraps the file in these so we can pull it back out without
# worrying about markdown fences or a chatty preamble.
BEGIN, END = "<<<BEGIN_FILE>>>", "<<<END_FILE>>>"


def sh(cmd):
    """Run a command and hand back the result. Never raises."""
    return subprocess.run(cmd, text=True, capture_output=True)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------------------------------------------------------------- step 1
def failure_log():
    """The real error, straight from the run that failed."""
    if not RUN_ID:
        return ""
    r = sh(["gh", "run", "view", RUN_ID, "--log-failed"])
    log = r.stdout or r.stderr
    return log[-MAX_LOG_CHARS:]


def looks_like_a_code_problem(log):
    """
    Only our own checker failing counts as "the code is broken".

    Expired credentials, a missing S3 backup, a CloudFront timeout -- those are
    infrastructure. No model is going to fix them by editing HTML, so we do not
    even ask. check-html.py prints exactly "FAIL  index.html", two spaces.
    """
    return "FAIL  " + TARGET in log


# ---------------------------------------------------------------- step 2
SYSTEM = """You repair a single HTML file for a teaching project.

You will be given a CI failure log, the checker script that produced it, and
the current file. Return the complete corrected file and nothing else.

Rules:
- Fix only what the checker is complaining about. Change nothing else: not the
  wording, not the styling, not the layout.
- Return the WHOLE file, from the first character to the last.
- Wrap it in the two markers exactly as shown, with no markdown fences and no
  explanation:

<<<BEGIN_FILE>>>
...the entire corrected file...
<<<END_FILE>>>"""


def bedrock_client():
    """One place to build the client, and one place to catch a stale SDK.

    converse() arrived in botocore 1.34.116. An older copy -- the apt-packaged
    one at /usr/lib/python3/dist-packages, typically -- raises a bare
    AttributeError from deep inside botocore, which reads like a bug in this
    script rather than an SDK that is simply too old. Say it plainly instead.
    """
    client = boto3.client("bedrock-runtime", region_name=REGION)
    if not hasattr(client, "converse"):
        sys.exit("botocore " + botocore.__version__ + " has no Converse API. "
                 "Install boto3 1.34.116 or newer: pip install -U boto3")
    return client


def ask_the_model(log, checker_src, current, complaint=None):
    """One Converse call. One answer. No tools, no agent loop."""
    parts = [
        "The deploy pipeline rejected this file.",
        "",
        "=== CI failure log ===",
        log or "(no log available)",
        "",
        "=== " + CHECKER + " (this defines what valid means) ===",
        checker_src,
        "",
        "=== current " + TARGET + " ===",
        current,
    ]
    if complaint:
        parts += [
            "",
            "=== your previous attempt STILL FAILED the checker ===",
            complaint,
            "",
            "Read that complaint carefully and fix it properly this time.",
        ]

    resp = bedrock_client().converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": "\n".join(parts)}]}],
        inferenceConfig={"maxTokens": 16000},
    )
    blocks = resp["output"]["message"]["content"]
    return "\n".join(b["text"] for b in blocks if "text" in b)


def extract(reply):
    """Pull the file back out from between the markers."""
    if BEGIN not in reply or END not in reply:
        return None
    body = reply.split(BEGIN, 1)[1].split(END, 1)[0]
    return body.strip("\n") + "\n"


# ---------------------------------------------------------------- step 3
def run_checker():
    """The same gate the pipeline runs. This, not the model, is the authority."""
    r = sh([sys.executable, CHECKER, TARGET])
    return r.returncode, (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- step 4
def open_pull_request(check_output):
    branch = "autofix/run-" + (RUN_ID or "manual")

    sh(["git", "config", "user.name", "github-actions[bot]"])
    sh(["git", "config", "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com"])

    already = sh(["git", "ls-remote", "--exit-code", "--heads", "origin", branch])
    if already.returncode == 0:
        print("Branch " + branch + " already exists on the remote.")
        print("Not opening a second PR for the same run.")
        return 0

    steps = [
        ["git", "checkout", "-b", branch],
        ["git", "add", TARGET],
        ["git", "commit", "-m", "fix: repair " + TARGET + " so the deploy check passes"],
        ["git", "push", "-u", "origin", branch],
    ]
    for cmd in steps:
        r = sh(cmd)
        if r.returncode != 0:
            print("git step failed: " + " ".join(cmd))
            print(r.stdout + r.stderr)
            return 1

    body = (
        "The deploy failed and `" + CHECKER + "` is what rejected it, so this is a code\n"
        "problem rather than an AWS one. This PR fixes `" + TARGET + "`.\n"
        "\n"
        "### What the checker says now\n"
        "\n"
        "```\n" + check_output + "\n```\n"
        "\n"
        "That is the same command the pipeline runs before it uploads anything, so a\n"
        "passing result here is the passing result the deploy will get.\n"
        "\n"
        "### How this fix was produced\n"
        "\n"
        "A single Converse call to `" + MODEL_ID + "` on Amazon Bedrock proposed the\n"
        "corrected file. The workflow then verified it by running `" + CHECKER + "`.\n"
        "Had that check failed, no PR would have been opened -- the logic lives in\n"
        "`.github/scripts/autofix.py` and it is short enough to read.\n"
        "\n"
        "Generated from run " + (RUN_ID or "manual") + ".\n"
    )

    r = sh(["gh", "pr", "create",
            "--base", BASE_BRANCH,
            "--head", branch,
            "--title", "fix: repair " + TARGET + " so the deploy check passes",
            "--body", body])
    print(r.stdout + r.stderr)
    return r.returncode


# ---------------------------------------------------------------- main
def main():
    log = failure_log()

    if not looks_like_a_code_problem(log):
        print("The checker did not reject the file, so this is not a code problem.")
        print("Most likely infrastructure: credentials, S3, or CloudFront.")
        print("Opening no PR. The tail of the failure log:")
        print("")
        print(log[-2000:] or "(no log)")
        return 0

    checker_src = read(CHECKER)
    original = read(TARGET)
    complaint = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print("--- attempt " + str(attempt) + " of " + str(MAX_ATTEMPTS)
              + ": asking " + MODEL_ID + " ---")
        try:
            reply = ask_the_model(log, checker_src, original, complaint)
        except ClientError as e:
            print("Bedrock refused the call: " + str(e))
            write(TARGET, original)
            return 1

        fixed = extract(reply)
        if not fixed or len(fixed) < 500:
            complaint = "You did not return the whole file between the two markers."
            print(complaint)
            continue

        write(TARGET, fixed)
        code, output = run_checker()
        print(output)

        if code == 0:
            return open_pull_request(output)

        complaint = output   # feed the checker's own words straight back in

    # Nothing worked. Leave the tree exactly as we found it.
    write(TARGET, original)
    print("")
    print("The model could not produce a file that passes the checker.")
    print("Opening no PR. A human should look at this one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
