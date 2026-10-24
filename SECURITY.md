# Security Policy

## Credentials

Never publish or commit bot tokens, API hashes, `.env` files, Telethon session files, or exported chat data. Use local environment variables or the secret manager provided by your execution environment.

If a bot token is exposed, revoke it through @BotFather and create a replacement. If a session file is exposed, treat the associated session as compromised and revoke it from Telegram's active sessions.

## Reporting a vulnerability

Do not include credentials, private chat exports, or full migration logs in a public issue. Redact message IDs and personal data before reporting bugs. This project is a personal migration utility and has not been audited as a security product.
