import streamlit as st
import boto3
import json
import os
import uuid
import time

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="SecRadar AI", page_icon="📊", layout="centered")
st.title("📊 SecRadar: SEC AI Analyst")
st.markdown("A Dual-Engine RAG combining **Athena SQL** for quantitative math and **Bedrock** for qualitative narrative risks.")

with st.sidebar:
    st.header("About SecRadar")
    st.markdown("""
    This application backend is a serverless AI agent designed for financial analysts. 
    It autonomously routes queries to an **Athena SQL Data Lake** for quantitative math, and an **Amazon Bedrock Knowledge Base** for qualitative SEC narratives.
    
    Currently it's limited to **Nasdaq 100** tickers and their most recent SEC filings.
    """)
    st.markdown("---")

# --- SECRETS & INITIALIZATION ---
try:
    # 1. Try fetching from Streamlit Cloud Secrets
    AGENT_ARN = st.secrets["AGENT_ARN"]

    # Initialize Boto3 with explicit Streamlit secrets
    client = boto3.client(
        'bedrock-agentcore',
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        region_name='us-east-1'
    )

except (FileNotFoundError, KeyError):
    # 2. Fallback for Local Testing
    AGENT_ARN = os.environ.get("AGENT_ARN")

    # Initialize Boto3 using your Mac's default ~/.aws credentials
    client = boto3.client('bedrock-agentcore', region_name='us-east-1')

# Fail loudly if the ARN wasn't found in either place
if not AGENT_ARN:
    st.error("CRITICAL: AGENT_ARN is not set. Please configure your environment variables or Streamlit secrets.")
    st.stop()

# --- CHAT HISTORY ---
if "messages" not in st.session_state:
    st.session_state.messages = []

if "session_id" not in st.session_state:
    # This generates a unique ID for the specific recruiter using the UI
    st.session_state.session_id = str(uuid.uuid4())

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- SECURITY GUARDRAILS ---
MAX_REQUESTS_PER_SESSION = 5  # Max questions a single person can ask
COOLDOWN_SECONDS = 60          # Seconds they must wait between questions

# Initialize trackers in session state
if "request_count" not in st.session_state:
    st.session_state.request_count = 0
if "last_request_time" not in st.session_state:
    st.session_state.last_request_time = 0

# --- USER INPUT & API CALL ---
if prompt := st.chat_input("E.g., What was Tesla's revenue in 2025, and based on their recent filings, what are their primary supply chain risks?", max_chars=300):
    current_time = time.time()
    time_since_last = current_time - st.session_state.last_request_time

    # Guardrail 1: Hard Cap
    if st.session_state.request_count >= MAX_REQUESTS_PER_SESSION:
        st.error(f"You have reached the maximum number of queries ({MAX_REQUESTS_PER_SESSION}) for this demo. Thank you for testing SecRadar!")
        st.stop()  # Halts the script so it doesn't call AWS

    # Guardrail 2: Rate Limiting (Cooldown)
    if time_since_last < COOLDOWN_SECONDS:
        st.warning(
            f"Please wait {int(COOLDOWN_SECONDS - time_since_last)} more seconds before asking another question to prevent spam.")
        st.stop()

    # If they pass the checks, record the new request
    st.session_state.request_count += 1
    st.session_state.last_request_time = current_time

    # 1. Display the user's prompt
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # 2. Call the AgentCore Cloud Endpoint
    with st.chat_message("assistant"):
        with st.spinner("SecRadar is analyzing SEC filings (Routing to SQL & RAG tools)..."):
            try:
                # 1. Grab all previous messages (excluding the current prompt we just appended)
                chat_history = st.session_state.messages[:-1]

                # 2. Package both the current prompt AND the history into the payload
                payload_data = json.dumps({
                    "prompt": prompt,
                    "history": chat_history
                }).encode('utf-8')

                # Invoke the Bedrock microVM with the Session ID
                boto_response = client.invoke_agent_runtime(
                    agentRuntimeArn=AGENT_ARN,
                    runtimeSessionId=st.session_state.session_id,
                    payload=payload_data
                )

                # --- NEW PARSING LOGIC ---
                result_text = ""

                # 1. Extract the StreamingBody from the 'response' key
                if 'response' in boto_response:
                    raw_body = boto_response['response'].read().decode('utf-8')

                    # 2. Parse the JSON returned by our agent.py ({"result": "..."})
                    try:
                        parsed_json = json.loads(raw_body)
                        # Extract just the clean Markdown text
                        final_answer = parsed_json.get("result", raw_body)
                    except json.JSONDecodeError:
                        final_answer = raw_body
                else:
                    # Absolute fail-safe if AWS changes the key again
                    final_answer = json.dumps(boto_response, default=str)

                    # Escape dollar signs to prevent Streamlit from triggering math mode ---
                    final_answer = final_answer.replace("$", r"\$")

                # 3. Render beautifully in Streamlit
                st.markdown(final_answer)
                st.session_state.messages.append({"role": "assistant", "content": final_answer})


            except Exception as e:
                error_string = str(e)

                # 1. Catch IAM / Access Denied Errors securely
                if "AccessDenied" in error_string or "403" in error_string:
                    st.error(
                        "🔒 **Access Denied:** The AI agent is currently unavailable due to security restrictions. If you are the administrator, please check the IAM policy permissions.")

                # 2. Catch Throttling / Rate Limit Errors
                elif "ThrottlingException" in error_string or "429" in error_string:
                    st.error(
                        "🚦 **Traffic Heavy:** The SEC agent is currently experiencing high traffic. Please try again in a few moments.")

                # 3. Generic fallback for everything else (without leaking the raw string)

                else:
                    st.error(
                        "⚠️ **System Error:** An unexpected error occurred while analyzing the SEC filings. Please try again later.")

                # Still print the raw error to your internal server logs for debugging!
                print(f"Backend AWS Error: {error_string}", flush=True)