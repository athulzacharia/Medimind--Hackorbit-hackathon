import google.generativeai as genai
import os
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, g, flash # Import flash for potential use
import sqlite3
from datetime import datetime, time, timedelta
from pytz import timezone
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import json
import traceback

# Set up basic logging for APScheduler
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# Configure the API key for Google Generative AI (THIS IS DONE ONCE, GLOBALLY)
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    logging.error("ERROR: GOOGLE_API_KEY environment variable not set. Chatbot will not function correctly.")
else:
    genai.configure(api_key=api_key)
    logging.info("Gemini API configured successfully.")

# --- TEMPORARY: Check available models ---
print("\n--- Available Models ---")
for m in genai.list_models():
    if "generateContent" in m.supported_generation_methods:
        print(f"- {m.name}")
print("------------------------\n")
# --- END TEMPORARY ---

# Safety settings for the model
safety_settings = [
    {
        "category": "HARM_CATEGORY_HARASSMENT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE"
    },
    {
        "category": "HARM_CATEGORY_HATE_SPEECH",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE"
    },
    {
        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE"
    },
    {
        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
        "threshold": "BLOCK_MEDIUM_AND_ABOVE"
    },
]

# Configure the Gemini model (globally)
model = genai.GenerativeModel('gemini-1.5-flash', safety_settings=safety_settings)

# The 'chat' object is useful for general conversation history.
# However, for the specific structured responses you want for symptoms,
# we will use `model.generate_content(prompt)` directly, as the prompt
# itself provides all the necessary context and format instructions for each turn.
chat = model.start_chat(history=[])


app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

# --- SQLite Database Setup ---
DATABASE = 'reminders.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db_path = os.path.join(app.root_path, 'reminders.db')
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        g._database = db
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medication_name TEXT NOT NULL,
                dosage TEXT,
                time_str TEXT NOT NULL,
                frequency TEXT NOT NULL,
                last_triggered TEXT
            )
        ''')
        db.commit()
        logging.info("Database initialized or already exists.")

# --- APScheduler Setup ---
scheduler = BackgroundScheduler(timezone='Asia/Kolkata')
notification_queue = []

def send_in_app_notification(medication_name, dosage, reminder_id):
    with app.app_context():
        db = get_db()
        cursor = db.cursor()

        reminder_info = cursor.execute("SELECT frequency, last_triggered FROM reminders WHERE id = ?", (reminder_id,)).fetchone()

        if not reminder_info:
            logging.warning(f"Reminder ID {reminder_id} not found for notification. Skipping.")
            return

        frequency = reminder_info['frequency']
        last_triggered_str = reminder_info['last_triggered']

        current_time_ist = datetime.now(timezone('Asia/Kolkata'))
        today_iso_date = current_time_ist.isoformat().split('T')[0]

        # Check if already triggered today for recurring reminders
        if frequency not in ['once', 'specific_date']: # 'once' and 'specific_date' don't care about daily checks
            if last_triggered_str and last_triggered_str.split('T')[0] == today_iso_date:
                logging.info(f"Reminder ID {reminder_id} ({medication_name}) already triggered today. Skipping duplicate notification.")
                return

        message = f"Time for your {medication_name} ({dosage})!" if dosage else f"Time for your {medication_name}!"
        logging.info(f"NOTIFICATION TRIGGERED: {message} for reminder ID {reminder_id}")
        notification_queue.append(message)

        cursor.execute("UPDATE reminders SET last_triggered = ? WHERE id = ?", (current_time_ist.isoformat(), reminder_id))
        db.commit()
        logging.info(f"Updated last_triggered for reminder ID {reminder_id}")

        if frequency == 'once':
            try:
                # Remove the job from the scheduler immediately after triggering for 'once' reminders
                job_id = f'reminder_{reminder_id}'
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                    logging.info(f"Removed 'once' job '{job_id}' from scheduler after single trigger.")
                
                # Delete from DB only after successful notification and job removal
                cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                db.commit()
                logging.info(f"Removed 'once' reminder {reminder_id} from DB after single trigger.")
            except Exception as e:
                logging.error(f"Error cleaning up 'once' reminder {reminder_id}: {e}")

def schedule_reminder_job(reminder):
    job_id = f'reminder_{reminder["id"]}'
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logging.info(f"Removed existing job '{job_id}' from scheduler before rescheduling.")

    med_name = reminder['medication_name']
    dosage = reminder['dosage'] if reminder.get('dosage') else ''
    time_str = reminder['time_str'] # Can be HH:MM or YYYY-MM-DD HH:MM
    frequency = reminder['frequency']

    try:
        if frequency == 'daily':
            hour, minute = map(int, time_str.split(':'))
            scheduler.add_job(
                send_in_app_notification,
                'cron',
                hour=hour,
                minute=minute,
                args=[med_name, dosage, reminder['id']],
                id=job_id
            )
            logging.info(f"Scheduled 'daily' job '{job_id}' for {hour:02d}:{minute:02d} (IST).")
        elif frequency == 'once':
            # For 'once', time_str should include date YYYY-MM-DD HH:MM
            run_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M').astimezone(timezone('Asia/Kolkata'))
            now_ist = datetime.now(timezone('Asia/Kolkata'))

            if run_time <= now_ist:
                logging.warning(f"Once reminder time ({time_str}) for {med_name} is in the past. Not scheduling.")
                return # Don't schedule if time is in the past

            scheduler.add_job(
                send_in_app_notification,
                'date',
                run_date=run_time,
                args=[med_name, dosage, reminder['id']],
                id=job_id,
                misfire_grace_period=60 # seconds
            )
            logging.info(f"Scheduled 'once' job '{job_id}' for {run_time.strftime('%Y-%m-%d %H:%M %Z%z')}.")
        elif frequency in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
                          'monday,wednesday,friday', 'tuesday,thursday,saturday',
                          'monday,tuesday,wednesday,thursday,friday,saturday,sunday']:
            time_parts = time_str.split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1])

            # Handle multiple days in frequency string
            aps_days = []
            if ',' in frequency:
                for day_name in frequency.split(','):
                    aps_days.append(day_name.strip().lower())
            else:
                aps_days.append(frequency.lower())

            scheduler.add_job(
                send_in_app_notification,
                'cron',
                hour=hour,
                minute=minute,
                day_of_week=','.join(aps_days), # APScheduler accepts comma-separated list
                args=[med_name, dosage, reminder['id']],
                id=job_id
            )
            logging.info(f"Scheduled 'cron' job '{job_id}' for {hour:02d}:{minute:02d} on {frequency} (IST).")
        else:
            logging.warning(f"Unknown or malformed frequency '{frequency}' for reminder ID {reminder['id']}. Not scheduled.")
    except Exception as e:
        logging.error(f"Error scheduling job for reminder ID {reminder['id']}: {e}")
        traceback.print_exc()


def load_and_schedule_existing_reminders(scheduler_instance, app_instance):
    with app_instance.app_context():
        db = get_db()
        cursor = db.cursor()
        reminders_data = cursor.execute("SELECT * FROM reminders").fetchall()
        for row in reminders_data:
            reminder = dict(row)
            schedule_reminder_job(reminder)
            logging.info(f"Rescheduled reminder ID {reminder['id']}: {reminder['medication_name']}")

# --- Flask Routes ---

@app.route('/')
def index():
    return render_template('landing_page.html')

@app.route('/chatbot')
def chatbot_page():
    return render_template('chatbot.html')

# !!! NEW: Endpoint to serve the HTML page for reminders !!!
@app.route('/reminders')
def reminders_page():
    # The actual reminder data will be fetched by JavaScript from /api/reminders
    logging.info("Rendering reminders.html page.")
    return render_template('reminders.html')

# !!! NEW: Endpoint for AJAX/fetch calls to get JSON reminders data !!!
@app.route('/api/reminders', methods=['GET'])
def get_reminders_json():
    try:
        db = get_db()
        cursor = db.cursor()
        reminders_data = cursor.execute("SELECT id, medication_name, dosage, time_str, frequency FROM reminders ORDER BY time_str").fetchall()
        reminders_list = [dict(row) for row in reminders_data]
        logging.info(f"Fetched {len(reminders_list)} reminders for API JSON response.")
        return jsonify(reminders_list) # Return JSON here!
    except Exception as e:
        logging.error(f"Error fetching reminders for JSON API: {e}")
        traceback.print_exc()
        return jsonify({"error": "Failed to retrieve reminders."}), 500

# Modified: POST endpoint for adding reminders (expects JSON input)
@app.route('/reminders', methods=['POST'])
def add_reminder():
    data = request.json
    medication_name = data.get('medication_name')
    dosage = data.get('dosage')
    time_str = data.get('time_str')
    frequency = data.get('frequency')

    if not all([medication_name, time_str, frequency]):
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO reminders (medication_name, dosage, time_str, frequency, last_triggered) VALUES (?, ?, ?, ?, NULL)",
            (medication_name, dosage, time_str, frequency)
        )
        db.commit()
        new_reminder_id = cursor.lastrowid
        new_reminder = {
            'id': new_reminder_id,
            'medication_name': medication_name,
            'dosage': dosage,
            'time_str': time_str,
            'frequency': frequency
        }
        schedule_reminder_job(new_reminder)
        logging.info(f"Reminder added and scheduled: {new_reminder}")
        return jsonify({'message': 'Reminder added successfully!', 'id': new_reminder_id}), 201
    except Exception as e:
        logging.error(f"Error adding reminder: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/chat', methods=['POST'])
def chat_endpoint():
    data = request.json
    user_message = data.get('message')

    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    try:
        # --- Enhanced Prompt Engineering for structured medical-like responses ---
        prompt = f"""
        You are a highly informative and structured AI assistant specializing in providing general health information. You are NOT a medical professional and cannot provide diagnoses, treatments, or medical advice. Your primary goal is to educate users on potential causes, self-care measures, and crucial indicators for seeking professional medical attention for common symptoms.

        When a user describes a symptom, respond in a clear, structured, and helpful markdown format, including the following sections:

        1.  **Symptom Name (e.g., "Feeling Dizzy:")** - Start with the symptom the user mentioned.
        2.  **Overview/Introduction:** A brief, clear explanation of what the symptom can stem from, emphasizing the importance of hydration, rest, and avoiding sudden movements. Always include a strong disclaimer about consulting a doctor if severe or persistent.
        3.  **Possible Causes of [Symptom Name]:**
            * Use a bulleted list.
            * Provide common, general causes (e.g., dehydration, low blood sugar, stress, common cold viruses, etc.).
            * Keep explanations concise.
        4.  **What to Do:**
            * Use a bulleted list.
            * Offer general, non-medical self-care suggestions (e.g., hydrate, rest, over-the-counter remedies, gentle exercise).
            * Reiterate the importance of avoiding activities that worsen symptoms.
        5.  **When to Seek Medical Attention:**
            * Use a bulleted list.
            * Clearly list critical "red flag" symptoms that require immediate medical attention (e.g., severe headache, chest pain, difficulty breathing, changes in vision, numbness or weakness, loss of consciousness or near-fainting, confusion or disorientation, high fever, persistent vomiting).
            * Advise contacting a doctor for persistent, worsening, or concerning symptoms even if not immediately severe.
            * Strongly emphasize that this information is not a substitute for professional medical advice.

        Example desired format for "I am feeling dizzy":
        Feeling dizzy:
        Feeling dizzy can stem from various causes, including dehydration, low blood sugar, or even anxiety. It's important to address potential underlying causes and take steps to manage the dizziness, such as staying hydrated, resting, and avoiding sudden movements. If the dizziness is severe or persistent, it's crucial to consult a doctor to rule out any serious medical conditions.

        Possible Causes of Dizziness:
        * Dehydration: Not drinking enough fluids can lead to dizziness.
        * Low Blood Sugar: Especially common for individuals with diabetes, low blood sugar can cause lightheadedness.
        * Low Blood Pressure: A sudden drop in blood pressure, particularly when standing up, can cause dizziness.
        * Anxiety and Stress: Feelings of anxiety or stress can sometimes trigger dizziness.
        * Inner Ear Problems: Conditions affecting the inner ear, such as benign paroxysmal positional vertigo (BPPV), can cause dizziness.
        * Medications: Certain medications, including some blood pressure medications, can have dizziness as a side effect.
        * Other Medical Conditions: Less common causes include heart conditions, anemia, and even certain neurological disorders.

        What to Do:
        * Hydrate: If you suspect dehydration, drink water or other fluids.
        * Rest: Lie down or sit down until the dizziness passes.
        * Move Slowly: Avoid sudden movements, especially when changing positions.
        * Address Potential Triggers: If you know what triggers your dizziness, try to avoid those situations.
        * Consult a Doctor: If dizziness is severe, persistent, or accompanied by other symptoms like chest pain or difficulty speaking, seek medical attention immediately.

        When to Seek Medical Attention:
        * Severe or Persistent Dizziness: If dizziness doesn't improve with simple measures or if it's severe and interferes with daily life.
        * Dizziness with Other Symptoms: If you experience dizziness along with chest pain, difficulty speaking, vision changes, or other concerning symptoms, seek immediate medical help.
        * Recurrent Dizziness: If you have frequent episodes of dizziness without a clear cause, consult a doctor to investigate potential underlying issues.

        Now, please respond to the following user input in the exact structured format described above, always including the disclaimers:
        User: {user_message}
        """

        # Use model.generate_content for a direct, single-turn response based on the structured prompt.
        response = model.generate_content(prompt)
        ai_response_text = response.text
        logging.info(f"Gemini responded to: '{user_message[:50]}...' with structured content.")
        return jsonify({'response': ai_response_text})
    except Exception as e:
        logging.error(f"Error in chat endpoint: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to get response from AI. Please ensure your API key is valid and the service is available, or try rephrasing your query.'}), 500

@app.route('/reminders/<int:reminder_id>', methods=['DELETE'])
def delete_reminder(reminder_id):
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        db.commit()

        job_id = f'reminder_{reminder_id}'
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logging.info(f"Removed job '{job_id}' from scheduler.")
        logging.info(f"Reminder {reminder_id} deleted successfully from DB and scheduler.")
        return jsonify({'message': f'Reminder {reminder_id} deleted successfully!'}), 200
    except Exception as e:
        logging.error(f"Error deleting reminder {reminder_id}: {e}")
        traceback.print_exc() # Added for better debugging
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/notifications', methods=['GET'])
def get_notifications():
    global notification_queue
    if notification_queue:
        notifications_to_send = list(notification_queue)
        notification_queue.clear() # Clear after sending
        logging.info(f"Sent {len(notifications_to_send)} notifications to frontend.")
        return jsonify({'notifications': notifications_to_send}), 200
    return jsonify({'notifications': []}), 200

@app.template_filter('format_frequency')
def format_frequency_filter(frequency_str):
    if frequency_str == "monday,tuesday,wednesday,thursday,friday,saturday,sunday":
        return "Everyday"
    elif frequency_str == "monday,wednesday,friday":
        return "Mon, Wed, Fri"
    elif frequency_str == "tuesday,thursday,saturday":
        return "Tue, Thu, Sat"
    days = [day.strip().capitalize() for day in frequency_str.split(',')]
    return ", ".join(days)


if __name__ == '__main__':
    init_db()
    load_and_schedule_existing_reminders(scheduler, app)
    scheduler.start()
    logging.info("APScheduler started.")
    # Use reloader=False when using APScheduler in debug mode to prevent duplicate jobs
    app.run(debug=True, port=5002, use_reloader=False)
    logging.info("Flask app started on port 5002.")