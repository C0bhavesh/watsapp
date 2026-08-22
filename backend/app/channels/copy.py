from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "hi", "hinglish", "gu")
DEFAULT_LANGUAGE: Final = "en"
# Meta rejects an empty template body parameter (named or positional) — every template-sending
# call site substitutes this when a real value is unavailable, rather than sending "".
EMPTY_PARAM_PLACEHOLDER: Final = "-"

_COPY: Final[dict[str, dict[str, str]]] = {
    # {order_name} is interpolated via str.format() at the call site (order_actions.py).
    "order_confirmed": {
        "en": "Your order {order_name} has been confirmed. Thank you for shopping with us!",
        "hi": "आपका ऑर्डर {order_name} कन्फर्म हो गया है। हमारे साथ शॉपिंग करने के लिए धन्यवाद!",
        "hinglish": (
            "Aapka order {order_name} confirm ho gaya hai. "
            "Humare saath shopping karne ke liye dhanyawad!"
        ),
        "gu": "તમારો ઓર્ડર {order_name} કન્ફર્મ થઈ ગયો છે. અમારી સાથે શોપિંગ કરવા બદલ આભાર!",
    },
    "cancel_confirm_prompt": {
        "en": "Are you sure you want to cancel this order? This cannot be undone.",
        "hi": "क्या आप वाकई यह ऑर्डर कैंसल करना चाहते हैं? इसे बाद में वापस नहीं किया जा सकता।",
        "hinglish": (
            "Kya aap sach mein yeh order cancel karna chahte hain? "
            "Yeh baad mein wapas nahi hoga."
        ),
        "gu": "શું તમે ખરેખર આ ઓર્ડર કેન્સલ કરવા માંગો છો? આ પછીથી પાછું નહીં થાય.",
    },
    "order_cancelled": {
        "en": "Your order has been cancelled as requested.",
        "hi": "आपके अनुरोध पर आपका ऑर्डर कैंसल कर दिया गया है।",
        "hinglish": "Aapke request par order cancel kar diya gaya hai.",
        "gu": "તમારી વિનંતી મુજબ તમારો ઓર્ડર કેન્સલ કરવામાં આવ્યો છે.",
    },
    "order_not_found": {
        "en": (
            "We could not find an order linked to this number. "
            "Could you share your order number?"
        ),
        "hi": "इस नंबर से जुड़ा कोई ऑर्डर नहीं मिला। कृपया अपना ऑर्डर नंबर बताएं।",
        "hinglish": "Is number se koi order nahi mila. Please apna order number bataiye.",
        "gu": "આ નંબર સાથે જોડાયેલો કોઈ ઓર્ડર મળ્યો નથી. કૃપા કરીને તમારો ઓર્ડર નંબર જણાવો.",
    },
    "refusal_other_order": {
        "en": "This order is not linked to your number, so we cannot share its details.",
        "hi": "यह ऑर्डर आपके नंबर से जुड़ा नहीं है, इसलिए हम इसकी जानकारी साझा नहीं कर सकते।",
        "hinglish": (
            "Yeh order aapke number se linked nahi hai, isliye hum details share nahi kar sakte."
        ),
        "gu": "આ ઓર્ડર તમારા નંબર સાથે જોડાયેલો નથી, તેથી અમે તેની વિગતો શેર કરી શકતા નથી.",
    },
    "error_fallback": {
        "en": (
            "Something went wrong on our end. Please try again shortly, "
            "or we will connect you to our team."
        ),
        "hi": (
            "हमारी ओर से कुछ गड़बड़ी हुई है। कृपया थोड़ी देर बाद पुनः प्रयास करें, "
            "या हम आपको हमारी टीम से जोड़ देंगे।"
        ),
        "hinglish": (
            "Kuch gadbad ho gayi hai hamari taraf se. Thodi der baad try karein, "
            "ya hum aapko team se connect kar denge."
        ),
        "gu": (
            "અમારી તરફથી કંઈક ગડબડ થઈ છે. કૃપા કરીને થોડી વાર પછી ફરી પ્રયાસ કરો, "
            "અથવા અમે તમને અમારી ટીમ સાથે જોડીશું."
        ),
    },
    # --- Phase 5: deterministic confirm/cancel button-tap replies ---
    "confirm_success": {
        "en": "Thank you, your order is confirmed. We will ship it soon.",
        "hi": "धन्यवाद, आपका ऑर्डर कन्फर्म हो गया है। हम इसे जल्द भेजेंगे।",
        "hinglish": "Thank you, aapka order confirm ho gaya hai. Hum ise jaldi ship karenge.",
        "gu": "આભાર, તમારો ઓર્ડર કન્ફર્મ થઈ ગયો છે. અમે તેને જલ્દી મોકલીશું.",
    },
    "already_confirmed": {
        "en": "This order is already confirmed. There is nothing more to do.",
        "hi": "यह ऑर्डर पहले ही कन्फर्म हो चुका है। अब कुछ और करने की ज़रूरत नहीं है।",
        "hinglish": "Yeh order pehle se confirm hai. Kuch aur karne ki zaroorat nahi hai.",
        "gu": "આ ઓર્ડર પહેલેથી જ કન્ફર્મ છે. હવે બીજું કંઈ કરવાની જરૂર નથી.",
    },
    "cancel_are_you_sure": {
        "en": "Are you sure you want to cancel this order? This cannot be undone.",
        "hi": "क्या आप वाकई यह ऑर्डर कैंसल करना चाहते हैं? इसे बाद में वापस नहीं किया जा सकता।",
        "hinglish": (
            "Kya aap sach mein yeh order cancel karna chahte hain? "
            "Yeh baad mein wapas nahi hoga."
        ),
        "gu": "શું તમે ખરેખર આ ઓર્ડર કેન્સલ કરવા માંગો છો? આ પછીથી પાછું નહીં થાય.",
    },
    "cancel_yes_title": {
        "en": "Yes, cancel",
        "hi": "हां, कैंसल करें",
        "hinglish": "Haan, cancel",
        "gu": "હા, કેન્સલ કરો",
    },
    "cancel_no_title": {
        "en": "No, keep it",
        "hi": "नहीं, रखें",
        "hinglish": "Nahi, rakhein",
        "gu": "ના, રહેવા દો",
    },
    # {order_name} is interpolated via str.format() at the call site (order_actions.py).
    "cancel_requested": {
        "en": "Your order {order_name} has been cancelled.",
        "hi": "आपका ऑर्डर {order_name} कैंसल कर दिया गया है।",
        "hinglish": "Aapka order {order_name} cancel kar diya gaya hai.",
        "gu": "તમારો ઓર્ડર {order_name} કેન્સલ કરવામાં આવ્યો છે.",
    },
    "cancel_kept": {
        "en": "No problem, we have kept your order as it is.",
        "hi": "कोई बात नहीं, हमने आपका ऑर्डर वैसे ही रखा है।",
        "hinglish": "Koi baat nahi, humne aapka order waise hi rakha hai.",
        "gu": "કોઈ વાંધો નહીં, અમે તમારો ઓર્ડર એમ જ રાખ્યો છે.",
    },
    "cancel_too_late": {
        "en": (
            "This order has already been dispatched, so it cannot be cancelled here. "
            "Please contact our support team for help."
        ),
        "hi": (
            "यह ऑर्डर पहले ही भेजा जा चुका है, इसलिए इसे यहां कैंसल नहीं किया जा सकता। "
            "कृपया सहायता के लिए हमारी टीम से संपर्क करें।"
        ),
        "hinglish": (
            "Yeh order pehle se dispatch ho chuka hai, isliye ise yahan cancel nahi kar sakte. "
            "Please help ke liye hamari team se baat karein."
        ),
        "gu": (
            "આ ઓર્ડર પહેલેથી જ મોકલાઈ ગયો છે, તેથી તેને અહીં કેન્સલ કરી શકાતો નથી. "
            "કૃપા કરીને મદદ માટે અમારી ટીમનો સંપર્ક કરો."
        ),
    },
    "already_cancelled": {
        "en": "This order is already cancelled. There is nothing more to do.",
        "hi": "यह ऑर्डर पहले ही कैंसल हो चुका है। अब कुछ और करने की ज़रूरत नहीं है।",
        "hinglish": "Yeh order pehle se cancel ho chuka hai. Kuch aur karne ki zaroorat nahi hai.",
        "gu": "આ ઓર્ડર પહેલેથી જ કેન્સલ થઈ ગયો છે. હવે બીજું કંઈ કરવાની જરૂર નથી.",
    },
    "cancel_not_available_prepaid": {
        "en": (
            "This order was prepaid, so it can't be cancelled once placed. "
            "Please contact our support team for help."
        ),
        "hi": (
            "यह ऑर्डर प्रीपेड था, इसलिए इसे ऑर्डर देने के बाद कैंसल नहीं किया जा सकता। "
            "कृपया सहायता के लिए हमारी टीम से संपर्क करें।"
        ),
        "hinglish": (
            "Yeh order prepaid tha, isliye order place hone ke baad ise cancel nahi kar sakte. "
            "Please help ke liye hamari team se baat karein."
        ),
        "gu": (
            "આ ઓર્ડર પ્રીપેડ હતો, તેથી ઓર્ડર આપ્યા પછી તેને કેન્સલ કરી શકાતો નથી. "
            "કૃપા કરીને મદદ માટે અમારી ટીમનો સંપર્ક કરો."
        ),
    },
    "cancel_failed": {
        "en": (
            "We could not cancel your order just now. "
            "Our team will look into it and get back to you."
        ),
        "hi": (
            "हम अभी आपका ऑर्डर कैंसल नहीं कर सके। "
            "हमारी टीम इसे देखकर आपसे संपर्क करेगी।"
        ),
        "hinglish": (
            "Hum abhi aapka order cancel nahi kar paaye. "
            "Hamari team ise dekhkar aapse baat karegi."
        ),
        "gu": (
            "અમે અત્યારે તમારો ઓર્ડર કેન્સલ કરી શક્યા નથી. "
            "અમારી ટીમ તેને જોઈને તમારો સંપર્ક કરશે."
        ),
    },
    "not_found": {
        "en": (
            "We could not access that order for this number. If you need help, "
            "please share your order number and we will check."
        ),
        "hi": (
            "हम इस नंबर के लिए वह ऑर्डर नहीं देख सके। यदि आपको मदद चाहिए, "
            "तो कृपया अपना ऑर्डर नंबर बताएं, हम जांच करेंगे।"
        ),
        "hinglish": (
            "Hum is number ke liye woh order access nahi kar paaye. Agar madad chahiye, "
            "to apna order number bataiye, hum check karenge."
        ),
        "gu": (
            "અમે આ નંબર માટે તે ઓર્ડર જોઈ શક્યા નથી. જો તમને મદદ જોઈએ, "
            "તો કૃપા કરીને તમારો ઓર્ડર નંબર જણાવો, અમે તપાસ કરીશું."
        ),
    },
}


def copy_for(key: str, language: str) -> str:
    """Return the fixed reply string for a key/language, falling back to English.

    Raises KeyError on an unknown key -- call sites are internal, never user input.
    """
    entry = _COPY[key]
    return entry.get(language, entry[DEFAULT_LANGUAGE])
