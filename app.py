from flask import Flask, request, jsonify, render_template
import requests
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import os
from dotenv import load_dotenv
import uuid
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.debug = True
app.config['PROPAGATE_EXCEPTIONS'] = True

# Configuration
app.config['TWILIO_ACCOUNT_SID'] = os.getenv('TWILIO_ACCOUNT_SID')
app.config['TWILIO_AUTH_TOKEN'] = os.getenv('TWILIO_AUTH_TOKEN')
app.config['TWILIO_PHONE_NUMBER'] = os.getenv('TWILIO_PHONE_NUMBER')
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

# Gemini API key
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# In-memory conversation store
conversations = {}

# Ngrok URL - to be pasted in the CLI
NGROK_URL = input("Paste your ngrok HTTPS URL (e.g. https://1234.ngrok.io): ").strip()

def call_gemini_ai(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        if 'candidates' in data and data['candidates']:
            parts = data['candidates'][0].get('content', {}).get('parts', [])
            if parts:
                return parts[0].get('text', '...')
        return "I'm having trouble understanding. Could you repeat that?"
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return "Something went wrong. Could you please repeat?"

def generate_ai_response(conversation_history, user_input, context):
    system_message = f"""You are an AI customer service assistant calling on behalf of a user.
The user's issue is: {context.get('issue_description', 'not specified')}

Guidelines:
1. Be polite but persistent.
2. Keep responses short and clear.
3. Ask clarifying questions if needed.
4. Never say you're an AI.
5. Confirm resolution before ending the call.
"""

    conversation_text = system_message + "\n"
    for msg in conversation_history[-5:]:
        speaker = "Customer Service" if msg['role'] == "user" else "You"
        conversation_text += f"{speaker}: {msg['content']}\n"

    if user_input:
        conversation_text += f"Customer Service: {user_input}\nYou:"

    response = call_gemini_ai(conversation_text)
    return response[:500] + "..." if len(response) > 500 else response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/initiate_call', methods=['POST'])
def initiate_call():
    data = request.form
    required_fields = ['user_number', 'customer_service_number', 'issue_description']
    if not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields'}), 400

    conversation_id = str(uuid.uuid4())
    conversations[conversation_id] = {
        'history': [],
        'context': {
            'user_number': data['user_number'],
            'customer_service_number': data['customer_service_number'],
            'issue_description': data['issue_description'],
            'status': 'initiating',
            'call_sid': None
        }
    }

    client = Client(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])

    webhook_url = f"{NGROK_URL}/voice/{conversation_id}"
    status_callback_url = f"{NGROK_URL}/call_status/{conversation_id}"

    call = client.calls.create(
        url=webhook_url,
        to=data['customer_service_number'],
        from_=app.config['TWILIO_PHONE_NUMBER'],
        record=True,
        status_callback=status_callback_url,
        status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
        timeout=30
    )

    conversations[conversation_id]['context']['call_sid'] = call.sid
    conversations[conversation_id]['context']['status'] = 'initiated'

    return jsonify({
        'status': 'call_initiated',
        'conversation_id': conversation_id,
        'call_sid': call.sid
    })

@app.route('/voice/<conversation_id>', methods=['POST'])
def voice(conversation_id):
    response = VoiceResponse()

    if conversation_id not in conversations:
        response.say("Invalid conversation. Goodbye.")
        response.hangup()
        return str(response), 200

    conversation = conversations[conversation_id]
    user_input = request.form.get('SpeechResult', '').strip()

    if not conversation['history'] and not user_input:

        greetingPromptTemplate = f'''
You are a professional customer service assistant calling on behalf of Anirudh. Your name is Otto. 
You are representing him directly, as his personal assistant.

Instructions:
- Introduce yourself as Anirudh’s assistant.
- Clearly explain his issue, using the description provided below.
- Be firm, polite, and persistent.
- Do not say you are an AI. Be firm, confident but friendly. 
- Speak naturally and confidently, like a real person.
- Make sure the issue is fully resolved before ending the call. Make decisions based on the conversation context and in the best interest of Anirudh. Now create a greeting that will be used to start the call. The greeting will automatically be read out loud by the customer service agent. These are not instructions, this is a greeting. keep it short and clear.

Issue to resolve: "{conversation['context']['issue_description']}"
'''
        greeting = call_gemini_ai(greetingPromptTemplate)
        conversation['history'].append({"role": "assistant", "content": greeting})
        response.say(greeting, voice='alice', language='te-IN')
    elif user_input:
        ai_response = generate_ai_response(conversation['history'], user_input, conversation['context'])
        conversation['history'].append({"role": "service_rep", "content": user_input})
        conversation['history'].append({"role": "assistant", "content": ai_response})
        response.say(ai_response, voice='alice', language='te-IN')
    else:
        response.say("I didn't catch that. Please say that again.", voice='alice')

    gather = Gather(
        input='speech',
        speech_ti0meout=2,
        timeout=3,
        action=f'/voice/{conversation_id}',
        method='POST',
        speech_model='experimental_conversations',
        language='en-US'
    )
    gather.say("You may speak now.", voice='alice')
    response.append(gather)

    response.say("No input detected. Goodbye.", voice='alice')
    response.hangup()

    return str(response), 200

@app.route('/call_status/<conversation_id>', methods=['POST'])
def call_status(conversation_id):
    if conversation_id in conversations:
        status = request.form.get('CallStatus')
        conversations[conversation_id]['context']['status'] = status
    return '', 200

@app.route('/get_conversation/<conversation_id>')
def get_conversation(conversation_id):
    if conversation_id in conversations:
        return jsonify({'status': 'success', 'conversation': conversations[conversation_id]})
    return jsonify({'status': 'not_found'}), 404

@app.route('/end_call/<conversation_id>', methods=['POST'])
def end_call(conversation_id):
    if conversation_id in conversations:
        try:
            client = Client(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])
            call_sid = conversations[conversation_id]['context']['call_sid']
            if call_sid:
                client.calls(call_sid).update(status='completed')
                conversations[conversation_id]['context']['status'] = 'ended'
                return jsonify({'status': 'call_ended'})
        except Exception as e:
            logger.error(f"End call error: {e}")
            return jsonify({'error': 'Failed to end call'}), 500
    return jsonify({'status': 'not_found'}), 404

# Error handlers
@app.errorhandler(500)
def internal_error(error):
    logger.error(f"500 error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(404)
def not_found(error):
    logger.error(f"404 error: {error}")
    return jsonify({'error': 'Not found'}), 404

if __name__ == '__main__':
    missing = [v for v in ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'GEMINI_API_KEY'] if not os.getenv(v)]
    if missing:
        logger.error(f"Missing env variables: {missing}")
    app.run(host='0.0.0.0', port=5000, debug=True)
