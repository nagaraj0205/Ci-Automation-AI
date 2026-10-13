#!/usr/bin/env bash
# ============================================================
#  6F School of IT - CI/CD pre-flight check
#  Run this BEFORE you push. It does not change anything -
#  it just tells you exactly what is missing, and how to fix it.
#
#  Windows: open "Git Bash" (comes with Git) and run:  bash check.sh
#  Mac/Linux:                                          bash check.sh
# ============================================================
set -u

G='\033[32m'; R='\033[31m'; Y='\033[33m'; B='\033[1m'; N='\033[0m'
PASS=0; FAIL=0; WARN=0

ok()   { printf "  ${G}PASS${N}  %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  ${R}FAIL${N}  %s\n" "$1"; printf "        ${B}fix:${N} %s\n" "$2"; FAIL=$((FAIL+1)); }
warn() { printf "  ${Y}WARN${N}  %s\n" "$1"; WARN=$((WARN+1)); }
head_() { printf "\n${B}%s${N}\n" "$1"; }

printf "\n${B}6F CI/CD pre-flight check${N}\n"
printf "=========================\n"

WF=".github/workflows/deploy-s3.yml"

# ---------------------------------------------------------- 1. tools
head_ "1. Tools on your machine"
command -v git >/dev/null 2>&1 \
  && ok "git is installed" \
  || bad "git not found" "install Git, then reopen this terminal"
command -v aws >/dev/null 2>&1 \
  && ok "aws cli is installed" \
  || warn "aws cli not found locally - not required, the pipeline has its own"
command -v gh  >/dev/null 2>&1 \
  && ok "gh cli is installed" \
  || warn "gh cli not found - some checks below will be skipped"

# ---------------------------------------------------------- 2. repo + branch
head_ "2. Your repository"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -n "$BRANCH" ]; then
  ok "inside a git repo, current branch: $BRANCH"
else
  bad "this folder is not a git repository" "run: git init && git remote add origin YOUR-REPO-URL"
fi

if git remote get-url origin >/dev/null 2>&1; then
  ok "remote 'origin' is set -> $(git remote get-url origin)"
else
  bad "no remote named 'origin'" "run: git remote add origin https://github.com/YOU/selfheal-site.git"
fi

# ---------------------------------------------------------- 3. workflow file
head_ "3. The pipeline file"
if [ -f "$WF" ]; then
  ok "$WF exists"

  # which branch does the workflow listen to?
  TRIG="$(grep -oE 'branches:[[:space:]]*\[[^]]*\]' "$WF" | head -1)"
  if [ -z "$TRIG" ]; then
    warn "could not read the 'branches:' line - check it by eye"
  elif [ -n "$BRANCH" ] && printf '%s' "$TRIG" | grep -q "$BRANCH"; then
    ok "workflow triggers on your branch ($TRIG)"
  else
    bad "workflow trigger does not match your branch" \
        "you are on '$BRANCH' but the workflow says '$TRIG' - make them the same, or nothing will ever run"
  fi

  # is the bucket actually filled in?
  BUCKET="$(grep -oE '^[[:space:]]*(S3_)?BUCKET:[[:space:]]*[A-Za-z0-9._-]+' "$WF" \
            | head -1 | awk -F': *' '{print $2}')"
  if [ -z "${BUCKET:-}" ]; then
    warn "no BUCKET / S3_BUCKET line found in the env: block"
  elif [ "$BUCKET" = "yourname-web-2026" ]; then
    bad "bucket is still the placeholder ($BUCKET)" \
        "put your real bucket name in the env: block of $WF"
  else
    ok "bucket is set: $BUCKET"
  fi
else
  bad "$WF not found" "create it - Project 4, Step 6"
  BUCKET=""
fi

# ---------------------------------------------------------- 4. the site file
head_ "4. The site file and its health marker"
if [ -f index.html ]; then
  ok "index.html exists"
  HITS="$(grep -c 'SITE-OK' index.html || true)"
  if [ "${HITS:-0}" -eq 0 ]; then
    warn "no SITE-OK marker - the health check will FAIL and roll back"
    printf "        (that is correct if you are demoing rollback on purpose)\n"
  elif [ "${HITS:-0}" -gt 1 ]; then
    warn "SITE-OK appears $HITS times - grep matches ANY of them"
    printf "        the check can never fail while an extra copy sits in your text\n"
  else
    ok "health marker SITE-OK present exactly once"
  fi
else
  bad "index.html not found" "create it - Project 4, Step 4"
fi

# ---------------------------------------------------------- 5. AWS reachability
head_ "5. AWS"
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  ok "local AWS credentials work (account $(aws sts get-caller-identity --query Account --output text 2>/dev/null))"
  if [ -n "${BUCKET:-}" ] && [ "$BUCKET" != "yourname-web-2026" ]; then
    if aws s3 ls "s3://$BUCKET" >/dev/null 2>&1; then
      ok "bucket s3://$BUCKET is reachable"
    else
      bad "cannot read s3://$BUCKET" "check the bucket name and region, and that your IAM user can access it"
    fi
  fi
else
  warn "no local AWS credentials - fine, the pipeline uses GitHub secrets instead"
fi

# ---------------------------------------------------------- 6. GitHub side
head_ "6. GitHub secrets and token scope"
if command -v gh >/dev/null 2>&1; then
  SCOPES="$(gh auth status 2>&1 | grep -i 'Token scopes' | head -1 || true)"
  if [ -z "$SCOPES" ]; then
    warn "not logged in to gh - run: gh auth login"
  elif printf '%s' "$SCOPES" | grep -q "workflow"; then
    ok "your token has the 'workflow' scope"
  else
    bad "token is missing the 'workflow' scope" \
        "without it, pushing .github/workflows/ is REJECTED. Run: gh auth refresh -h github.com -s workflow && gh auth setup-git"
  fi

  SECRETS="$(gh secret list 2>/dev/null || true)"
  for s in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    if printf '%s' "$SECRETS" | grep -q "$s"; then
      ok "secret $s is set"
    else
      bad "secret $s is missing" "add it: Repo -> Settings -> Secrets and variables -> Actions"
    fi
  done
else
  warn "gh cli not installed - check by hand that your token has the 'workflow' scope"
  printf "        and that AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY exist in repo secrets\n"
fi

# ---------------------------------------------------------- verdict
printf "\n=========================\n"
printf "  ${G}%s passed${N}   ${R}%s failed${N}   ${Y}%s warnings${N}\n" "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf "\n${G}${B}Ready to push.${N}  git add . && git commit -m \"deploy\" && git push\n\n"
  exit 0
else
  printf "\n${R}${B}Fix the FAIL lines above first.${N} Each one has a 'fix:' under it.\n\n"
  exit 1
fi
