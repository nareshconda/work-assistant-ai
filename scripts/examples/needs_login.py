#!/usr/bin/env python3
# @name: needs_login
# @description: EXAMPLE of a script that asks for a password. Shows how to prompt for a credential that is masked in the chat, kept out of the transcript, and never sent to the language model. It only reports the length of what you typed.
# @timeout: 60
"""
A script runs as a SUBPROCESS, so a plain input() would hit EOF -- its stdin is
not the chat. Instead, announce the question on stdout with a marker and then
read ONE line from stdin:

    print("[secret] ...", flush=True)   -> masked credential card in the chat
    print("[ask] ...",    flush=True)   -> an ordinary question

The prompt line itself is never echoed into the transcript, and a [secret]
answer is replaced with ******** before anything is sent to the model.
"""
import sys

print("This example needs a credential to continue.")

print("[secret] Password for the example service:", flush=True)
secret = sys.stdin.readline().strip()

print("[ask] Which environment should it connect to (dev/prod)?", flush=True)
env = sys.stdin.readline().strip()

if not secret:
    print("No password given; nothing to do.")
else:
    print(f"Received a credential of {len(secret)} characters.")
    print(f"Environment chosen: {env or '(none)'}")
    print("A real script would use it here, then let it go out of scope.")
    print("It was never written to disk and never sent to the model.")
