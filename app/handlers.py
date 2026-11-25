from datetime import datetime

from app import db


# Format 'YYYY-MM-DD HH:MM' into a human-friendly weekday and 12-hour time like 'Tuesday 4:00 PM'.
def format_slot_human(slot_datetime: str) -> str:
    dt = datetime.strptime(slot_datetime, "%Y-%m-%d %H:%M")
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%A')} {hour}:{dt.strftime('%M %p')}"


# Build the fixed booking confirmation message in the session language from the confirmed slot.
def format_booked_reply(language: str, slot_datetime: str) -> str:
    slot_human = format_slot_human(slot_datetime)
    if language == "hi":
        return f"हो गया! आपका डेमो {slot_human} के लिए बुक हो गया है। आपको जल्दी कन्फर्मेशन मिलेगा।"
    if language == "hinglish":
        return f"Ho gaya! Aapka demo {slot_human} ke liye book ho gaya hai. Aapko jaldi confirmation milega."
    return f"Done! Your demo is booked for {slot_human}. You will get a confirmation shortly."


# Build a confirmation-asking reply when a slot is proposed for the first time without prior confirmation.
def format_slot_proposal_reply(language: str, slot_datetime: str) -> str:
    slot_human = format_slot_human(slot_datetime)
    if language == "hi":
        return f"{slot_human} का स्लॉट खाली है। क्या मैं यह स्लॉट आपके लिए बुक कर दूँ?"
    if language == "hinglish":
        return f"{slot_human} ka slot available hai. Kya main yeh slot aapke liye book kar doon?"
    return f"I have {slot_human} available. Would you like me to confirm this slot for you?"


# Build a polite reply when a caller declines a previously proposed demo slot.
def format_booking_rejected_reply(language: str) -> str:
    if language == "hi":
        return "कोई बात नहीं, मैंने यह स्लॉट बुक नहीं किया है। आप जब चाहें कोई दूसरा समय बता सकते हैं।"
    if language == "hinglish":
        return "Koi baat nahi, maine yeh slot book nahi kiya hai. Aap koi doosra time batayeinge toh main check kar loongi."
    return "No problem, I have not booked that slot. Let me know if you would like a different time."


# Format a polite slot-unavailable reply offering the next 2 open slots in the call language.
def format_alternative_slots_reply(language: str, next_slots: list[str]) -> str:
    if len(next_slots) >= 2:
        slot_text = f"{next_slots[0]} or {next_slots[1]}"
    elif len(next_slots) == 1:
        slot_text = next_slots[0]
    else:
        slot_text = None

    if not slot_text:
        return "That slot is not available right now, and we have no open slots left online. I will have a human specialist call you to schedule."

    if language == "hi":
        return f"Maaf kijiye, yeh slot uplabdh nahi hai. Kya main aapke liye {slot_text} book kar doon?"
    if language == "hinglish":
        return f"Sorry, yeh slot already booked hai. Kya main aapke liye {slot_text} book kar doon?"
    return f"Sorry, that slot is already taken. Would you like me to book {slot_text} instead?"


# Insert the confirmed slot inside the caller's transaction, or offer 2 alternatives on IntegrityError.
def handle_booking(context: dict) -> dict:
    result = context["result"]
    lead_id = context["lead_id"]
    conn = context["conn"]
    session_language = context.get("session_language", result.get("language", "en"))
    confirm_lang = session_language if session_language in ("en", "hi", "hinglish") else "en"
    requested_slot = result.get("slot_datetime")

    booked = db.book_slot(lead_id, requested_slot, conn=conn)
    if booked:
        result["booked"] = True
        result["pending_slot"] = None
        result["language"] = confirm_lang
        result["reply"] = format_booked_reply(confirm_lang, requested_slot)
        return result

    free_slots = db.get_available_slots(days=3, conn=conn)[:2]
    alt_lang = result.get("language") if result.get("language") in ("en", "hi", "hinglish") else "en"
    result["booked"] = False
    result["slot_datetime"] = None
    result["pending_slot"] = None
    result["language"] = alt_lang
    result["reply"] = format_alternative_slots_reply(alt_lang, free_slots)
    return result
