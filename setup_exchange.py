#!/usr/bin/env python3
"""One-time OAuth exchange for the Blog Bot Setup app.

The setup app (GitHub Pages) sends the user through Google consent and
returns with an authorization code. The app stores that code as the
SETUP_CODE repo secret and dispatches this workflow. This script:

1. Exchanges the code for a refresh token using the dedicated
   "Blog Bot Setup" OAuth client (NEW_CLIENT_ID / NEW_CLIENT_SECRET
   repo secrets, placed there when the client was created).
2. Writes REFRESH_TOKEN, CLIENT_ID and CLIENT_SECRET repo secrets
   (encrypted with the repo public key) using the temporary SETUP_PAT.
3. Deletes the temporary SETUP_CODE and SETUP_PAT secrets.

No token or secret value is ever printed.
"""
import base64
import os
import sys

import requests
from nacl import encoding, public

REDIRECT_URI = "https://testforany20-ship-it.github.io/bot-setup-app/"
API = "https://api.github.com"


def gh_headers(pat):
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def set_secret(repo, pat, name, value):
    r = requests.get(f"{API}/repos/{repo}/actions/secrets/public-key",
                     headers=gh_headers(pat), timeout=30)
    r.raise_for_status()
    key = r.json()
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode())
    r = requests.put(
        f"{API}/repos/{repo}/actions/secrets/{name}",
        headers=gh_headers(pat),
        json={"encrypted_value": base64.b64encode(sealed).decode(),
              "key_id": key["key_id"]},
        timeout=30)
    r.raise_for_status()


def delete_secret(repo, pat, name):
    requests.delete(f"{API}/repos/{repo}/actions/secrets/{name}",
                    headers=gh_headers(pat), timeout=30)


def main():
    code = os.environ["SETUP_CODE"]
    client_id = os.environ["NEW_CLIENT_ID"]
    client_secret = os.environ["NEW_CLIENT_SECRET"]
    pat = os.environ["SETUP_PAT"]
    repo = os.environ["REPO"]

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    data = r.json()
    if "refresh_token" not in data:
        print("ERROR: Google did not return a refresh token:",
              data.get("error", "unknown"))
        sys.exit(1)
    refresh_token = data["refresh_token"]
    print("Google exchange OK — refresh token received.")

    set_secret(repo, pat, "REFRESH_TOKEN", refresh_token)
    set_secret(repo, pat, "CLIENT_ID", client_id)
    set_secret(repo, pat, "CLIENT_SECRET", client_secret)
    print("Repo secrets updated: REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET.")

    delete_secret(repo, pat, "SETUP_CODE")
    delete_secret(repo, pat, "SETUP_PAT")
    print("Temporary setup secrets removed. Done.")


if __name__ == "__main__":
    main()
