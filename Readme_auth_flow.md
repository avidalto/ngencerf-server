# MFA UI Integration Guide

## Endpoints

1. `POST /auth/login/`
2. `POST /auth/mfa/setup/`
3. `POST /auth/mfa/setup/confirm/`
4. `POST /auth/mfa/verify/`

---

## Standard error shape

All MFA/login error responses may include:

```json
{
  "response_type": "error",
  "error_code": "MFA_TOKEN_EXPIRED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA token has expired. Please log in again."
}
```

The UI should use `ui_action` for routing/state changes, not the message text.

### UI action values

| `ui_action`           | UI behavior                                   |
| --------------------- | --------------------------------------------- |
| `STAY_ON_LOGIN`       | Keep user on login screen                     |
| `RETURN_TO_LOGIN`     | Clear temporary MFA state and return to login |
| `RETRY_SETUP_CONFIRM` | Stay on MFA setup confirmation/code screen    |
| `RETRY_MFA_VERIFY`    | Stay on MFA verification screen               |
| `RESTART_MFA_SETUP`   | Restart MFA setup flow                        |

---

# 1. `POST /auth/login/`

## Request

```json
{
  "email": "user@example.com",
  "password": "password"
}
```

## Possible success responses

### Login complete

```json
{
  "access": "<jwt access token>",
  "refresh": "<jwt refresh token>"
}
```

UI action: store tokens and enter app.

### MFA setup required

```json
{
  "mfa_setup_required": true,
  "mfa_token": "<temporary token>",
  "message": "MFA setup required before login."
}
```

UI action: show MFA setup screen. Store `mfa_token` temporarily.

### MFA verification required

```json
{
  "mfa_required": true,
  "mfa_token": "<temporary token>",
  "message": "MFA verification required."
}
```

UI action: show MFA verification screen. Store `mfa_token` temporarily.

## Possible errors

```json
{
  "response_type": "error",
  "error_code": "INVALID_CREDENTIALS",
  "ui_action": "STAY_ON_LOGIN",
  "message": "Invalid credentials"
}
```

```json
{
  "response_type": "error",
  "error_code": "USER_DISABLED",
  "ui_action": "STAY_ON_LOGIN",
  "message": "User account is disabled"
}
```

---

# 2. `POST /auth/mfa/setup/`

## Purpose

Creates or resets the user’s TOTP credential and returns an `otpauth_url`.

The UI should use `otpauth_url` to generate a QR code.

## Request

```json
{
  "mfa_token": "<temporary token from /auth/login/>"
}
```

## Success response

```json
{
  "otpauth_url": "otpauth://totp/..."
}
```

UI action: show QR code and 6-digit code entry form.

## Possible errors

```json
{
  "response_type": "error",
  "error_code": "MFA_DISABLED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is not enabled."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_TOKEN_EXPIRED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA token has expired. Please log in again."
}
```

```json
{
  "response_type": "error",
  "error_code": "INVALID_MFA_TOKEN",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "Invalid MFA token."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_ALREADY_CONFIGURED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is already configured for this user."
}
```

---

# 3. `POST /auth/mfa/setup/confirm/`

## Purpose

Confirms the user successfully scanned/imported the TOTP credential.

This does **not** return JWT tokens.

After success, the UI should send the user through `/auth/login/` again.

## Request

```json
{
  "mfa_token": "<temporary token from /auth/login/>",
  "code": "123456"
}
```

## Success response

```json
{
  "message": "MFA setup completed successfully."
}
```

UI action: call `/auth/login/` again.

## Possible errors

```json
{
  "response_type": "error",
  "error_code": "MFA_DISABLED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is not enabled."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_TOKEN_EXPIRED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA token has expired. Please log in again."
}
```

```json
{
  "response_type": "error",
  "error_code": "INVALID_MFA_TOKEN",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "Invalid MFA token."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_SETUP_NOT_STARTED",
  "ui_action": "RESTART_MFA_SETUP",
  "message": "MFA setup has not been started for this user."
}
```

```json
{
  "response_type": "error",
  "error_code": "INVALID_MFA_SETUP_CODE",
  "ui_action": "RETRY_SETUP_CONFIRM",
  "message": "Invalid authentication code."
}
```

---

# 4. `POST /auth/mfa/verify/`

## Purpose

Completes login after MFA is already configured.

## Request

```json
{
  "mfa_token": "<temporary token from /auth/login/>",
  "code": "123456"
}
```

## Success response

```json
{
  "access": "<jwt access token>",
  "refresh": "<jwt refresh token>"
}
```

UI action: store tokens and enter app.

## Possible errors

```json
{
  "response_type": "error",
  "error_code": "MFA_DISABLED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is not enabled."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_TOKEN_EXPIRED",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA token has expired. Please log in again."
}
```

```json
{
  "response_type": "error",
  "error_code": "INVALID_MFA_TOKEN",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "Invalid MFA token."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_NOT_ENABLED_FOR_USER",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is not enabled for this user."
}
```

```json
{
  "response_type": "error",
  "error_code": "MFA_DEVICE_MISSING",
  "ui_action": "RETURN_TO_LOGIN",
  "message": "MFA is enabled for this user but no confirmed MFA credential exists."
}
```

```json
{
  "response_type": "error",
  "error_code": "INVALID_MFA_CODE",
  "ui_action": "RETRY_MFA_VERIFY",
  "message": "Invalid or expired code"
}
```

---

# UI flow summary

## First-time MFA setup

1. Call `/auth/login/`
2. If response has `mfa_setup_required=true`, store `mfa_token`
3. Call `/auth/mfa/setup/`
4. Generate QR code from `otpauth_url`
5. User scans QR code and enters 6-digit code
6. Call `/auth/mfa/setup/confirm/`
7. On success, call `/auth/login/` again
8. If response has `mfa_required=true`, call `/auth/mfa/verify/`
9. Store JWT tokens

## Returning MFA user

1. Call `/auth/login/`
2. If response has `mfa_required=true`, store `mfa_token`
3. User enters 6-digit code
4. Call `/auth/mfa/verify/`
5. Store JWT tokens

## MFA disabled globally

1. Call `/auth/login/`
2. Server returns JWT tokens directly

```
```
