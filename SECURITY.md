# Security Policy · سياسة الأمان

## Reporting a vulnerability · الإبلاغ عن ثغرة

Please **do not** open a public issue for security problems.
Use GitHub's private reporting ("Security" tab → "Report a vulnerability"),
or email **hello@execraai.com**.

الرجاء **عدم** فتح Issue عام للمشاكل الأمنية. استخدم الإبلاغ الخاص في GitHub
(تبويب Security ← Report a vulnerability) أو راسل hello@execraai.com.

We acknowledge reports within 3 business days and aim to ship a fix within 30 days.

## Secrets · الأسرار

This repository must never contain credentials. Configuration is read from
environment variables; see `.env.example` where present. Every push is scanned
by gitleaks (CI) and GitHub secret scanning with push protection.

## Supported versions

Only the default branch receives security fixes.
