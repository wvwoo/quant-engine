# QTS — نظام تداول كمي (paper-trading) لخيارات 0DTE

نظام قائمة-تحقّق لخيارات أسهم أمريكية تنتهي نفس اليوم، مبني من
`institutional_quant_trading_report.md` (المواصفة التنفيذية) بعد تصنيف الـPRD
وثيقةَ رؤية (ADR-000). **تداول ورقي فقط — لا يوجد وسيط حقيقي في هذا الكود
بالتصميم**، و`LIVE_TRADING` بوابة مالك مقفلة تُرفض حتى مع الإقرار.

## تشغيل سريع

```bash
# 1) البيئة (Python 3.14)
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# 2) بوابة الجودة الكاملة: اختبارات + ruff + mypy + pip-audit + golden
./ci.sh

# 3) خطوة paper حيّة واحدة (بيانات yfinance مؤجّلة، السوق مفتوحًا)
.venv/bin/python -m qts_core.live --symbols SPY --db qts_v8/state/paper.db \
    --report qts_v8/state/session_report.md

# 4) اللوحة (كل رقم فيها من الخادم)
QTS_DB=qts_v8/state/paper.db .venv/bin/uvicorn qts_core.api:app --port 8787
# ثم افتح http://localhost:8787/
```

كرّر الخطوة 3 في حلقة (cron أو shell) لجلسة متصلة — المخزن يجعل أي عدد من
عمليات القتل/الاستئناف آمنًا: لا أمر مكرّر ولا مركز ضائع (مُختبَر).

## الخريطة

| المسار | الدور |
|---|---|
| `qts_core/money.py` | كل النقد سنتات صحيحة؛ tick schedules؛ التحجيم |
| `qts_core/clock.py` | `America/New_York` + تقويم XNYS؛ الـnaive datetime يرفع خطأ |
| `qts_core/pricing.py` | Black-Scholes محلي: parity → IV → delta (بلا تبعيات) |
| `qts_core/models.py` | `MarketView` — جدار look-ahead في الـconstructor |
| `qts_core/signals/` | 6 فلاتر نقية + قائمة تحقّق مُعلَّلة |
| `qts_core/risk.py` | سلّم الخروج IRONCLAD + قضبان SAFETY (force-flat, daily-loss) |
| `qts_core/store.py` | SQLite WAL + `fullfsync` + معرّفات أوامر حتمية uuid5 |
| `qts_core/broker.py` | `PaperBroker` — الوسيط الوحيد؛ fills موسومة MODELED |
| `qts_core/paper.py` | حلقة الجلسة (Agent 0) قابلة للاستئناف |
| `qts_core/backtest.py` | طبقتان: إشارات حقيقية + P&L خيارات MODELED |
| `qts_core/api.py` + `dashboard/` | FastAPI + لوحة RTL كل أرقامها من `/api/*` |
| `tests/golden/` | بوابة انحدار بايت-مطابقة |
| `archive/` | الـmockups الثلاثة الأصلية (بيانات ثابتة، للتاريخ) |

## قرارات مُلزِمة

المرجع الكامل: `~/.claude/brain/projects/proj-77372f/decisions.md` (ADR-000..009).
أهمها: الـgreeks تُحسب محليًّا ولا تأتي من المزوّد (ADR-002)؛ كل رقم backtest
يحمل وسم **MODELED** ما دام لا مصدر لأقساط تاريخية حقيقية (ADR-007)؛ الانزلاق
كميتان مستقلتان 3.5%/1% (ADR-005)؛ عالم النظام = التقرير المرجعي: $850 بالدولار
(ADR-006).

## حدود معروفة (بصراحة)

- بيانات yfinance مؤجّلة ~15 دقيقة وبلا greeks — كافية للورقي، والترقية
  (ThetaData $40/شهر) لا تمسّ سطر استراتيجية واحدًا.
- NVDA لا تُدرج 0DTE إلا الاثنين/الأربعاء/الجمعة؛ SPY/QQQ يوميًّا — النظام
  يفحص التوفّر كل يوم ولا يفترضه (B4).
- نتائج الـbacktest ليست وعد أرباح ولا نصيحة استثمارية؛ افتراضاتها مذكورة في
  كل تقرير.
