#!/usr/bin/env bash
# @name: ask_in_bash
# @description: EXAMPLE showing how a bash script asks the user a question mid-run and uses the answer. Prints back what you chose.
# @timeout: 60

echo "[ask] Which environment do you want to target (dev/staging/prod)?"
read -r ENVIRONMENT

echo "[secret] API key for ${ENVIRONMENT:-that environment}:"
read -r API_KEY

echo "Target environment: ${ENVIRONMENT:-none}"
echo "Received an API key of ${#API_KEY} characters."
echo "(this example does nothing with it)"
