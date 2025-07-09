
from flask import Flask, request, jsonify, send_from_directory, render_template
from werkzeug.utils import secure_filename
from flask_cors import CORS
import os
import re
from datetime import datetime
import sqlite3
from dotenv import load_dotenv
from twilio.rest import Client
from PIL import Image
import pytesseract
import fitz  # PyMuPDF
from collections import defaultdict
import json

load_dotenv()


TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER') 

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("Twilio client initialized successfully.")
    except Exception as e:
        print(f"Error initializing Twilio client: {e}")
else:
    print("Twilio credentials not found in environment variables. SMS functionality will be disabled.")


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')
UPLOAD_FOLDER_NAME = 'uploads'
UPLOAD_PATH = os.path.join(BASE_DIR, UPLOAD_FOLDER_NAME)
SOUNDS_DIR = os.path.join(BASE_DIR, 'sounds')

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='', template_folder=FRONTEND_DIR)
CORS(app)

app.add_url_rule('/sounds/<path:filename>', endpoint='sounds_static', view_func=lambda filename: send_from_directory(SOUNDS_DIR, filename))

if not os.path.exists(UPLOAD_PATH):
    os.makedirs(UPLOAD_PATH)
app.config['UPLOAD_FOLDER'] = UPLOAD_PATH
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

# Database Configuration
DATABASE = os.path.join(BASE_DIR, 'records.db')
USER_DATABASE = os.path.join(BASE_DIR, 'users.db') 

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_db_connection():
    conn = sqlite3.connect(USER_DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_databases():
   
    conn_records = sqlite3.connect(DATABASE)
    c_records = conn_records.cursor()
    
    
    c_records.execute('''CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT,
        content TEXT,
        timestamp TEXT,
        structured_data TEXT DEFAULT '{}'
    )''')
    
    
    c_records.execute("""CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        location TEXT,
        time TEXT NOT NULL,
        frequency TEXT NOT NULL,
        phone TEXT,
        created_at TEXT
    )""")

    
    c_records.execute("PRAGMA table_info(uploads)")
    columns = [column[1] for column in c_records.fetchall()]
    if 'structured_data' not in columns:
        c_records.execute("ALTER TABLE uploads ADD COLUMN structured_data TEXT DEFAULT '{}'")
    
    conn_records.commit()
    conn_records.close()

    
    conn_users = sqlite3.connect(USER_DATABASE)
    c_users = conn_users.cursor()
    c_users.execute('''
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    ''')
    conn_users.commit()
    conn_users.close()


init_databases()

def save_to_db(filename, content, structured_data=None):
    conn = get_db_connection()
    c = conn.cursor()
    structured_json = json.dumps(structured_data) if structured_data else None
    c.execute("INSERT INTO uploads (filename, content, structured_data, timestamp) VALUES (?, ?, ?, ?)",
              (filename, content, structured_json, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def summarize_text(text, max_lines=5):
    lines = text.strip().splitlines()
    summary = '\n'.join(line for line in lines if line.strip())
    return '\n'.join(summary.split('\n')[:max_lines])

def highlight_keywords(text, keywords):
    for keyword in keywords:
        text = re.sub(rf'\b({re.escape(keyword)})\b', r'<mark>\1</mark>', text, flags=re.IGNORECASE)
    return text

def parse_structured_data(text):
    structured = defaultdict(str)

    patterns = {
        'name': r'(?:Patient Name|Name):?\s*(.+)',
        'age': r'Age:?\s*(\d+)',
        'gender': r'Gender:?\s*(\w+)',
        'blood_pressure': r'(?:Blood Pressure|BP):?\s*([\d/]+)',
        'glucose': r'(?:Glucose|Blood Sugar|Sugar) Level?:?\s*(\d+)(?:\s*mg/dL)?',
        'cholesterol': r'Cholesterol:?\s*(\d+)(?:\s*mg/dL)?',
        'diagnosis': r'Diagnosis:?\s*[-:]?\s*(.+)',
        'medication': r'(?:Medication|Medicine):?\s*(.+)',
        'dosage': r'Dosage:?\s*(.+)',
        'frequency': r'Frequency:?\s*(.+)',
        'next_visit': r'(?:Next Follow-up|Follow up|Next Visit):?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        'doctor': r'(?:Doctor|Dr\.?|Physician):?\s*(.+)',
        'temperature': r'(?:Temperature|Temp):?\s*(\d+\.?\d*)\s*°?[CF]?',
        'heart_rate': r'(?:Heart Rate|Pulse|HR):?\s*(\d+)(?:\s*bpm)?',
        'weight': r'Weight:?\s*(\d+\.?\d*)\s*(?:kg|lbs)?',
        'height': r'Height:?\s*(\d+\.?\d*)\s*(?:cm|ft|in)?',
        'rbc_count': r'(?:RBC Count|Red Blood Cell Count):?\s*(\d+\.?\d*)\s*(?:x10\^6/uL|million/uL)?',
        'wbc_count': r'(?:WBC Count|White Blood Cell Count):?\s*(\d+\.?\d*)\s*(?:x10\^3/uL|thousand/uL)?',
        'hemoglobin': r'(?:Hemoglobin|Hb):?\s*(\d+\.?\d*)\s*(?:g/dL)?',
        'platelets': r'(?:Platelets|Platelet Count):?\s*(\d+)(?:\s*x10\^3/uL)?',
        'creatinine': r'(?:Creatinine):?\s*(\d+\.?\d*)\s*(?:mg/dL)?',
        'blood_sugar_fasting': r'(?:Fasting Blood Sugar|FBS):?\s*(\d+)(?:\s*mg/dL)?',
        'blood_sugar_post_prandial': r'(?:Post-prandial Blood Sugar|PPBS):?\s*(\d+)(?:\s*mg/dL)?',
        'hba1c': r'(?:HbA1c|Glycated Hemoglobin):?\s*(\d+\.?\d*)\s*(?:%)?',
        'urine_protein': r'(?:Urine Protein|Protein in Urine):?\s*(Negative|Trace|\+\+|Positive|\d\+)', # Can be qualitative or quantitative
        'vitamin_d': r'(?:Vitamin D|Vit D):?\s*(\d+\.?\d*)\s*(?:ng/mL)?',
        'tsh': r'(?:TSH|Thyroid Stimulating Hormone):?\s*(\d+\.?\d*)\s*(?:mIU/L|uIU/mL)?',
        'calcium': r'(?:Calcium):?\s*(\d+\.?\d*)\s*(?:mg/dL)?',
        'potassium': r'(?:Potassium|K):?\s*(\d+\.?\d*)\s*(?:mmol/L)?',
        'sodium': r'(?:Sodium|Na):?\s*(\d+\.?\d*)\s*(?:mmol/L)?',
        'bilirubin': r'(?:Bilirubin):?\s*(\d+\.?\d*)\s*(?:mg/dL)?',
        'alt': r'(?:ALT|Alanine Aminotransferase):?\s*(\d+)(?:\s*U/L)?',
        'ast': r'(?:AST|Aspartate Aminotransferase):?\s*(\d+)(?:\s*U/L)?'
    }

    for field, pattern in patterns.items():
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            if field in ['medication', 'diagnosis']:
                
                structured[field] = [match.strip() for match in matches if match.strip()]
            elif field == 'urine_protein':
                
                structured[field] = [match.strip() for match in matches if match.strip()]
            else:
                structured[field] = matches[0].strip()

    return structured

def extract_health_data_from_file_content(file_path):
    raw_text_content = ""
    file_extension = file_path.rsplit('.', 1)[1].lower()

    try:
        if file_extension == 'pdf':
            doc = fitz.open(file_path)
            for page in doc:
                raw_text_content += page.get_text()
        elif file_extension in ['png', 'jpg', 'jpeg']:
            raw_text_content = pytesseract.image_to_string(Image.open(file_path))
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_text_content = f.read()
    except Exception as e:
        raw_text_content = f"Error extracting content: {str(e)}"

    
    summary = summarize_text(raw_text_content)
    
    highlighted_text = highlight_keywords(raw_text_content, [
        'Blood Pressure', 'Glucose', 'Cholesterol', 'Diagnosis', 'Medication',
        'Dosage', 'Frequency', 'Follow-up', 'Doctor', 'Temperature', 'Heart Rate',
        'RBC Count', 'WBC Count', 'Hemoglobin', 'Platelets', 'Creatinine',
        'Fasting Blood Sugar', 'Post-prandial Blood Sugar', 'HbA1c',
        'Urine Protein', 'Vitamin D', 'TSH', 'Calcium', 'Potassium', 'Sodium',
        'Bilirubin', 'ALT', 'AST'
    ])
    structured_data = parse_structured_data(raw_text_content)

    return {
        'full_text': raw_text_content,
        'highlighted': highlighted_text,
        'summary': summary,
        'structured': dict(structured_data)
    }, raw_text_content, structured_data

@app.route('/upload_record', methods=['POST'])
def upload_record():
    if 'files' not in request.files:
        return jsonify({'error': 'No files part'}), 400

    files = request.files.getlist('files')
    responses = []

    for file in files:
        if file.filename == '':
            continue

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            extracted_data, raw_text, structured_data = extract_health_data_from_file_content(filepath)
            save_to_db(filename, raw_text, structured_data)
            responses.append({
                'filename': filename,
                'message': 'Uploaded and processed',
                'data': extracted_data
            })
        else:
            responses.append({'filename': file.filename, 'error': 'File type not allowed'})

    return jsonify({'results': responses}), 200

@app.route('/analytics_data', methods=['GET'])
def get_analytics_data():
    conn = sqlite3.connect("records.db")
    c = conn.cursor()
    
    
    c.execute("PRAGMA table_info(uploads)")
    columns = [column[1] for column in c.fetchall()]
    
    if 'structured_data' in columns:
        c.execute("SELECT filename, content, structured_data, timestamp FROM uploads ORDER BY timestamp DESC")
    else:
        c.execute("SELECT filename, content, timestamp FROM uploads ORDER BY timestamp DESC")
    
    records = c.fetchall()
    conn.close()

    if not records:
        return jsonify({'has_data': False})

    
    analytics = {
        'has_data': True,
        'summary': {
            'total_records': len(records),
            'latest_bp': None,
            'latest_glucose': None,
            'active_medications': 0
        },
        'blood_pressure': {
            'dates': [],
            'systolic': [],
            'diastolic': [],
            'values': [] 
        },
        'glucose': {
            'dates': [],
            'values': []
        },
        'cholesterol': {
            'dates': [],
            'values': []
        },
        'medications': {},
        'diagnosis': {},
        'timeline': {
            'dates': [],
            'counts': []
        },
        'heart_rate': {
            'dates': [],
            'values': []
        },
        'rbc_count': {
            'dates': [],
            'values': []
        },
        'wbc_count': {
            'dates': [],
            'values': []
        },
        'hemoglobin': {
            'dates': [],
            'values': []
        },
        'platelets': {
            'dates': [],
            'values': []
        },
        'creatinine': {
            'dates': [],
            'values': []
        },
        'blood_sugar_fasting': {
            'dates': [],
            'values': []
        },
        'blood_sugar_post_prandial': {
            'dates': [],
            'values': []
        },
        'hba1c': {
            'dates': [],
            'values': []
        },
        'urine_protein': {
            'dates': [],
            'values': [] 
        },
        'vitamin_d': {
            'dates': [],
            'values': []
        },
        'tsh': {
            'dates': [],
            'values': []
        },
        'calcium': {
            'dates': [],
            'values': []
        },
        'potassium': {
            'dates': [],
            'values': []
        },
        'sodium': {
            'dates': [],
            'values': []
        },
        'bilirubin': {
            'dates': [],
            'values': []
        },
        'alt': {
            'dates': [],
            'values': []
        },
        'ast': {
            'dates': [],
            'values': []
        },
        'available_metrics': [], 
        'recent_records': [] 
    }

    
    all_medications = set()
    timeline_data = defaultdict(int)
    
    for record in records:
        if len(record) == 4:  
            filename, content, structured_json, timestamp = record
            try:
                structured_data = json.loads(structured_json) if structured_json else {}
            except:
                structured_data = {}
        else:  
            filename, content, timestamp = record
            structured_data = parse_structured_data(content) 

       
        try:
            record_date = datetime.fromisoformat(timestamp).strftime('%Y-%m-%d')
        except:
            record_date = datetime.now().strftime('%Y-%m-%d')

        
        timeline_data[record_date] += 1

        
        def add_numeric_metric(metric_key, unit=None, is_latest=False):
            if metric_key in structured_data and structured_data[metric_key]:
                try:
                    value_str = str(structured_data[metric_key])
                    numeric_match = re.search(r'(\d+\.?\d*)', value_str)
                    if numeric_match:
                        value = float(numeric_match.group(1))
                        analytics[metric_key]['dates'].append(record_date)
                        analytics[metric_key]['values'].append(value)
                        if metric_key not in analytics['available_metrics']:
                            analytics['available_metrics'].append(metric_key)
                        if is_latest:
                            analytics['summary'][f'latest_{metric_key}'] = f"{value} {unit}" if unit else str(value)
                        return True
                except ValueError:
                    pass
            return False

        
        if 'blood_pressure' in structured_data and structured_data['blood_pressure']:
            bp_value = structured_data['blood_pressure']
            if '/' in bp_value:
                try:
                    systolic, diastolic = map(int, bp_value.split('/'))
                    analytics['blood_pressure']['dates'].append(record_date)
                    analytics['blood_pressure']['systolic'].append(systolic)
                    analytics['blood_pressure']['diastolic'].append(diastolic)
                    analytics['blood_pressure']['values'].append(f"{systolic}/{diastolic}")
                    if 'blood_pressure' not in analytics['available_metrics']:
                        analytics['available_metrics'].append('blood_pressure')
                    if not analytics['summary']['latest_bp']: 
                        analytics['summary']['latest_bp'] = f"{systolic}/{diastolic}"
                except:
                    pass
        
        
        if 'glucose' in structured_data and structured_data['glucose']:
            try:
                glucose_str = str(structured_data['glucose'])
                glucose_match = re.search(r'(\d+)', glucose_str)
                if glucose_match:
                    glucose_value = int(glucose_match.group(1))
                    analytics['glucose']['dates'].append(record_date)
                    analytics['glucose']['values'].append(glucose_value)
                    if 'glucose' not in analytics['available_metrics']:
                        analytics['available_metrics'].append('glucose')
                    if not analytics['summary']['latest_glucose']:
                        analytics['summary']['latest_glucose'] = f"{glucose_value} mg/dL"
            except:
                pass


        
        add_numeric_metric('cholesterol', 'mg/dL')
        add_numeric_metric('heart_rate', 'bpm')
        add_numeric_metric('rbc_count', 'x10^6/uL')
        add_numeric_metric('wbc_count', 'x10^3/uL')
        add_numeric_metric('hemoglobin', 'g/dL')
        add_numeric_metric('platelets', 'x10^3/uL')
        add_numeric_metric('creatinine', 'mg/dL')
        add_numeric_metric('blood_sugar_fasting', 'mg/dL')
        add_numeric_metric('blood_sugar_post_prandial', 'mg/dL')
        add_numeric_metric('hba1c', '%')
        add_numeric_metric('vitamin_d', 'ng/mL')
        add_numeric_metric('tsh', 'mIU/L')
        add_numeric_metric('calcium', 'mg/dL')
        add_numeric_metric('potassium', 'mmol/L')
        add_numeric_metric('sodium', 'mmol/L')
        add_numeric_metric('bilirubin', 'mg/dL')
        add_numeric_metric('alt', 'U/L')
        add_numeric_metric('ast', 'U/L')

       
        if 'urine_protein' in structured_data and structured_data['urine_protein']:
            
            urine_proteins = structured_data['urine_protein'] if isinstance(structured_data['urine_protein'], list) else [structured_data['urine_protein']]
            for up_val in urine_proteins:
                if up_val:
                    analytics['urine_protein']['dates'].append(record_date)
                    analytics['urine_protein']['values'].append(up_val)
                    if 'urine_protein' not in analytics['available_metrics']:
                        analytics['available_metrics'].append('urine_protein')

        
        if 'medication' in structured_data and structured_data['medication']:
            medications = structured_data['medication']
            if isinstance(medications, list):
                for med in medications:
                    med_name = str(med).strip().split(',')[0].split('(')[0].strip() # More robust splitting for med name
                    if med_name:
                        analytics['medications'][med_name] = analytics['medications'].get(med_name, 0) + 1
                        all_medications.add(med_name)
            else:
                med_name = str(medications).strip().split(',')[0].split('(')[0].strip()
                if med_name:
                    analytics['medications'][med_name] = analytics['medications'].get(med_name, 0) + 1
                    all_medications.add(med_name)
            if 'medications' not in analytics['available_metrics'] and analytics['medications']:
                analytics['available_metrics'].append('medications')

        
        if 'diagnosis' in structured_data and structured_data['diagnosis']:
            diagnoses = structured_data['diagnosis']
            if isinstance(diagnoses, list):
                for diag in diagnoses:
                    diag_name = str(diag).strip()
                    if diag_name:
                        analytics['diagnosis'][diag_name] = analytics['diagnosis'].get(diag_name, 0) + 1
            else:
                diag_name = str(diagnoses).strip()
                if diag_name:
                    analytics['diagnosis'][diag_name] = analytics['diagnosis'].get(diag_name, 0) + 1
            if 'diagnosis' not in analytics['available_metrics'] and analytics['diagnosis']:
                analytics['available_metrics'].append('diagnosis')

        
        key_findings = []
        if 'blood_pressure' in structured_data and structured_data['blood_pressure']:
            key_findings.append(f"BP: {structured_data['blood_pressure']}")
        if 'glucose' in structured_data and structured_data['glucose']:
            key_findings.append(f"Glucose: {structured_data['glucose']}")
        if 'diagnosis' in structured_data and structured_data['diagnosis']:
            if isinstance(structured_data['diagnosis'], list):
                key_findings.append(f"Diagnosis: {', '.join(structured_data['diagnosis'][:2])}")
            else:
                key_findings.append(f"Diagnosis: {structured_data['diagnosis']}")
        
        analytics['recent_records'].append({
            'date': record_date,
            'filename': filename,
            'key_findings': '; '.join(key_findings) if key_findings else 'No key findings extracted'
        })
    
    
    analytics['recent_records'].sort(key=lambda x: datetime.strptime(x['date'], '%Y-%m-%d'), reverse=True)
    analytics['recent_records'] = analytics['recent_records'][:10]


    
    analytics['summary']['active_medications'] = len(all_medications)

    
    sorted_timeline = sorted(timeline_data.items())
    analytics['timeline']['dates'] = [date for date, _ in sorted_timeline]
    analytics['timeline']['counts'] = [count for _, count in sorted_timeline]
    if analytics['timeline']['dates']:
        analytics['available_metrics'].append('timeline')

    return jsonify(analytics)

@app.route('/list_records', methods=['GET'])
def list_records():
    files = os.listdir(app.config['UPLOAD_FOLDER'])
    return jsonify({'files': files})

@app.route('/delete_record/<filename>', methods=['DELETE'])
def delete_record(filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        # Also remove from database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM uploads WHERE filename = ?", (filename,))
        conn.commit()
        conn.close()
        return jsonify({'message': f'{filename} deleted successfully'})
    else:
        return jsonify({'error': 'File not found'}), 404

@app.route('/')
def serve_login():
    return send_from_directory(app.static_folder, 'login.html')

@app.route('/dashboard')
@app.route('/index.html')
def serve_dashboard():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/aidoc.html')
def serve_aidoc():
    gemini_api_key = os.getenv('GEMINI_API_KEY')
    if not gemini_api_key:
        return "API Key not configured. Please check your .env file.", 500
    return render_template('aidoc.html', gemini_api_key=gemini_api_key)

@app.route('/chatbot.html')
def serve_chatbot():
    gemini_api_key = os.getenv('GEMINI_API_KEY')
    if not gemini_api_key:
        return "API Key not configured. Please check your .env file.", 500
    return render_template('chatbot.html', gemini_api_key=gemini_api_key)

@app.route('/pill.html')
def serve_pill_reminder():
    return send_from_directory(app.static_folder, 'pill.html')

@app.route('/workout.html')
def serve_workout_page():
    return send_from_directory(app.static_folder, 'workout.html')

@app.route('/appointment.html')
def serve_appointment_reminder():
    return send_from_directory(app.static_folder, 'appointment.html')


@app.route('/send_sms', methods=['POST'])
def send_sms():
    if not twilio_client:
        # This will now correctly trigger if env vars are truly missing or client initialization failed
        return jsonify({'error': 'SMS service not configured. Twilio credentials missing or invalid.'}), 500
    
    data = request.get_json()
    to_phone_number = data.get('to_phone_number')
    message_body = data.get('message_body')

    if not to_phone_number or not message_body:
        return jsonify({'error': 'Missing phone number or message body'}), 400

    try:
        
        if not re.match(r'^\+\d{1,15}$', to_phone_number):
             return jsonify({'error': 'Invalid phone number format. Must be in E.164 format (e.g., +1234567890).'}), 400

        message = twilio_client.messages.create(
            to=to_phone_number,
            from_=TWILIO_PHONE_NUMBER,
            body=message_body
        )
        print(f"SMS sent successfully! SID: {message.sid}")
        return jsonify({'message': 'SMS sent successfully', 'sid': message.sid}), 200
    except Exception as e:
        print(f"Error sending SMS: {e}")
        return jsonify({'error': f'Failed to send SMS: {str(e)}'}), 500


@app.route('/dashboard_summary', methods=['GET'])
def get_dashboard_summary():
    """Get a quick summary for dashboard display"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        
        c.execute("PRAGMA table_info(uploads)")
        columns = [column[1] for column in c.fetchall()]
        
        if 'structured_data' in columns:
            c.execute("SELECT COUNT(*) FROM uploads")
            total_records = c.fetchone()[0]
            
            c.execute("SELECT structured_data FROM uploads WHERE structured_data IS NOT NULL ORDER BY timestamp DESC LIMIT 10")
            recent_records_raw = c.fetchall()
        else:
            c.execute("SELECT COUNT(*) FROM uploads")
            total_records = c.fetchone()[0]
            recent_records_raw = []
        
        conn.close()
        
        # Quick stats
        latest_bp = None
        latest_glucose = None
        all_unique_meds = set() # Initialize set for unique medications
        
        for record_raw in recent_records_raw:
            if record_raw[0]:
                try:
                    structured_data = json.loads(record_raw[0])
                    if not latest_bp and 'blood_pressure' in structured_data and structured_data['blood_pressure']:
                        latest_bp = structured_data['blood_pressure']
                    if not latest_glucose and 'glucose' in structured_data and structured_data['glucose']:
                        latest_glucose = structured_data['glucose']
                    if 'medication' in structured_data and structured_data['medication']:
                        medications = structured_data['medication']
                        if isinstance(medications, list):
                            for m in medications:
                                med_name = str(m).strip().split(',')[0].split('(')[0].strip()
                                if med_name:
                                    all_unique_meds.add(med_name)
                        else:
                            med_name = str(medications).strip().split(',')[0].split('(')[0].strip()
                            if med_name:
                                all_unique_meds.add(med_name)
                except:
                    continue
        active_medications = len(all_unique_meds)
        
        return jsonify({
            'total_records': total_records,
            'latest_bp': latest_bp,
            'latest_glucose': latest_glucose,
            'active_medications': active_medications
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Appointment Reminder Routes
@app.route('/add_appointment', methods=['POST'])
def add_appointment():
    data = request.get_json()
    title = data.get('title')
    location = data.get('location')
    time = data.get('time')
    frequency = data.get('frequency')
    phone = data.get('phone')

    if not title or not time or not frequency:
        return jsonify({'error': 'Missing required fields'}), 400

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO appointments (title, location, time, frequency, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (title, location, time, frequency, phone, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    return jsonify({'message': 'Appointment saved successfully'}), 200


@app.route('/appointments', methods=['GET'])
def get_appointments():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, title, location, time, frequency, phone FROM appointments ORDER BY created_at DESC")
    appointments = [
        {'id': row[0], 'title': row[1], 'location': row[2], 'time': row[3],
         'frequency': row[4], 'phone': row[5]}
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(appointments)

@app.route('/delete_appointment/<int:appointment_id>', methods=['DELETE'])
def delete_appointment(appointment_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    conn.commit()
    deleted_count = c.rowcount
    conn.close()

    if deleted_count > 0:
        return jsonify({'message': 'Appointment deleted successfully'}), 200
    else:
        return jsonify({'error': 'Appointment not found'}), 404


if __name__ == '__main__':
    app.run(debug=True)
