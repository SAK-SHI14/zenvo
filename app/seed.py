import json
import uuid

from dotenv import load_dotenv

from app import db

load_dotenv()


# Insert 3 sample past leads, sessions, messages, and call summaries so the dashboard starts populated.
def seed_database() -> None:
    db.init_db()
    conn = db.get_connection()

    sample_leads = [
        {
            "phone": "+919876543210",
            "name": "Dr. Ananya Sharma",
            "email": "ananya@sharmaclinic.in",
            "budget": "Rs 2,499/month",
            "need": "WhatsApp and voice reminders for 2 dental clinics",
            "timeline": "This week",
            "score": 88,
            "sentiment": "positive",
            "status": "booked",
            "summary": {
                "summary": "Dr. Ananya Sharma runs 2 dental clinics and wants Pro plan WhatsApp and voice reminders.",
                "objections": ["Wanted to confirm patient data stays in India"],
                "buying_signals": ["Asked for Pro plan pricing", "Booked an onboarding demo slot"],
                "next_best_action": "Send calendar invite and Mumbai data residency PDF before the demo.",
                "overall_sentiment": "positive",
                "final_score": 88,
                "sentiment_timeline": ["neutral", "positive", "positive"],
            },
            "messages": [
                ("user", "Hi, we have two dental clinics and high no-shows. What does Pro cost?", "en", "neutral", 55),
                ("agent", "Zenvo Pro is Rs 2,499 per month for up to 2,500 reminders and 3 clinics. Would you like a quick demo?", "en", "positive", 70),
                ("user", "Yes, also does patient data stay in India? Book me for tomorrow morning.", "en", "positive", 88),
            ],
            "book_first_slot": True,
        },
        {
            "phone": "+919811223344",
            "name": "Rajesh Verma",
            "email": "rajesh@vermaortho.in",
            "budget": "Rs 999/month",
            "need": "Basic SMS reminders for single orthopedic clinic",
            "timeline": "Within 2 weeks",
            "score": 72,
            "sentiment": "positive",
            "status": "hot",
            "summary": {
                "summary": "Rajesh Verma inquired about the Rs 999 Basic plan for his orthopedic clinic in Delhi.",
                "objections": ["Concerned about setup effort for reception staff"],
                "buying_signals": ["Confirmed Rs 999 fits budget", "Asked about 14-day free trial"],
                "next_best_action": "Share the 14-day trial link and 30-minute onboarding checklist.",
                "overall_sentiment": "positive",
                "final_score": 72,
                "sentiment_timeline": ["neutral", "positive"],
            },
            "messages": [
                ("user", "Humein ek clinic ke liye simple reminder tool chahiye. Basic plan kitne ka hai?", "hi", "neutral", 45),
                ("agent", "Zenvo Basic plan Rs 999 per month hai jisme 500 SMS ya WhatsApp reminders milte hain. Kya main aapko 14-day free trial bhej doon?", "hi", "positive", 72),
            ],
            "book_first_slot": False,
        },
        {
            "phone": "+919822334455",
            "name": "Dr. Harpreet Gill",
            "email": None,
            "budget": "Evaluating",
            "need": "Hinglish voice and WhatsApp reminders for eye clinic",
            "timeline": "Next month",
            "score": 48,
            "sentiment": "neutral",
            "status": "warm",
            "summary": {
                "summary": "Dr. Harpreet Gill wants Hinglish voice reminders and plans to decide next month.",
                "objections": ["Needs partner approval before subscribing"],
                "buying_signals": ["Specifically asked about Hinglish language support"],
                "next_best_action": "Follow up next week with a sample Hinglish voice reminder recording.",
                "overall_sentiment": "neutral",
                "final_score": 48,
                "sentiment_timeline": ["neutral", "neutral"],
            },
            "messages": [
                ("user", "Kya Zenvo Hinglish mein voice reminders bhej sakta hai?", "hinglish", "neutral", 48),
                ("agent", "Haan ji, Zenvo English, Hindi aur Hinglish mein reminders support karta hai. Kya main aapke liye demo schedule karoon?", "hinglish", "neutral", 48),
            ],
            "book_first_slot": False,
        },
    ]

    inserted = 0
    try:
        for item in sample_leads:
            existing = conn.execute(
                "SELECT id FROM leads WHERE phone = ?", (item["phone"],)
            ).fetchone()
            if existing is not None:
                continue

            ts = db.now_iso()
            cur = conn.execute(
                """
                INSERT INTO leads (
                    phone, name, email, budget, need, timeline,
                    score, sentiment, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["phone"],
                    item["name"],
                    item["email"],
                    item["budget"],
                    item["need"],
                    item["timeline"],
                    item["score"],
                    item["sentiment"],
                    item["status"],
                    ts,
                    ts,
                ),
            )
            lead_id = cur.lastrowid
            session_id = str(uuid.uuid4())

            conn.execute(
                """
                INSERT INTO sessions (session_id, lead_id, language, status, started_at, ended_at)
                VALUES (?, ?, ?, 'ended', ?, ?)
                """,
                (session_id, lead_id, item["messages"][0][2], ts, ts),
            )

            for role, text, lang, sentiment, score in item["messages"]:
                conn.execute(
                    """
                    INSERT INTO messages (session_id, lead_id, role, text, language, sentiment, score, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, lead_id, role, text, lang, sentiment, score, ts),
                )

            summary = item["summary"]
            conn.execute(
                """
                INSERT INTO calls (
                    session_id, lead_id, summary, objections, buying_signals,
                    next_action, overall_sentiment, final_score, sentiment_timeline, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    lead_id,
                    summary["summary"],
                    json.dumps(summary["objections"]),
                    json.dumps(summary["buying_signals"]),
                    summary["next_best_action"],
                    summary["overall_sentiment"],
                    summary["final_score"],
                    json.dumps(summary["sentiment_timeline"]),
                    ts,
                ),
            )

            if item["book_first_slot"]:
                free_slots = db.get_available_slots(days=3, conn=conn)
                if free_slots:
                    conn.execute(
                        "INSERT INTO appointments (lead_id, slot_datetime, status, created_at) VALUES (?, ?, 'confirmed', ?)",
                        (lead_id, free_slots[0], ts),
                    )
                else:
                    print(f"Warning: no free slots available; skipped sample appointment for {item['name']}.")

            inserted += 1

        conn.commit()
        print(f"Seed complete: inserted {inserted} sample lead(s) and call(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    seed_database()

