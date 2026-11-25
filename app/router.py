from app.handlers import handle_booking

# Router = intent dispatch; new tools are added by adding one dict entry.
INTENT_HANDLERS = {
    "book_appointment": handle_booking,
}


# Dispatch the intent to its handler if registered, otherwise pass the result through unchanged.
def dispatch_intent(intent: str, context: dict) -> dict:
    handler = INTENT_HANDLERS.get(intent)
    if handler is None:
        return context["result"]
    return handler(context)
