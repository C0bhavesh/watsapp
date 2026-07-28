# WhatsApp Message Templates — Drafts for Approval (2026-07-28)

> Category **UTILITY** (transactional wording only — no promo words, keeps cost ~₹0.12/msg
> and near-instant auto-approval). One template name, one version per language (en/hi/gu).
> Variables: {{1}} = customer first name, {{2}} = order number (tavasNNNN), {{3}} = amount (₹).
> Buttons are QUICK_REPLY (tap comes back to our webhook as a button reply + context id).
> We can create these programmatically via `POST /{waba_id}/message_templates` once we have
> WABA access — owner approves wording first.

## T1 — `order_confirmation_cod` (the main template, COD orders)

**English (en):**
> Hi {{1}}, we have received your order {{2}} of ₹{{3}} (Cash on Delivery) at Thetavas.
> Please confirm your order so we can ship it soon.

Buttons: `Confirm Order` · `Cancel Order`

**Hindi (hi):**
> नमस्ते {{1}}, Thetavas पर आपका ऑर्डर {{2}} (₹{{3}}, कैश ऑन डिलीवरी) प्राप्त हुआ है।
> कृपया अपना ऑर्डर कन्फर्म करें ताकि हम उसे जल्द भेज सकें।

Buttons: `ऑर्डर कन्फर्म करें` · `ऑर्डर कैंसल करें`

**Gujarati (gu):**
> નમસ્તે {{1}}, Thetavas પર તમારો ઓર્ડર {{2}} (₹{{3}}, કેશ ઓન ડિલિવરી) મળી ગયો છે.
> કૃપા કરીને તમારો ઓર્ડર કન્ફર્મ કરો જેથી અમે તેને જલ્દી મોકલી શકીએ.

Buttons: `ઓર્ડર કન્ફર્મ કરો` · `ઓર્ડર કેન્સલ કરો`

## T2 — `order_received_prepaid` (optional — only if client picks Q1 option C)

**English:**
> Hi {{1}}, your order {{2}} of ₹{{3}} has been received at Thetavas and is being
> prepared for shipping. We will keep you updated.

No buttons. (hi/gu versions on approval of T1 wording.)

## API-creation JSON (T1 English; other languages identical structure)

```json
{
  "name": "order_confirmation_cod",
  "language": "en",
  "category": "UTILITY",
  "components": [
    {
      "type": "BODY",
      "text": "Hi {{1}}, we have received your order {{2}} of ₹{{3}} (Cash on Delivery) at Thetavas. Please confirm your order so we can ship it soon.",
      "example": { "body_text": [["Suman", "tavas3733", "949"]] }
    },
    {
      "type": "BUTTONS",
      "buttons": [
        { "type": "QUICK_REPLY", "text": "Confirm Order" },
        { "type": "QUICK_REPLY", "text": "Cancel Order" }
      ]
    }
  ]
}
```

## Design notes (locked into the build)

1. **Language selection at send time:** pick template language from the order's
   `customerLocale` if available, else default English; the *conversation* after the tap is
   handled by Gemini in whatever language the customer writes.
2. **Tap → order mapping:** a button reply arrives with `context.id` = the wamid of our
   template message. We therefore store `sent_wamid → order_gid` in `order_mappings` when
   sending — taps route deterministically to the exact order even if the customer has
   multiple orders. (No parsing of button text needed — language-independent.)
3. **Button text ≤ 25 chars** (Meta cap) — all drafts comply.
4. **UTILITY wording rules:** reference the specific transaction only; no discounts, no
   "check out our collection" — that would reclassify to MARKETING (~7× cost) or get rejected.
5. Cancel taps do NOT cancel directly — the bot asks the confirm question first (pending
   client Q4 recommendation A).
