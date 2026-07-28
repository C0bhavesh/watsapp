from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "hi", "hinglish", "gu")
DEFAULT_LANGUAGE: Final = "en"

_COPY: Final[dict[str, dict[str, str]]] = {
    "order_confirmed": {
        "en": "Thank you, your order has been confirmed. We will ship it soon.",
        "hi": "धन्यवाद, आपका ऑर्डर कन्फर्म हो गया है। हम इसे जल्द भेजेंगे।",
        "hinglish": "Thank you, aapka order confirm ho gaya hai. Hum ise jaldi ship karenge.",
        "gu": "આભાર, તમારો ઓર્ડર કન્ફર્મ થઈ ગયો છે. અમે તેને જલ્દી મોકલીશું.",
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
}


def copy_for(key: str, language: str) -> str:
    """Return the fixed reply string for a key/language, falling back to English.

    Raises KeyError on an unknown key -- call sites are internal, never user input.
    """
    entry = _COPY[key]
    return entry.get(language, entry[DEFAULT_LANGUAGE])
