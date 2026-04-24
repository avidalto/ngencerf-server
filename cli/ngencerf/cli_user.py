import getpass
import os

import qrcode
import requests

from ngencerf.cli_util import check_http_error

LOGIN_ENDPOINT = "http://localhost:8000/auth/login/"
MFA_SETUP_ENDPOINT = "http://localhost:8000/auth/mfa/setup/"
MFA_CONFIRM_SETUP_ENDPOINT = "http://localhost:8000/auth/mfa/setup/confirm/"
MFA_VERIFY_ENDPOINT = "http://localhost:8000/auth/mfa/verify/"
REFRESH_ENDPOINT = "http://localhost:8000/auth/jwt/refresh"
REGISTER_ENDPOINT = "http://localhost:8000/auth/users/"
ENV_FILE = os.path.join(os.path.expanduser("~"), ".ngencerf_env")


def save_to_env_file(key: str, value: str):
    """
    Save or update a key-value pair in ~/.ngencerf_env without duplication.

    If the key already exists, its value is updated. Otherwise, it's appended.
    """
    lines = []

    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        found = False
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")


def load_ngencerf_env():
    """
    Load variables from ~/.ngencerf_env into the environment if not already present.
    Ignores comments and blank lines.
    """
    if not os.path.exists(ENV_FILE):
        return

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key not in os.environ:
                os.environ[key] = value


def save_credentials_to_env_file(email: str, password: str):
    """
    Persist email and password to ~/.ngencerf_env so user isn't prompted every time.
    """
    save_to_env_file("NGEN_EMAIL", email)
    save_to_env_file("NGEN_PASSWORD", password)


def ngen_login() -> bool:
    """
    Ensures there is some ACCESS_TOKEN available.

    Logic:
      1. If ACCESS_TOKEN exists AND REFRESH_TOKEN exists → use access token (refresh will be attempted on 401).
      2. If ACCESS_TOKEN exists BUT no REFRESH_TOKEN → treat as expired, do full login.
      3. If no ACCESS_TOKEN but REFRESH_TOKEN exists → try refresh.
      4. If neither exist → full login.
    """
    load_ngencerf_env()

    access_token = os.environ.get("ACCESS_TOKEN")
    refresh_token = os.environ.get("REFRESH_TOKEN")

    # Case 1: Both tokens exist → trust access token, let 401 trigger refresh
    if access_token and refresh_token:
        print("Using existing access token (refresh token available).")
        return True

    # Case 2: Access token exists but no refresh token → treat as expired
    if access_token and not refresh_token:
        print("Access token found but no refresh token. Performing full login.")
        return perform_full_login()

    # Case 3: No access token, but refresh token exists → try refresh
    if refresh_token:
        print("No access token found. Attempting refresh...")
        if refresh_access_token():
            print("Refresh succeeded. Using new access token.")
            return True
        else:
            print("Refresh failed. Performing full login...")
            return perform_full_login()

    # Case 4: Neither token exists → full login
    print("No tokens found. Performing full login.")
    return perform_full_login()


def perform_full_login(_retry=False) -> bool:
    """
    Perform a full login using stored or prompted credentials.
    Supports MFA setup and verification flows.

    Will re-prompt once on failure (but never loops indefinitely).
    """
    print("Performing full login with email/password.")

    # Load latest env
    load_ngencerf_env()

    # Always load the latest email from env if available
    email = os.environ.get("NGEN_EMAIL") or os.environ.get("NGEN_USERNAME")

    # Decide whether to prompt for email
    # RULE:
    #   - If no email at all → must prompt
    #   - If retry → do NOT prompt (email is trusted)
    #   - If first attempt AND no saved password → allow optional override
    #   - If first attempt AND saved password exists → SKIP prompt entirely
    if not email:
        # Only prompt if email truly unknown
        email = input("ngenCerf email: ")
    elif not _retry and "NGEN_PASSWORD" not in os.environ:
        # Only offer override on FIRST attempt
        entered = input(f"ngenCerf email [{email}]: ").strip()
        if entered:
            email = entered
    # else: email prompt is skipped entirely

    # PASSWORD STRATEGY:
    #   - First attempt: use saved password if present, otherwise prompt
    #   - Retry attempt: always force prompt
    if _retry:
        print("Your saved credentials appear to be invalid. Please re-enter your password.")
        # On retry, always force prompt for new password
        os.environ.pop("NGEN_PASSWORD", None)
        password = getpass.getpass("ngenCerf password: ")
    else:
        # Use stored password or prompt if missing
        password = os.environ.get("NGEN_PASSWORD")
        if not password:
            password = getpass.getpass("ngenCerf password: ")

    # ───────────────────────────────
    # Step 1: Call /auth/login/
    # ───────────────────────────────
    payload = {"email": email, "password": password}
    print("Logging in with", email)
    response = requests.post(LOGIN_ENDPOINT, json=payload)

    # Handle failed login attempts
    if response.status_code != 200:
        if response.status_code == 401:
            print("Login failed — incorrect email or password.")
        else:
            check_http_error(response.status_code, response.text)
            print(f"Login failed with HTTP {response.status_code}. Please try again.")

        # Clear stored password for retry
        print("Saved password failed. Prompting for new credentials...")
        _clear_saved_password()
        os.environ.pop("NGEN_PASSWORD", None)

        if not _retry:
            print("Saved password failed — retrying full login...")
            return perform_full_login(_retry=True)
        else:
            print("Second login attempt failed. Aborting.")
            return False

    # Success case
    response_json = response.json()

    # ───────────────────────────────
    # Case 1: MFA NOT required → tokens returned
    # ───────────────────────────────
    if response_json.get("access"):
        access_token = response_json.get("access")
        refresh_token = response_json.get("refresh")

        os.environ["ACCESS_TOKEN"] = access_token
        os.environ["NGEN_EMAIL"] = email
        os.environ["NGEN_PASSWORD"] = password

        save_credentials_to_env_file(email, password)
        save_to_env_file("ACCESS_TOKEN", access_token)

        if refresh_token:
            os.environ["REFRESH_TOKEN"] = refresh_token
            save_to_env_file("REFRESH_TOKEN", refresh_token)

        print(f"{email} login successful.\n")
        return True

    # ───────────────────────────────
    # Case 2: MFA SETUP required
    # ───────────────────────────────
    if response_json.get("mfa_setup_required"):
        mfa_token = response_json.get("mfa_token")

        print("\nMFA setup required.")

        setup_resp = requests.post(
            MFA_SETUP_ENDPOINT,
            json={"mfa_token": mfa_token},
        )

        if setup_resp.status_code != 200:
            check_http_error(
                setup_resp.status_code,
                setup_resp.text,
                setup_resp.headers.get("Content-Type"),
            )
            return False

        setup_json = setup_resp.json()
        otpauth_url = setup_json.get("otpauth_url")

        print("\nOpening QR code for MFA setup...")

        try:
            img = qrcode.make(otpauth_url)
            img.show()
        except Exception as e:
            print(f"Failed to open QR code window: {e}")
            print("\nFallback: paste this into a QR generator or enter manually:")
            print(otpauth_url)

        code = input("Enter 6-digit code: ").strip()

        confirm_resp = requests.post(
            MFA_CONFIRM_SETUP_ENDPOINT,
            json={
                "mfa_token": mfa_token,
                "code": code,
            },
        )

        if confirm_resp.status_code != 200:
            check_http_error(
                confirm_resp.status_code,
                confirm_resp.text,
                confirm_resp.headers.get("Content-Type"),
            )
            return False

        confirm_json = confirm_resp.json()

        print("\nMFA setup complete. Save these recovery codes:\n")
        for c in confirm_json.get("recovery_codes", []):
            print(f"  {c}")

        input("\nPress Enter after saving recovery codes...")

        print("Restarting login to complete MFA...")
        return perform_full_login(_retry=_retry)

    # ───────────────────────────────
    # Case 3: MFA VERIFY required
    # ───────────────────────────────
    if response_json.get("mfa_required"):
        mfa_token = response_json.get("mfa_token")

        code = input("Enter MFA code or recovery code: ").strip()

        verify_resp = requests.post(
            MFA_VERIFY_ENDPOINT,
            json={
                "mfa_token": mfa_token,
                "code": code,
            },
        )

        if verify_resp.status_code != 200:
            check_http_error(
                verify_resp.status_code,
                verify_resp.text,
                verify_resp.headers.get("Content-Type"),
            )
            return False

        verify_json = verify_resp.json()

        access_token = verify_json.get("access")
        refresh_token = verify_json.get("refresh")

        os.environ["ACCESS_TOKEN"] = access_token
        os.environ["NGEN_EMAIL"] = email
        os.environ["NGEN_PASSWORD"] = password

        save_credentials_to_env_file(email, password)
        save_to_env_file("ACCESS_TOKEN", access_token)

        if refresh_token:
            os.environ["REFRESH_TOKEN"] = refresh_token
            save_to_env_file("REFRESH_TOKEN", refresh_token)

        print(f"{email} login successful.\n")
        return True

    print("Unexpected login response.")
    return False


def _clear_saved_password():
    """Remove only the saved password so user is reprompted."""
    print("Clearing invalid saved password from ~/.ngencerf_env...")
    os.environ.pop("NGEN_PASSWORD", None)

    if not os.path.exists(ENV_FILE):
        return

    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if not line.startswith("NGEN_PASSWORD="):
                    f.write(line)
    except Exception as e:
        print(f"Failed to clear password: {e}")


def _clear_auth_state():
    """Remove tokens and stored password to ensure a clean retry."""
    for key in ("ACCESS_TOKEN", "REFRESH_TOKEN", "NGEN_PASSWORD"):
        os.environ.pop(key, None)
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if not line.startswith(("ACCESS_TOKEN=", "REFRESH_TOKEN=", "NGEN_PASSWORD=")):
                    f.write(line)
        print("Cleared invalid tokens and password from ~/.ngencerf_env.")
    except Exception as e:
        print(f"Failed to clean invalid credentials: {e}")


def refresh_access_token() -> bool:
    """
    Attempts to refresh the access token using REFRESH_TOKEN in environment variables.
    Updates ~/.ngencerf_env if successful.

    Returns:
        True if refresh succeeded, False otherwise.
    """
    load_ngencerf_env()
    refresh_token = os.environ.get("REFRESH_TOKEN")
    if not refresh_token:
        return False

    payload = {"refresh": refresh_token}
    response = requests.post(REFRESH_ENDPOINT, json=payload)

    if response.status_code != 200:
        print(f"Refresh failed with status {response.status_code}: {response.text}")
        return False

    response_json = response.json()
    access_token = response_json.get("access")
    if not access_token:
        print("Refresh response missing access token.")
        return False

    os.environ["ACCESS_TOKEN"] = access_token
    save_to_env_file("ACCESS_TOKEN", access_token)
    print("Access token refreshed.\n")
    return True


def ngen_register(optional_email: str = None):
    """
    Registers a new user for the NGEN API. Prompts for password input and confirmation.
    """
    email = optional_email or os.environ.get("NGEN_EMAIL") or os.environ.get("NGEN_USERNAME")
    if not email:
        email = input("Enter a new email for ngenCerf registration: ")

    while True:
        password = getpass.getpass("Enter a new password for ngenCerf registration: ")
        password_confirm = getpass.getpass("Confirm your password: ")
        if password == password_confirm:
            break
        print("Passwords do not match. Please try again.")

    payload = {
        "email": email,
        "password": password,
        "re_password": password_confirm,
    }

    response = requests.post(REGISTER_ENDPOINT, json=payload)
    if check_http_error(response.status_code, response.text):
        print(f"User '{email}' registered successfully.")

def _save_tokens(access_token: str, refresh_token: str | None, email: str, password: str) -> None:
    os.environ["ACCESS_TOKEN"] = access_token
    os.environ["NGEN_EMAIL"] = email
    os.environ["NGEN_PASSWORD"] = password

    save_credentials_to_env_file(email, password)
    save_to_env_file("ACCESS_TOKEN", access_token)

    if refresh_token:
        os.environ["REFRESH_TOKEN"] = refresh_token
        save_to_env_file("REFRESH_TOKEN", refresh_token)


def _handle_token_response(response_json: dict, email: str, password: str) -> bool:
    access_token = response_json.get("access")
    refresh_token = response_json.get("refresh")

    if not access_token:
        return False

    _save_tokens(access_token, refresh_token, email, password)
    print(f"{email} login successful.\n")
    return True


def _prompt_mfa_code(prompt: str = "MFA code or recovery code: ") -> str:
    return input(prompt).strip()


def _print_recovery_codes(recovery_codes: list[str]) -> None:
    print("\nMFA setup completed.")
    print("Save these recovery codes now. They will not be shown again.\n")

    for code in recovery_codes:
        print(f"  {code}")

    print()