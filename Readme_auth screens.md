# MFA UI Screens & QR Code Implementation

---

# 1. MFA Setup Screen

## Purpose

User scans QR code and enters first code to confirm setup.

## Layout (rough)

```
-----------------------------------------
 Multi-Factor Authentication Setup
-----------------------------------------

Scan this QR code with your authenticator app:

[ QR CODE IMAGE HERE ]

Or enter this code manually:
ABCDEF123456...

-----------------------------------------

Enter the 6-digit code from your app:

[  _ _ _ _ _ _  ]

[ Confirm Setup ]

-----------------------------------------
Error message (if any)
-----------------------------------------
```

## Required data

* `otpauth_url` (from `/auth/mfa/setup/`)
* `mfa_token` (stored from login)

## Actions

* Generate QR from `otpauth_url`
* On submit → call `/auth/mfa/setup/confirm/`

---

# 2. MFA Setup Confirm Behavior

No separate screen needed.

## Error handling

* `RETRY_SETUP_CONFIRM` → show error, stay on screen
* `RESTART_MFA_SETUP` → restart setup flow (call `/auth/mfa/setup/`)
* `RETURN_TO_LOGIN` → go back to login

---

# 3. MFA Verify Screen (Returning User)

## Layout

```
-----------------------------------------
 Multi-Factor Authentication
-----------------------------------------

Enter the 6-digit code from your authenticator app:

[  _ _ _ _ _ _  ]

[ Verify ]

-----------------------------------------
Error message (if any)
-----------------------------------------
```

## Required data

* `mfa_token` (from `/auth/login/`)

## Actions

* On submit → call `/auth/mfa/verify/`

## Error handling

* `RETRY_MFA_VERIFY` → show error, stay on screen
* `RETURN_TO_LOGIN` → go back to login

---

# 4. Login Screen

```
-----------------------------------------
 Login
-----------------------------------------

Email:
[               ]

Password:
[               ]

[ Login ]

-----------------------------------------
Error message
-----------------------------------------
```

---

# QR Code Generation

Input:

```
otpauth://totp/ngenCerf:email?... 
```

Convert this into a QR image.

---

## (Recommended): Frontend QR Library

### Example (JavaScript)

```javascript
import QRCode from "qrcode";

const canvas = document.getElementById("qr");

QRCode.toCanvas(canvas, otpauthUrl);
```

---

### Example (Vue)

```vue
<qrcode-vue :value="otpauthUrl" :size="200" />
```

---

# Summary

UI only needs 3 screens:

1. Login
2. MFA Setup (QR + code entry)
3. MFA Verify (code entry)

All navigation driven by:

* `mfa_setup_required`
* `mfa_required`
* `ui_action`
