# MFA Authentication – UI Integration Guide

## Endpoints

1. `POST /auth/login/`
2. `POST /auth/mfa/setup/`
3. `POST /auth/mfa/setup/confirm/`
4. `POST /auth/mfa/verify/`

---

## Standard Error Format

All errors follow this structure:

```json
{
  "response_type": "error",
  "error_code": "SOME_CODE",
  "ui_action": "ACTION",
  "message": "Human readable message"
}
```

### UI Actions

| ui_action           | Meaning                   |
| ------------------- | ------------------------- |
| STAY_ON_LOGIN       | Show error on login       |
| RETURN_TO_LOGIN     | Clear state → go to login |
| RETRY_SETUP_CONFIRM | Stay on setup confirm     |
| RETRY_MFA_VERIFY    | Stay on verify            |
| RESTART_MFA_SETUP   | Restart setup flow        |

**UI must rely on `ui_action`, not message text.**

---

# 1. Login (`/auth/login/`)

## Request

```json
{
  "email": "user@example.com",
  "password": "password"
}
```

## Responses

### A. Login Complete (MFA disabled)

```json
{
  "access": "...",
  "refresh": "...",
  "message": "Login successful"
}
```

→ Store tokens → enter app

---

### B. MFA Setup Required (first-time user)

```json
{
  "mfa_setup_required": true,
  "mfa_token": "...",
  "message": "MFA setup required before login."
}
```

→ Store `mfa_token` → go to MFA Setup

---

### C. MFA Verification Required (returning user)

```json
{
  "mfa_required": true,
  "mfa_token": "...",
  "message": "MFA verification required."
}
```

→ Store `mfa_token` → go to MFA Verify

---

# 2. MFA Setup (`/auth/mfa/setup/`)

## Request

```json
{
  "mfa_token": "..."
}
```

## Response

```json
{
  "otpauth_url": "otpauth://totp/...",
  "message": "Scan QR code to complete MFA setup."
}
```

## UI Behavior

* Generate QR code from `otpauth_url`
* Optionally display manual secret
* Prompt user for 6-digit code

---

# 3. MFA Setup Confirm (`/auth/mfa/setup/confirm/`)

## Request

```json
{
  "mfa_token": "...",
  "code": "123456"
}
```

## Response

```json
{
  "message": "MFA setup completed successfully.",
  "recovery_codes": [
    "abc123-def456",
    "..."
  ]
}
```

## UI Behavior

* Show recovery codes immediately
* Provide:

  * **Download button (required)**
  * Copy option
* Warn user these are **one-time only**

### After success

→ Call `/auth/login/` again
→ Expect `mfa_required=true`

---

# 4. MFA Verify (`/auth/mfa/verify/`)

## Request

```json
{
  "mfa_token": "...",
  "code": "123456"
}
```

## Code types supported

* TOTP (6-digit): `123456`
* Recovery code: `abc123-def456`

## Response

```json
{
  "access": "...",
  "refresh": "...",
  "message": "MFA verification successful"
}
```

→ Store tokens → enter app

---

# UI Screens

## 1. Login

```
Email
Password
[ Login ]
```

---

## 2. MFA Setup

```
Scan QR Code

[ QR IMAGE ]

or manual entry

Enter 6-digit code:
[ _ _ _ _ _ _ ]

[ Confirm Setup ]
```

---

## 3. Recovery Codes (NEW)

```
MFA Setup Complete

Save these recovery codes:

abc123-def456
...

[ Download ]
[ Copy ]
[ Continue ]
```

---

## 4. MFA Verify

```
Enter code:

[ _ _ _ _ _ _ ]

(or recovery code)

[ Verify ]
```

---

# QR Code Generation

Input:

```
otpauth://totp/...
```

### Example (JS)

```javascript
import QRCode from "qrcode";
QRCode.toCanvas(canvas, otpauthUrl);
```

---

# Full Flow

## First-time user

1. Login
2. `mfa_setup_required`
3. Call `/auth/mfa/setup/`
4. Show QR
5. User enters code
6. Call `/auth/mfa/setup/confirm/`
7. Show recovery codes (download required)
8. Call `/auth/login/`
9. `mfa_required`
10. Call `/auth/mfa/verify/`
11. Enter app

---

## Returning user

1. Login
2. `mfa_required`
3. Enter TOTP or recovery code
4. Call `/auth/mfa/verify/`
5. Enter app

---

## Error Handling Summary

* Use `ui_action` to determine navigation
* Do not parse message text
* Common cases:

  * Expired token → RETURN_TO_LOGIN
  * Bad code → RETRY screen
  * Setup broken → RESTART_MFA_SETUP

---

## Notes

* TOTP rotates every ~30 seconds
* If expired → user enters next code
* No countdown required
* Recovery codes:

  * One-time use
  * Work without authenticator
  * Must be saved by user

---
